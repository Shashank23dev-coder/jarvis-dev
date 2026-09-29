"""Durable, bounded engineering queue. No execution or approval authority here."""
import json
import time
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
        from .candidate_tools import EngineeringArguments
        from tools.candidate_edit import CandidatePolicy, safe_text
        text_value(goal)
        action = EngineeringAction.model_validate(action).check(goal, context)
        if not all(valid_id(context.get(key)) for key in ('task_id', 'conversation_id')):
            raise ToolDenied('Invalid job source')
        args = EngineeringArguments.model_validate(arguments).model_dump()
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
        with self.database.transaction() as db:
            row = db.execute('SELECT record FROM engineering_jobs WHERE task_id=?', (action.task_id,)).fetchone()
            if row:
                job = json.loads(row[0])
                if any(job[key] != value for key, value in
                       [('goal', goal), ('action', action.model_dump()), ('arguments', args), ('source', source)]):
                    raise ToolDenied('Engineering job source mismatch')
                return self.get(job['id'])
            job = dict(id=str(uuid4()), type='engineering', goal=goal, source=source,
                status='queued', provider='codex', created_at=timestamp(), started_at=None,
                completed_at=None, current_stage='queued', attempts=0, candidate_id=None,
                candidate_ids=[], test_result=[], review_result=None, error=None,
                block_reason=None, control=None, lease_owner=None, lease_until=0,
                action=action.model_dump(), arguments=args,
                improvement=ImprovementMetadata(observed_problem=args['problem_detected'],
                    hypothesis=args['hypothesis'], proposed_change=goal).model_dump())
            db.execute('INSERT INTO engineering_jobs VALUES (?,?,?,?)',
                       (job['id'], action.task_id, job['created_at'], json.dumps(job)))
            self._save(db, job, 'created')
        return self.get(job['id'])

    def get(self, identifier):
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            job['history'] = [dict(row) for row in db.execute(
                'SELECT sequence,timestamp,event,status FROM engineering_job_events WHERE job_id=? ORDER BY sequence',
                (identifier,))]
            return job

    def recent(self, conversation_id=None):
        with self.database.transaction() as db:
            rows = db.execute('''SELECT id FROM engineering_jobs
                WHERE (? IS NULL OR json_extract(record, '$.source.conversation_id') = ?)
                ORDER BY created_at DESC LIMIT 10''', (conversation_id, conversation_id)).fetchall()
            return [j for row in rows if (j := self.get(row[0])) and
                    (conversation_id is None or j['source']['conversation_id'] == conversation_id)]

    def control(self, identifier, operation):
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            if job['lease_owner'] or job['status'] in ACTIVE:
                raise ToolDenied('Active job cannot be controlled safely')
            if operation in {'pause', 'cancel'} and job['status'] in {'queued', 'blocked', 'failed'}:
                job.update(status='blocked', control=operation, block_reason=operation)
            elif (operation == 'resume' and job['status'] in {'blocked', 'failed'}
                  and job['control'] != 'cancel' and job['attempts'] < MAX_ATTEMPTS):
                job.update(status='queued', control=None, block_reason=None, error=None, completed_at=None)
            else:
                raise ToolDenied('Engineering job transition denied')
            job['current_stage'] = job['status']
            self._save(db, job, operation)
        return self.get(identifier)

    def claim(self, owner, *, now=None):
        """Caller must hold the worker flock, including during lease recovery."""
        now = time.time() if now is None else now
        with self.database.transaction() as db:
            for row in db.execute('SELECT record FROM engineering_jobs ORDER BY created_at').fetchall():
                job = json.loads(row[0])
                if job['status'] in ACTIVE and job['lease_until'] < now:
                    job.update(status='queued', lease_owner=None, lease_until=0)
                    self._save(db, job, 'lease_recovered')
                if job['status'] != 'queued':
                    continue
                if job['attempts'] >= MAX_ATTEMPTS:
                    job.update(status='failed', error='Retry limit reached', completed_at=timestamp())
                    self._save(db, job, 'retry_limit')
                    continue
                job.update(status='planning', current_stage='planning', attempts=job['attempts'] + 1,
                           lease_owner=owner, lease_until=now + 330,
                           started_at=job['started_at'] or timestamp())
                self._save(db, job, 'claimed')
                return job
        return None

    def finish(self, identifier, owner, candidate=None):
        with self.database.transaction() as db:
            job = self._get(db, identifier)
            if job['lease_owner'] != owner:
                raise ToolDenied('Stale engineering lease')
            passed = bool(candidate and candidate['state'] == 'passed' and not candidate.get('busy')
                          and candidate.get('tested_fingerprint') == candidate.get('reviewed_fingerprint')
                          and (candidate.get('reviewer_result') or {}).get('verdict') == 'passed')
            status = 'waiting_for_approval' if passed else ('failed' if job['attempts'] >= MAX_ATTEMPTS else 'blocked')
            job.update(status=status, current_stage=status, lease_owner=None, lease_until=0,
                       error=None if passed else 'Candidate execution did not pass tests and review',
                       block_reason=None if passed else 'Explicit retry required',
                       completed_at=timestamp() if status == 'failed' else None)
            if candidate:
                improvement = candidate.get('improvement', {})
                job.update(test_result=candidate['test_results'], review_result=candidate['reviewer_result'])
                job['improvement'].update(baseline_evidence=improvement.get('baseline_comparison'),
                    implementation_attempts=improvement.get('failures_revisions', []),
                    test_evidence=candidate['test_results'], review_evidence=candidate['reviewer_result'],
                    candidate_result=candidate['state'])
            self._save(db, job, 'execution_finished')
        return self.get(identifier)


def project_job(job):
    """Closed production projection: no paths, model prose, logs or host config."""
    if not valid_id(job.get('id')) or job.get('status') not in STATES or job.get('provider') != 'codex':
        raise ToolDenied('Invalid engineering job result')
    return {key: job.get(key) for key in ('id', 'type', 'status', 'provider', 'created_at',
        'started_at', 'completed_at', 'current_stage', 'attempts', 'candidate_id', 'control')} | {
        'error': 'Engineering job requires attention' if job.get('error') else None,
        'history': [{key: event[key] for key in ('sequence', 'timestamp', 'status')}
                    for event in job.get('history', [])[-16:]],
        'test_result': [{key: item.get(key) for key in ('phase', 'returncode', 'timed_out', 'truncated')}
                        for item in job.get('test_result', [])[-3:]],
        'review_result': {'verdict': (job.get('review_result') or {}).get('verdict')}}
