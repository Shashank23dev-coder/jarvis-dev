"""Durable, bounded engineering queue. No execution or approval authority here."""
import json
import time
import os
import fcntl
from contextlib import contextmanager
from uuid import uuid4
from pydantic import Field

from .candidate_action import EngineeringAction
from .database import timestamp, valid_id, text_value
from tools.policy import ToolDenied
from tools.schemas import StrictModel

STATES = {'queued', 'planning', 'implementing', 'testing', 'reviewing',
          'waiting_for_approval', 'completed', 'blocked', 'failed'}
ACTIVE = {'planning', 'implementing', 'testing', 'reviewing'}
MAX_ATTEMPTS = 3
# A claimed job is recovered as worker_failed and re-run once this expires.
LEASE_SECONDS = 930
JOB_PAGE_SIZE = 5
FAILURE_PROJECTION_LIMIT = 16


@contextmanager
def management_lock(database):
    """Queue controls and candidate management share the worker's fence."""
    fd = os.open(database.path + '.engineering.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ToolDenied('Engineering worker or candidate management is active') from None
        yield
    finally:
        os.close(fd)


class ImprovementMetadata(StrictModel):
    observed_problem: str = Field(min_length=1, max_length=2000)
    hypothesis: str = Field(min_length=1, max_length=2000)
    baseline_evidence: dict | None = None
    proposed_change: str = Field(min_length=1, max_length=32000)
    implementation_attempts: list[dict] = Field(default_factory=list)
    test_evidence: list[dict] = Field(default_factory=list)
    review_evidence: dict | None = None
    candidate_result: str | None = None


class EngineeringJobs:
    def __init__(self, database):
        if database.path == ':memory:':
            raise ValueError('Engineering jobs require durable storage')
        self.database = database
        with database.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS engineering_jobs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, record TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS engineering_job_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES engineering_jobs(id),
                timestamp TEXT NOT NULL, event TEXT NOT NULL, status TEXT NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS engineering_worker_status (id INTEGER PRIMARY KEY, observed REAL NOT NULL)')

    def heartbeat(self):
        with self.database.transaction() as db:
            db.execute('INSERT OR REPLACE INTO engineering_worker_status VALUES (1, ?)', (time.time(),))

    def runtime(self):
        """Timestamped observation, never a claim that providers will stay usable."""
        with self.database.transaction() as db:
            row = db.execute('SELECT observed FROM engineering_worker_status WHERE id=1').fetchone()
            active = db.execute("SELECT COUNT(*) FROM engineering_jobs WHERE json_extract(record, '$.lease_until') > ?",
                                (time.time(),)).fetchone()[0]
        observed = row[0] if row else None
        from .engineering_providers import EngineeringRegistry
        from .self_edit import model_request
        registry = EngineeringRegistry.defaults(self.database, model_request).snapshot()
        from .engineering_portfolio import Portfolio
        from .engineering_decision import Decisions
        return dict(worker='active' if observed and (time.time() - observed < 20 or active) else
                    'inactive' if observed else 'unknown', observed_at=observed,
                    jobs=[project_job(j) for j in self.recent()], supported=True,
                    provider_registry=registry, portfolio=Portfolio(self.database).repository_snapshot(provider_registry=registry),
                    decisions=Decisions(self.database).recent())

    def _save(self, db, job, event):
        db.execute('UPDATE engineering_jobs SET record=? WHERE id=?',
                   (json.dumps(job, allow_nan=False), job['id']))
        db.execute('INSERT INTO engineering_job_events(job_id,timestamp,event,status) VALUES (?,?,?,?)',
                   (job['id'], timestamp(), event, job['status']))

    def _get(self, db, identifier):
        if not valid_id(identifier):
            raise ToolDenied('Invalid engineering job ID')
        row = db.execute('SELECT record FROM engineering_jobs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ToolDenied('Engineering job unavailable')
        return json.loads(row[0])

    def create(self, goal, action, arguments, context):
        from .candidate_tools import EngineeringArguments, OperatorArguments
        from tools.candidate_edit import CandidatePolicy, safe_text
        text_value(goal)
        if action is None:
            from .candidate_action import goal_digest
            action = dict(provider='codex_cli', scope='code', task_id=context.get('task_id'),
                conversation_id=context.get('conversation_id'), goal_digest=goal_digest(goal))
        action = EngineeringAction.model_validate(action).check(goal, context)
        if not all(valid_id(context.get(key)) for key in ('task_id', 'conversation_id')):
            raise ToolDenied('Invalid job source')
        schema = OperatorArguments if 'execution_mode' in arguments else EngineeringArguments
        args = schema.model_validate(arguments).model_dump(exclude_none=True)
        if not (args['inspect_paths'] and args['targeted_tests']):
            # Scope discovery needs an agent in a worktree (direct path); the
            # legacy model proposal path only edits files it was shown.
            from .direct_dev import AGENT_FOR_PROVIDER
            if action.scope != 'code' or action.provider not in AGENT_FOR_PROVIDER:
                raise ToolDenied('This engineering route needs explicit inspect_paths and targeted_tests')
        from .engineering_decision import resolve_project
        from .engineering_portfolio import Portfolio
        project_id = resolve_project(goal, Portfolio(self.database).projects(), args.get('project_id'))
        policy = CandidatePolicy()
        for path in args['inspect_paths']:
            policy.edit_path(path)
            if action.scope == 'documentation' and not path.startswith('docs/'):
                raise ToolDenied('Documentation scope mismatch')
        for path in args['targeted_tests']:
            parts = policy.check(path)
            if not parts or parts[0] != 'tests' or '/'.join(parts) != path or '::' in path:
                raise ToolDenied('Invalid job test path')
        for key in ('problem_detected', 'hypothesis'):
            safe_text(args[key])
        source = {key: context[key] for key in ('task_id', 'conversation_id')}
        # Persist the rationale in its own transaction even when submission is
        # refused. Idempotent retries keep the originally committed decision.
        with self.database.transaction() as db:
            prior = db.execute('SELECT record FROM engineering_jobs WHERE task_id=?', (action.task_id,)).fetchone()
            if prior:
                return self.get(self._resubmit(db, json.loads(prior[0]), goal, action, args, source))
        from .engineering_decision import submission_decision
        decision = submission_decision(self.database, goal, project_id=args.get('project_id'), task_id=action.task_id)
        with self.database.transaction() as db:
            row = db.execute('SELECT record FROM engineering_jobs WHERE task_id=?', (action.task_id,)).fetchone()
            if row:
                return self.get(self._resubmit(db, json.loads(row[0]), goal, action, args, source))
            job = dict(id=str(uuid4()), type='engineering', goal=goal, source=source,
                project_id=project_id, decision=decision,
                status='queued', provider=action.provider, created_at=timestamp(), started_at=None,
                completed_at=None, current_stage='queued', attempts=0, candidate_id=None,
                candidate_ids=[], test_result=[], review_result=None, error=None,
                block_reason=None, control=None, lease_owner=None, lease_until=0,
                action=action.model_dump(), arguments=args,
                improvement=ImprovementMetadata(observed_problem=args['problem_detected'],
                    hypothesis=args['hypothesis'], proposed_change=goal).model_dump())
            if 'execution_mode' in args:
                from .engineering_operator import routes
                job.update(provider=routes(args['execution_mode'])[0],
                           operator=dict(mode=args['execution_mode'], phase='inspect', revision=0,
                                         ready=False, controller_validated=False))
            db.execute('INSERT INTO engineering_jobs VALUES (?,?,?,?)',
                       (job['id'], action.task_id, job['created_at'], json.dumps(job)))
            self._save(db, job, 'created')
        return self.get(job['id'])

    def _resubmit(self, db, job, goal, action, args, source):
        """The task's one job: idempotent while live; a rejected attempt is requeued for a new candidate.

        Requeueing keeps the job's attempt count and limit, so it spends the
        existing retry budget and never renews it.
        """
        if any(job[key] != value for key, value in
               [('goal', goal), ('action', action.model_dump()), ('source', source)]):
            raise ToolDenied('Engineering job source mismatch')
        retry = (job['status'] == 'blocked' and job['control'] is None and not job['lease_owner']
                 and not job.get('operator') and job['attempts'] < job.get('attempt_limit', MAX_ATTEMPTS))
        if retry and job.get('candidate_id'):
            busy = db.execute('SELECT busy FROM candidates WHERE id=?', (job['candidate_id'],)).fetchone()
            retry = not (busy and busy[0])
        if not retry:
            if job['arguments'] != args:
                raise ToolDenied('Engineering retry budget exhausted' if job['status'] == 'failed'
                                 else 'Engineering job source mismatch')
            return job['id']
        if job['arguments'] != args:
            job.setdefault('input_revisions', []).append(dict(timestamp=timestamp(), arguments=job['arguments']))
            job['arguments'] = args
            job['improvement'].update(observed_problem=args['problem_detected'], hypothesis=args['hypothesis'])
        # candidate_ids keeps the rejected candidate; the worker starts a new one.
        job.update(status='queued', current_stage='queued', candidate_id=None, control=None,
                   error=None, block_reason=None, completed_at=None)
        self._save(db, job, 'resubmitted')
        return job['id']

    def get(self, identifier):
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            job['history'] = [dict(row) for row in db.execute(
                'SELECT sequence,timestamp,event,status FROM engineering_job_events WHERE job_id=? ORDER BY sequence',
                (identifier,))]
            return job

    def recent(self, conversation_id=None, *, before=None):
        with self.database.transaction() as db:
            cursor = self._get(db, before) if before else None
            rows = db.execute('''SELECT id FROM engineering_jobs
                WHERE (? IS NULL OR json_extract(record, '$.source.conversation_id') = ?)
                AND (? IS NULL OR (created_at,id) < (?,?))
                ORDER BY created_at DESC,id DESC LIMIT ?''',
                (conversation_id, conversation_id, before,
                 cursor['created_at'] if cursor else None, before, JOB_PAGE_SIZE)).fetchall()
            return [j for row in rows if (j := self.get(row[0])) and
                    (conversation_id is None or j['source']['conversation_id'] == conversation_id)]

    def control(self, identifier, operation, *, authority=None):
        with management_lock(self.database):
            return self._control_locked(identifier, operation, authority=authority)

    def _control_locked(self, identifier, operation, *, authority=None):
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            if job['control'] == 'cancel':
                raise ToolDenied('Cancelled engineering jobs are terminal')
            if job['lease_owner'] or job['status'] in ACTIVE:
                raise ToolDenied('Active job cannot be controlled safely')
            # The management flock fences the worker and its descendants. An
            # expired candidate lease left by a crash must not block recovery.
            from .candidate_store import CandidateStore
            candidate = CandidateStore(self.database).get(job.get('candidate_id'), internal=True)
            if candidate and candidate['busy']:
                raise ToolDenied('Active job cannot be controlled safely')
            if operation in {'pause', 'cancel'} and job['status'] in {'queued', 'blocked', 'failed'}:
                job.update(status='blocked', control=operation, block_reason=operation)
            elif operation == 'resume' and job['status'] in {'blocked', 'failed'}:
                if job['attempts'] >= job.get('attempt_limit', MAX_ATTEMPTS):
                    # Explicit owner recovery grants one more bounded execution;
                    # crash recovery cannot renew its own budget or erase history.
                    from .engineering_authority import require_owner
                    require_owner(authority)
                    job['attempt_limit'] = job['attempts'] + 1
                    self._save(db, job, 'owner_retry_authorized')
                if not job.get('failure') and job.get('candidate_id'):
                    # Migrate known safe legacy denials when recovering an old
                    # job. No provider text is copied into the projection.
                    from .candidate_provider import execution_error_code, failure_evidence
                    row = db.execute('SELECT record FROM candidates WHERE id=?', (job['candidate_id'],)).fetchone()
                    if row:
                        candidate = json.loads(row[0])
                        code = execution_error_code(ToolDenied(candidate.get('error') or ''))
                        job['failure'] = candidate.get('failure') or failure_evidence(
                            job['provider'], code, candidate.get('stage', 'planning'), max(1, job['attempts']))
                        job.setdefault('failures', []).append(job['failure'])
                job.update(status='queued', control=None, block_reason=None, error=None, completed_at=None)
            else:
                raise ToolDenied('Engineering job transition denied')
            job['current_stage'] = job['status']
            self._save(db, job, operation)
        return self.get(identifier)

    def revise_inputs(self, identifier, arguments, *, authority):
        """Owner recovery of obsolete inspection/test inputs; objective stays bound."""
        with management_lock(self.database):
            return self._revise_inputs_locked(identifier, arguments, authority=authority)

    def _revise_inputs_locked(self, identifier, arguments, *, authority):
        from .engineering_authority import require_owner
        from .candidate_tools import EngineeringArguments, OperatorArguments
        from tools.candidate_edit import CandidatePolicy, safe_text
        require_owner(authority)
        schema = OperatorArguments if 'execution_mode' in arguments else EngineeringArguments
        args = schema.model_validate(arguments).model_dump(exclude_none=True)
        policy = CandidatePolicy()
        for path in args['inspect_paths']:
            policy.edit_path(path)
        for path in args['targeted_tests']:
            parts = policy.check(path)
            if not parts or parts[0] != 'tests' or '/'.join(parts) != path or '::' in path:
                raise ToolDenied('Invalid job test path')
        for key in ('problem_detected', 'hypothesis'):
            safe_text(args[key])
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            busy = db.execute('SELECT busy FROM candidates WHERE id=?', (job.get('candidate_id'),)).fetchone()
            if job['status'] not in {'blocked', 'failed'} or job['lease_owner'] or busy and busy[0]:
                raise ToolDenied('Only inactive blocked job inputs may be revised')
            if job['action']['scope'] == 'documentation' and any(
                    not p.startswith('docs/') for p in args['inspect_paths']):
                raise ToolDenied('Documentation scope mismatch')
            if args.get('project_id', job.get('project_id')) != job.get('project_id'):
                raise ToolDenied('Input recovery cannot change the job project')
            if job.get('operator') and args != job['arguments']:
                raise ToolDenied('Checkpointed operator inputs are immutable; submit a new goal for changed inputs')
            job.setdefault('input_revisions', []).append(dict(timestamp=timestamp(), arguments=job['arguments']))
            job['arguments'] = args
            self._save(db, job, 'inputs_revised')
        return self.get(identifier)

    def reconcile_candidate(self, candidate):
        """Refresh evidence after owner test/review/discard under the worker lock."""
        identifier = candidate.get('source_context', {}).get('engineering_job_id')
        if identifier is None:
            return
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            if job['candidate_id'] != candidate['id'] or job['lease_owner'] or job['control']:
                return
            # Database.transaction joins nested calls under its reentrant lock;
            # keeping this transaction open prevents a concurrent cancel race.
            self.finish(identifier, None, candidate)

    def claim(self, owner, *, now=None):
        """Caller must hold the worker flock, including during lease recovery."""
        now = time.time() if now is None else now
        with self.database.transaction() as db:
            for row in db.execute('SELECT record FROM engineering_jobs ORDER BY created_at').fetchall():
                job = json.loads(row[0])
                if job['status'] in ACTIVE and job['lease_until'] < now:
                    from .candidate_store import CandidateStore
                    from .candidate_provider import failure_evidence
                    candidate = CandidateStore(self.database).get(job.get('candidate_id'), internal=True)
                    if candidate and candidate['busy']:
                        continue
                    if candidate:
                        self._merge_candidate_failures(job, candidate)
                    failure = failure_evidence(job['provider'], 'worker_failed',
                        job['current_stage'] if job['current_stage'] in ACTIVE else 'planning', job['attempts'])
                    if failure not in job.setdefault('failures', []):
                        job['failures'].append(failure)
                    job.update(status='queued', current_stage='queued', lease_owner=None, lease_until=0,
                               failure=failure, error='Engineering worker was interrupted')
                    self._save(db, job, 'lease_recovered')
                if job['status'] != 'queued':
                    continue
                if job['attempts'] >= job.get('attempt_limit', MAX_ATTEMPTS):
                    job.update(status='failed', current_stage='failed', error='Retry limit reached', completed_at=timestamp())
                    self._save(db, job, 'retry_limit')
                    continue
                job.update(status='planning', current_stage='planning', attempts=job['attempts'] + 1,
                           lease_owner=owner, lease_until=now + LEASE_SECONDS,
                           started_at=job['started_at'] or timestamp())
                self._save(db, job, 'claimed')
                return job
        return None

    def queued(self):
        """Whether claim() has a queued job to run; apply preparation yields to it between shards."""
        with self.database.transaction() as db:
            rows = db.execute("SELECT record FROM engineering_jobs WHERE json_extract(record, '$.status') = 'queued'")
            return any(job['attempts'] < job.get('attempt_limit', MAX_ATTEMPTS)
                       for job in (json.loads(row[0]) for row in rows.fetchall()))

    def advance(self, identifier, owner, status):
        """Record the live stage of a claimed job. The lease is deliberately not renewed."""
        if status not in {'implementing', 'testing', 'reviewing'}:
            raise ValueError('Invalid engineering stage')
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            if job['lease_owner'] != owner:
                raise ToolDenied('Stale engineering lease')
            if job['current_stage'] != status:
                job.update(status=status, current_stage=status)
                self._save(db, job, 'stage_' + status)

    @staticmethod
    def _merge_candidate_failures(job, candidate):
        offsets = job.setdefault('candidate_failure_offsets', {})
        prior_failures = candidate.get('failures', [])
        job.setdefault('failures', []).extend(prior_failures[offsets.get(candidate['id'], 0):])
        offsets[candidate['id']] = len(prior_failures)
        from .candidate_provider import work_attribution, merge_attribution
        job['attribution'] = merge_attribution(job.get('attribution'), work_attribution(candidate))

    def finish(self, identifier, owner, candidate=None, *, failure=None):
        from .engineering_portfolio import Portfolio
        read_only = {p['project_id'] for p in Portfolio(self.database).projects() if p.get('read_only')}
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            if job['lease_owner'] != owner:
                raise ToolDenied('Stale engineering lease')
            passed = bool(candidate and candidate['state'] == 'passed' and not candidate.get('busy')
                          and candidate.get('tested_fingerprint') == candidate.get('reviewed_fingerprint')
                          and (candidate.get('reviewer_result') or {}).get('verdict') == 'passed'
                          and (not job.get('operator') or candidate.get('operator', {}).get('ready')))
            from .engineering_authority import assess_action
            consequence = assess_action('candidate_apply', goal=candidate['goal'],
                changed_paths=candidate['files_changed'],
                read_only_target=job.get('project_id') in read_only) if passed else None
            status = ('waiting_for_approval' if consequence.approval_required else 'completed') if passed else (
                'failed' if job['attempts'] >= job.get('attempt_limit', MAX_ATTEMPTS) else 'blocked')
            if passed and job.get('operator') and job['project_id'] == 'jarvis-core':
                status = 'waiting_for_approval'
            job['authority'] = consequence.model_dump() if consequence else None
            job.update(status=status, current_stage=status, lease_owner=None, lease_until=0,
                       error=None if passed else 'Candidate execution did not pass tests and review',
                       block_reason=None if passed else 'Explicit retry required',
                       completed_at=timestamp() if status in {'failed', 'completed'} else None)
            if candidate:
                job['manual_operations'] = candidate.get('manual_operations', [])
                job['files_changed_count'] = len(candidate['files_changed'])
                if candidate.get('operator'):
                    job.update(operator=candidate['operator'], worktree=candidate['worktree'],
                               branch=candidate['branch'], commit_sha=candidate['operator'].get('commit_sha'))
                self._merge_candidate_failures(job, candidate)
                improvement = candidate.get('improvement', {})
                job.update(provider=improvement.get('implementation_provider', job['provider']),
                    provider_results=candidate.get('provider_results', []),
                    provider_route=candidate.get('provider_route'),
                    provider_blocker=candidate.get('provider_blocker'))
                job.update(test_result=candidate['test_results'], review_result=candidate['reviewer_result'])
                job['improvement'].update(baseline_evidence=improvement.get('baseline_comparison'),
                    baseline_outcome=improvement.get('baseline_outcome'),
                    implementation_attempts=improvement.get('failures_revisions', []),
                    test_evidence=candidate['test_results'], review_evidence=candidate['reviewer_result'],
                    candidate_result=candidate['state'])
            from .candidate_provider import failure_evidence
            if not passed:
                failure = failure or (candidate or {}).get('failure')
                if failure is None:
                    failure = failure_evidence(job['provider'],
                        'review_failed' if candidate and candidate.get('reviewer_result') else
                        'tests_failed' if candidate and candidate.get('test_results') else 'worker_failed',
                        job['current_stage'] if job['current_stage'] in ACTIVE else
                        (candidate or {}).get('stage', 'planning'), job['attempts'])
                if failure not in job.setdefault('failures', []):
                    job['failures'].append(failure)
            job['failure'] = failure if not passed else None
            self._save(db, job, 'execution_finished')
        return self.get(identifier)


def project_job(job):
    """Closed production projection: no paths, model prose, logs or host config."""
    from .candidate_provider import (ProviderId, project_failure, failure_evidence, baseline_outcome_for,
                                     project_manual_operations, project_execution_state)
    from pydantic import TypeAdapter
    from .candidate_provider import WorkAttribution, project_provider_results, work_attribution
    from .engineering_providers import ProviderBlocker
    from .engineering_operator import project_operator
    failures = job.get('failures', [])
    failure_count = job.get('failure_count', len(failures))
    if type(failure_count) is not int or failure_count < len(failures):
        raise ToolDenied('Invalid engineering failure count')
    failures = [project_failure(item) for item in failures[-FAILURE_PROJECTION_LIMIT:]]
    if not valid_id(job.get('id')) or job.get('status') not in STATES:
        raise ToolDenied('Invalid engineering job result')
    try:
        TypeAdapter(ProviderId).validate_python(job.get('provider'))
    except ValueError:
        raise ToolDenied('Invalid engineering provider') from None
    from datetime import datetime
    stage = job.get('current_stage')
    attempts = job.get('attempts')
    candidate_id = job.get('candidate_id')
    control = job.get('control')
    if (stage not in STATES or type(attempts) is not int or attempts < 0
            or candidate_id is not None and not valid_id(candidate_id)
            or control not in {None, 'pause', 'cancel'} or job.get('type') != 'engineering'):
        raise ToolDenied('Invalid engineering job metadata')
    for key in ('created_at', 'started_at', 'completed_at'):
        value = job.get(key)
        if value is not None:
            try:
                datetime.fromisoformat(value)
            except (ValueError, TypeError):
                raise ToolDenied('Invalid engineering job timestamp') from None
    tests = []
    for item in job.get('test_result', [])[-3:]:
        if item.get('phase') not in {'targeted', 'full_python', 'full_ui'} or type(item.get('returncode')) is not int:
            raise ToolDenied('Invalid engineering test evidence')
        tests.append(dict(phase=item['phase'], returncode=item['returncode'],
                          timed_out=bool(item.get('timed_out')), truncated=bool(item.get('truncated'))))
    verdict = (job.get('review_result') or {}).get('verdict')
    if verdict not in {None, 'passed', 'needs_revision', 'rejected'}:
        raise ToolDenied('Invalid engineering review evidence')
    history = []
    for event in job.get('history', [])[-16:]:
        if type(event.get('sequence')) is not int or event.get('status') not in STATES:
            raise ToolDenied('Invalid engineering history')
        try:
            datetime.fromisoformat(event['timestamp'])
        except (KeyError, ValueError, TypeError):
            raise ToolDenied('Invalid engineering history timestamp') from None
        history.append({key: event[key] for key in ('sequence', 'timestamp', 'status')})
    # Reprojection on the production client preserves only closed metadata.
    lease = job.get('lease')
    recovery = job.get('recovery')
    if lease is None:
        lease = dict(active=bool(job.get('lease_owner')),
                     expired=bool(job.get('lease_owner') and job.get('lease_until', 0) < time.time()))
    if recovery is None:
        recovery = dict(count=sum(e.get('event') == 'lease_recovered' for e in job.get('history', [])),
                        retry_available=job['status'] in {'blocked', 'failed'} and control != 'cancel'
                        and not lease['active'])
    if (set(lease) != {'active', 'expired'} or any(type(v) is not bool for v in lease.values())
            or set(recovery) != {'count', 'retry_available'}
            or type(recovery['count']) is not int or recovery['count'] < 0
            or type(recovery['retry_available']) is not bool):
        raise ToolDenied('Invalid engineering recovery metadata')
    baseline_outcome = baseline_outcome_for(job)
    execution_state = project_execution_state(job, job['current_stage'])
    visible_failure = (failure_evidence('baseline_validator', baseline_outcome['failure_code'],
        'testing', max(1, attempts)) if execution_state == 'blocked_before_implementation'
        and baseline_outcome and baseline_outcome['failure_code'] else job.get('failure'))
    manual_operations = project_manual_operations(job.get('manual_operations', []))
    changed_count = job.get('files_changed_count', 0)
    if type(changed_count) is not int or not 0 <= changed_count <= 400:
        raise ToolDenied('Invalid changed file count')
    owner_status = None
    if job['status'] == 'blocked' and execution_state == 'blocked_before_implementation' and baseline_outcome:
        owner_status = ('Blocked before implementation. Stage: Baseline validation. '
            f"Result: {baseline_outcome['result'].title()}. "
            f"{job['provider']} invoked: {'Yes' if baseline_outcome['provider_invoked'] else 'No'}. "
            f"Files changed: {changed_count}. Retryable: {'Yes' if baseline_outcome['result'] in {'timeout', 'fail'} else 'No'}.")
    return {key: job.get(key) for key in ('id', 'type', 'status', 'provider', 'created_at',
        'started_at', 'completed_at', 'current_stage', 'attempts', 'candidate_id', 'control')} | {
        'project_id': project_reference(job.get('project_id')),
        **({'operator': project_operator(job['operator'])} if job.get('operator') else {}),
        'decision': project_decision(job.get('decision')),
        'error': 'Engineering job requires attention' if job.get('error') else None,
        'failure': project_failure(visible_failure),
        'failures': failures, 'failure_count': failure_count,
        'failures_truncated': failure_count > len(failures),
        'blocker': control or ((visible_failure or {}).get('code') if visible_failure and job['status'] in {'blocked', 'failed'} else
            'retry_limit' if attempts >= MAX_ATTEMPTS and job['status'] == 'failed'
            else 'execution_not_passed' if job['status'] in {'blocked', 'failed'}
            else 'human_approval_required' if job['status'] == 'waiting_for_approval' else None),
        'lease': lease, 'recovery': recovery, 'history': history,
        'attribution': WorkAttribution.model_validate(job.get('attribution') or work_attribution(job)).model_dump(),
        'provider_results': project_provider_results(job.get('provider_results', [])),
        'provider_route': project_provider_route(job.get('provider_route'), job.get('provider')),
        'provider_blocker': ProviderBlocker.model_validate(job['provider_blocker']).model_dump() if job.get('provider_blocker') else None,
        'test_result': tests, 'review_result': {'verdict': verdict},
        'baseline_outcome': baseline_outcome, 'execution_state': execution_state,
        'manual_operations': manual_operations, 'files_changed_count': changed_count,
        'implementation_attempts': 0 if execution_state == 'blocked_before_implementation' else
            job.get('implementation_attempts', len((job.get('improvement') or {}).get('implementation_attempts', []))),
        'owner_status': owner_status}


def project_provider_route(route, requested):
    from .candidate_provider import ProviderId
    from pydantic import TypeAdapter
    if route is None:
        route = dict(provider_requested=requested, provider_used=None, adapter_type=None,
                     billing_class=None, fallback_reason=None)
    requested = TypeAdapter(ProviderId).validate_python(route['provider_requested'])
    used = TypeAdapter(ProviderId | None).validate_python(route.get('provider_used'))
    adapter = route.get('adapter_type')
    billing = route.get('billing_class')
    reason = route.get('fallback_reason')
    if adapter not in {None, 'cli', 'api'} or billing not in {None, 'subscription_cli', 'api_billed'}:
        raise ToolDenied('Invalid provider route')
    if billing != (None if adapter is None else 'subscription_cli' if adapter == 'cli' else 'api_billed'):
        raise ToolDenied('Invalid provider billing class')
    if reason is not None and reason not in {'financial_authorization_required', 'provider_authentication',
            'provider_quota', 'provider_rate_limit', 'provider_unavailable', 'provider_invocation_failed',
            'provider_timeout', 'provider_invalid_response', 'provider_output_limit', 'provider_request_rejected',
            'reviewer_independence', 'provider_excluded', 'capability_mismatch', 'route_not_allowed'}:
        raise ToolDenied('Invalid provider fallback reason')
    return dict(provider_requested=requested, provider_used=used, adapter_type=adapter,
                billing_class=billing, fallback_reason=reason,
                fallback_state='blocked' if reason == 'financial_authorization_required' else
                'used' if used and used != requested else 'none')


def project_reference(value):
    from .engineering_portfolio import Identifier
    from pydantic import TypeAdapter
    return TypeAdapter(Identifier | None).validate_python(value)


def project_decision(value):
    from .engineering_decision import Decision
    return Decision.model_validate(value).model_dump() if value is not None else None
