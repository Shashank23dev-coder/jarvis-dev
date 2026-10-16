"""Direct development: one agent edits an isolated worktree, a different agent reviews.

Isolation reuses SelfEditService._prepare(workspace_only=True): a private bare clone
and worktree of a clean, registered project baseline. Tests reuse the candidate
sandbox (_tests). Agent state and execution go through EngineeringRegistry; this
module only adds the role choice, recorded with a reason for every decision.
The legacy candidate/engineering paths are untouched.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from tools.policy import ToolDenied
from .candidate_provider import FAILURES, ProviderFailure, execution_error_code
from .database import timestamp
from .dev_agents import ADAPTERS, FailureLog
from .engineering_providers import EngineeringRegistry, ProviderBlocker, Registration
from .engineering_repository import development_service
from .self_edit import Limits, SelfEditService

DEFAULT_ROLES = {'implementing': 'codex_cli', 'reviewing': 'claude_cli'}
MAX_ATTEMPTS = 3  # the first attempt plus up to 2 revisions by the same author
UNUSABLE = {'unavailable', 'not_authenticated', 'quota_blocked', 'invocation_failed'}


class NoAgentAvailable(ProviderFailure):
    def __init__(self, stage, decision):
        self.decision = decision
        super().__init__(decision['default'], 'provider_unavailable')

    def __str__(self):
        reasons = ', '.join(f"{c['agent']}={c['reason']}" for c in self.decision['candidates'])
        return f"no usable {self.decision['stage']} agent: {reasons}"


def default_state_dir():
    return Path(os.getenv('XDG_STATE_HOME') or Path.home() / '.local/state') / 'jarvis-direct-dev'


class Router:
    """Chooses an agent per stage from registry state plus a live login probe."""

    def __init__(self, registry, *, roles=None, api_authorized=False, log=None, remaining=None):
        self.registry, self.api_authorized = registry, api_authorized
        self.remaining = remaining or (lambda: 10)
        self.roles = dict(DEFAULT_ROLES, **(roles or {}))
        self.decisions = []
        self.log = log or (lambda decision: None)

    def choose(self, stage, *, exclude=()):
        default = self.roles[stage]
        order = list(dict.fromkeys([default, *self.registry.registrations]))
        candidates, chosen = [], None
        for name in order:
            reason = self._unusable(name, stage, exclude)
            candidates.append(dict(agent=name, usable=reason is None, reason=reason or 'usable'))
            if reason is None:
                chosen = name
                break
        default_reason = next(c['reason'] for c in candidates if c['agent'] == default)
        decision = dict(at=timestamp(), stage=stage, default=default, chosen=chosen,
                        excluded=sorted(exclude), candidates=candidates,
                        reason=('default_usable' if chosen == default else
                                f'default_{default_reason}' if chosen else 'no_usable_agent'))
        self.decisions.append(decision)
        self.log(decision)
        if chosen is None:
            raise NoAgentAvailable(stage, decision)
        return chosen, decision

    def _unusable(self, name, stage, exclude):
        if name in exclude:
            return 'reviewer_must_differ_from_author' if stage == 'reviewing' else 'excluded'
        registration = self.registry.registrations.get(name)
        if registration is None:
            return 'not_registered'
        if registration.provider_type != 'cli' and not self.api_authorized:
            return 'api_billing_not_authorized'
        if stage not in registration.capabilities:
            return 'capability_mismatch'
        state = self.registry.state(name)
        if not registration.configured:
            return 'cli_not_installed'
        if state.current_usability in UNUSABLE:
            return f'recent_{state.current_usability}'
        probe = getattr(registration.adapter, 'probe', None)
        if probe:
            try:
                live = probe(max(1, min(10, self.remaining())))
            except Exception as exc:
                failures = getattr(registration.adapter, 'failures', None)
                if failures:
                    failures.record(registration.adapter.run_id, f'{stage}.route_probe', name, exc)
                live = 'invocation_failed'
            if live != 'available':
                self.registry.observe(name, live, 'provider_failure', 'engineering_request',
                    'provider_authentication' if live == 'not_authenticated' else
                    'provider_invocation_failed' if live == 'invocation_failed' else 'provider_unavailable',
                    stage=stage)
                return f'probe_{live}'
        return None


class DirectDevelopment:
    def __init__(self, database, *, candidate_root, state_dir=None, agents=None, roles=None,
                 api_authorized=False, limits=None, tester=None):
        self.database, self.tester = database, tester
        self.candidate_root = Path(candidate_root)
        self.state_dir = Path(state_dir or default_state_dir())
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.failures = FailureLog(self.state_dir / 'failures.jsonl')
        self.agents, self.roles, self.api_authorized = agents, roles, api_authorized
        self.limits = limits or Limits(seconds=900)

    def _registry(self, run_id):
        agents = self.agents or [cls(self.state_dir / 'agent-bin', self.failures, run_id) for cls in ADAPTERS.values()]
        registrations = []
        for agent in agents:
            agent.failures, agent.run_id = self.failures, run_id
            executable = getattr(agent, 'executable', lambda: True)
            registrations.append(Registration(agent, getattr(agent, 'provider_type', 'cli'), bool(executable())))
        return EngineeringRegistry(self.database, registrations)

    def _append(self, name, value):
        descriptor = os.open(self.state_dir / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'a') as handle:
            handle.write(json.dumps(value) + '\n')

    def run(self, project_id, task, inspect_paths, targeted_tests, *, timeout=900, source_context=None,
            on_stage=None):
        """Run one direct-development candidate within `timeout` seconds in total.

        Only the targeted tests run (decision D1, 2026-10-16): the whole run must
        fit one 930 s job lease. source_context links the candidate to a job.
        """
        run_id = str(uuid4())
        registry = self._registry(run_id)
        deadline = time.monotonic() + min(timeout, self.limits.seconds)
        router = Router(registry, roles=self.roles, api_authorized=self.api_authorized,
                        log=lambda d: self._append('routing.jsonl', dict(run_id=run_id, **d)),
                        remaining=lambda: deadline - time.monotonic())
        report = dict(run_id=run_id, project_id=project_id, task=task, started_at=timestamp(),
                      status='running', stage='prepare', routing=router.decisions, budget_seconds=min(timeout, self.limits.seconds))
        stage_changed = on_stage or (lambda stage: None)
        try:
            service = development_service(
                SelfEditService(self.database, candidate_root=self.candidate_root, limits=self.limits,
                                tester=self.tester), project_id)
            report['source_root'] = str(service.source)
            record = service.store.create(task, inspect_paths, targeted_tests,
                                          source_context=dict(source_context or {}, project_id=project_id,
                                                              direct_dev_run=run_id))
            record.update(source_root=str(service.source), limits=asdict(self.limits), execution_origin='provider')
            service.record, service.deadline, service.originals = record, deadline, {}
            service._prepare(workspace_only=True)
            _, bare, worktree = service._paths(record)
            report.update(candidate_id=record['id'], worktree=str(worktree), base_commit=record['base_commit'],
                          branch=record['branch'], repository=str(bare), attempts=[])
            report['stage'] = 'implement'
            stage_changed('implementing')
            author, _ = router.choose('implementing')
            report['author'], feedback = author, None
            for number in range(1, MAX_ATTEMPTS + 1):
                attempt = dict(attempt=number, author=author)
                report['attempts'].append(attempt)
                report['stage'] = 'implement'
                stage_changed('implementing')
                implementation, used = registry.complete(
                    dict(action='implement', worktree=str(worktree), task=_revision(task, feedback)), '',
                    service._check(), stage='implementing', preferred=author, allowed=(author,),
                    validate=_implementation)
                attempt['implementation'] = implementation
                report.update(implementation=implementation, route_implement=registry.last_route)
                record['improvement'] = dict(implementation_provider=used, scope='code')

                report['stage'] = 'test'
                stage_changed('testing')
                service._tests(focused=True)
                tests = [dict(phase=t['phase'], paths=t['paths'], returncode=t['returncode'],
                              timed_out=t['timed_out'], backend=(t['sandbox'] or {}).get('backend'),
                              stdout_tail=t['stdout'][-1500:])
                         for t in record['test_results'] if t['fingerprint'] == record['fingerprint']]
                passed = record.get('tested_fingerprint') == record['fingerprint']
                attempt.update(tests=tests, tests_passed=passed, files_changed=record['files_changed'])
                report.update(diff=record['diff'], diff_summary=record['diff_summary'],
                              files_changed=record['files_changed'], tests=tests, tests_passed=passed)
                if not passed:
                    # As in _review: only a change with passing sandboxed tests is reviewed.
                    attempt['outcome'] = report['status'] = 'tests_failed'
                    feedback = 'The sandboxed tests failed:\n' + '\n'.join(
                        f"[{t['phase']} rc={t['returncode']}]\n{t['stdout_tail']}" for t in tests if t['returncode'])
                    continue

                report['stage'] = 'commit'
                commit = _commit(service, used, number)
                attempt['commit'] = report['commit'] = commit

                report['stage'] = 'review'
                stage_changed('reviewing')
                reviewer, _ = router.choose('reviewing', exclude={used})
                review, reviewed_by = registry.complete(
                    dict(action='review', task=task, commit=commit, diff=record['diff'], tests=tests), '',
                    service._check(), stage='reviewing', preferred=reviewer, allowed=(reviewer,),
                    exclude={used}, validate=_review)
                if reviewed_by == used:  # defence in depth; Router and registry both exclude the author
                    raise ToolDenied('Reviewer must differ from author')
                if service._workgit('rev-parse', 'HEAD').strip() != commit or \
                        service._diff() != record['tested_fingerprint']:
                    raise ToolDenied('Worktree changed during review')
                record['reviewer_result'] = dict(review, provider=reviewed_by, commit=commit,
                                                 fingerprint=record['fingerprint'])
                service.store.save(record, 'direct_review_finished')
                attempt.update(reviewer=reviewed_by, review=review, outcome=review['verdict'])
                report.update(reviewer=reviewed_by, review=review, reviewed_commit=commit,
                              route_review=registry.last_route, status=review['verdict'])
                if review['verdict'] != 'needs_revision':
                    break
                feedback = 'An independent reviewer asked for revision:\n' + review['summary']
            report['attempts_exhausted'] = report['status'] in {'tests_failed', 'needs_revision'}
        except Exception as exc:
            entry = self.failures.record(run_id, report['stage'], getattr(exc, 'provider', None), exc)
            report.update(status='failed', failure=dict(stage=report['stage'], exception=entry['exception'],
                                                        message=entry['message'], code=entry['code']))
            if isinstance(exc, NoAgentAvailable):
                report['failure']['routing'] = exc.decision
                report['failure']['blocker'] = ProviderBlocker(
                    stage=exc.decision['stage'], excluded_providers=exc.decision['excluded'],
                    reasons=[registry.state(c['agent']) for c in exc.decision['candidates']
                             if c['agent'] in registry.registrations]).model_dump()
                # The cause is the first agent that could have taken the stage, not the excluded author.
                causes = [(r.get('observation') or {}).get('code') for r in report['failure']['blocker']['reasons']
                          if r['provider_id'] not in exc.decision['excluded']]
                report['failure']['code'] = next((c for c in causes if c), report['failure']['code'])
            blocker = getattr(exc, 'blocker', None)
            if blocker and blocker['reasons']:
                # RoutingBlocked always says provider_unavailable; the observation has the cause.
                report['failure']['code'] = (blocker['reasons'][0].get('observation') or {}).get('code') or entry['code']
            code = report['failure']['code']
            # Closed code for job evidence; the private log keeps the real class.
            report['failure']['jarvis_code'] = code if code in FAILURES else execution_error_code(exc)
        return self._finish(report)

    def _finish(self, report):
        report.update(ended_at=timestamp(), agent_failures=self.failures.entries(report['run_id']))
        self._append('runs.jsonl', {k: v for k, v in report.items() if k != 'diff'})
        return report


def _revision(task, feedback):
    if feedback is None:
        return task
    return (f'{task}\n\nYour previous attempt is still in the worktree (committed if its tests passed). '
            f'Revise it to address this feedback:\n{feedback[:6000]}')


def _commit(service, agent, attempt):
    """Commit the tested change to the candidate's private task branch; hooks never run."""
    # Changed since base, plus anything a revision reverted since the previous commit.
    since_head = service._workgit('diff', '--name-only', '-z', 'HEAD').split('\0')
    files = sorted(set(service.record['files_changed']) | {name for name in since_head if name})
    service._workgit('--literal-pathspecs', 'add', '-A', '--', *files)
    if not service._workgit('diff', '--cached', '--name-only'):
        raise ToolDenied('Revision made no changes to commit')
    identity = ['-c', f'user.name={agent}', '-c', f'user.email={agent}@jarvis.invalid', '-c', 'commit.gpgsign=false']
    service._workgit(*identity, 'commit', '--no-verify', '-q', '-m',
                     f'{agent}: direct-dev attempt {attempt}\n\nCandidate {service.record["id"]}')
    commit = service._workgit('rev-parse', 'HEAD').strip()
    service.record['commit'] = commit
    service.store.save(service.record, 'direct_commit')
    return commit


def _implementation(value):
    if not isinstance(value, dict) or not isinstance(value.get('summary'), str):
        raise ValueError('Invalid implementation result')
    return value


def _review(value):
    if not isinstance(value, dict) or value.get('verdict') not in {'passed', 'needs_revision', 'rejected'}:
        raise ValueError('Invalid review result')
    return value


# --- Engineering worker integration -------------------------------------------------

# Decision D1 (2026-10-16): the whole direct run, including routing probes, must end
# inside one job lease, or claim() recovers the job and runs it a second time.
DIRECT_BUDGET_SECONDS = 900
LEASE_MARGIN_SECONDS = 20  # record mapping, finish() and slack after the run
AGENT_FOR_PROVIDER = {'codex': 'codex_cli', 'codex_cli': 'codex_cli',
                      'claude_code': 'claude_cli', 'claude_cli': 'claude_cli'}
# Production (f95f459) knows 18 failure codes; dev-only codes project as worker_failed.
PRODUCTION_FAILURE_CODES = frozenset(FAILURES) - {'sandbox_integrity_failed', 'baseline_timeout', 'baseline_failed'}
JOB_STAGES = {'prepare': 'planning', 'implement': 'implementing', 'test': 'testing', 'commit': 'testing',
              'review': 'reviewing'}
FALLBACK_REASONS = {'not_authenticated': 'provider_authentication', 'quota_blocked': 'provider_quota',
                    'invocation_failed': 'provider_invocation_failed',
                    'reviewer_must_differ_from_author': 'reviewer_independence',
                    'capability_mismatch': 'capability_mismatch'}


def is_direct_candidate(record):
    return bool((record or {}).get('source_context', {}).get('direct_dev_run'))


def _state_dir(service):
    return service.root.parent / 'direct-dev'


def direct_route(service, job):
    """Choose the direct path only when every condition holds; anything else stays legacy."""
    from .engineering_portfolio import Portfolio
    reason = None
    if job.get('operator'):
        reason = 'operator_job'
    elif (job.get('action') or {}).get('scope') != 'code':
        reason = 'not_code_scope'
    elif job.get('provider') not in AGENT_FOR_PROVIDER:
        reason = 'provider_not_subscription_cli'  # codex_api/openai keep the legacy spend gate
    else:
        project = next((p for p in Portfolio(service.store.database).projects()
                        if p['project_id'] == job.get('project_id')), None)
        if not project or not project.get('repository'):
            reason = 'project_without_repository'
        elif project.get('read_only'):
            reason = 'project_read_only'
        elif job.get('candidate_id') and not is_direct_candidate(
                service.store.get(job['candidate_id'], internal=True, recover=False)):
            reason = 'legacy_candidate_resume'
    decision = dict(at=timestamp(), job_id=job['id'], attempt=job['attempts'], provider=job.get('provider'),
                    path='legacy' if reason else 'direct', reason=reason or 'eligible')
    state = _state_dir(service)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(state / 'path-routing.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        handle.write(json.dumps(decision) + '\n')
    return decision


def direct_budget(job, now=None):
    remaining = job['lease_until'] - (time.time() if now is None else now) - LEASE_MARGIN_SECONDS
    return min(DIRECT_BUDGET_SECONDS, remaining)


def run_direct(service, jobs, job, owner, *, agents=None):
    """Run a claimed job on the direct path; return (candidate, failure) for jobs.finish()."""
    from .candidate_provider import failure_evidence
    budget = direct_budget(job)
    author_role = AGENT_FOR_PROVIDER[job['provider']]
    roles = dict(implementing=author_role, reviewing=_other(author_role))
    if budget <= 0:
        return None, failure_evidence(author_role, 'candidate_deadline', 'planning', job['attempts'])
    arguments = job['arguments']
    task = (f"{job['goal']}\n\nProblem detected: {arguments['problem_detected']}\n"
            f"Hypothesis: {arguments['hypothesis']}")
    context = dict(job['source'], source='engineering_job', engineering_job_id=job['id'], lease_owner=owner,
                   attempt=job['attempts'], decision_id=(job.get('decision') or {}).get('id'))
    report = DirectDevelopment(service.store.database, candidate_root=service.root, state_dir=_state_dir(service),
                               agents=agents, roles=roles, tester=service.tester).run(
        job['project_id'], task, arguments['inspect_paths'], arguments['targeted_tests'], timeout=budget,
        source_context=context, on_stage=lambda stage: jobs.advance(job['id'], owner, stage))
    if not report.get('candidate_id'):
        failure = report.get('failure') or {}
        return None, failure_evidence(author_role, _production_code(failure.get('jarvis_code')),
                                      JOB_STAGES.get(failure.get('stage'), 'planning'), job['attempts'])
    record = service.store.get(report['candidate_id'], internal=True)
    record_outcome(record, report, job, roles)
    service.store.save(record, 'direct_dev_finished')
    return service.store.get(record['id']), record.get('failure')


def record_outcome(record, report, job, roles):
    """Express a direct run in the candidate fields that finish() and both projections read."""
    from .candidate_provider import failure_evidence
    from tools.candidate_edit import digest
    attempt = job['attempts']
    author = report.get('author') or roles['implementing']
    reviewer = report.get('reviewer') or (roles['reviewing'] if roles['reviewing'] != author else _other(author))
    decisions = {d['stage']: d for d in report.get('routing', [])}
    route = dict(provider_requested=job['provider'], adapter_type='cli', billing_class='subscription_cli')
    results = []
    for item in report.get('attempts', []):
        if 'implementation' in item:
            results.append(dict(route, provider=item['author'], stage='implementing', attempt=attempt,
                                iteration=item['attempt'], status='applied', execution_origin='provider',
                                response_sha256=digest(item['implementation']['summary'].encode()),
                                fallback_reason=_fallback(decisions.get('implementing'))))
        if item.get('review'):
            results.append(dict(route, provider=item['reviewer'], stage='reviewing', attempt=attempt,
                                iteration=item['attempt'], status='received', execution_origin='provider',
                                response_sha256=digest(item['review']['summary'].encode()),
                                fallback_reason=_fallback(decisions.get('reviewing'))))
    record['provider_results'] = results
    record['provider_route'] = dict(route, provider_used=report.get('author'),
                                    fallback_reason=_fallback(decisions.get('implementing')))
    record['improvement'] = dict(record.get('improvement') or {}, implementation_provider=author,
        review_provider=reviewer, scope='code', engineering_action='engineer',
        baseline_comparison=dict(result='not_run'),  # decision D2: no baseline comparison yet
        failures_revisions=[dict(attempt=item['attempt'],
                                 state='needs_revision' if item['outcome'] == 'tests_failed' else item['outcome'])
                            for item in report.get('attempts', []) if item.get('outcome')])
    record['stage'] = JOB_STAGES.get(report.get('stage'), 'planning')
    status = report['status']
    review = record.get('reviewer_result') or {}
    if (status == 'passed' and record.get('tested_fingerprint') == record.get('fingerprint')
            and review.get('verdict') == 'passed' and review.get('fingerprint') == record['fingerprint']):
        record.update(state='passed', reviewed_at=timestamp(), reviewed_fingerprint=record['fingerprint'],
                      reviewed_diff_hash=digest(record['diff'].encode()), failure=None, error=None,
                      provider_blocker=None)
        return
    if status in {'rejected', 'needs_revision', 'passed'}:
        code, stage, provider = 'review_failed', 'reviewing', reviewer
    elif status == 'tests_failed':
        code, stage, provider = 'tests_failed', 'testing', author
    else:
        failure = report.get('failure') or {}
        code, stage = _production_code(failure.get('jarvis_code')), JOB_STAGES.get(failure.get('stage'), 'planning')
        provider = reviewer if stage == 'reviewing' else author
        record['provider_blocker'] = failure.get('blocker')
        record['error'] = 'Direct development did not complete.'
    record.update(state='rejected' if status == 'rejected' else 'needs_revision',
                  failure=failure_evidence(provider, code, stage, attempt))


def _production_code(code):
    return code if code in PRODUCTION_FAILURE_CODES else 'worker_failed'


def _fallback(decision):
    if not decision or decision['reason'] == 'default_usable':
        return None
    reason = decision['reason'].removeprefix('default_').removeprefix('probe_').removeprefix('recent_')
    return FALLBACK_REASONS.get(reason, 'provider_unavailable')


def _other(agent):
    return 'claude_cli' if agent == 'codex_cli' else 'codex_cli'
