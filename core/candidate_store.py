"""Durable candidate state and operation leases; no raw model reasoning."""

import json
import time
from uuid import uuid4
from .database import timestamp, valid_id


class CandidateStore:
    def __init__(self, database):
        self.database = database

    def create(self, goal, paths, tests, *, source_context=None):
        record = dict(id=str(uuid4()), created_at=timestamp(), state='created', goal=goal,
                      inspect_paths=paths, targeted_tests=tests, branch=None, worktree=None,
                      base_commit=None, files_changed=[], diff='', diff_summary={},
                      test_results=[], reviewer_result=None, iterations=0, tool_calls=0,
                      elapsed_seconds=0, error=None, manifest=None, fingerprint=None,
                      tested_fingerprint=None)
        if source_context is not None:
            record['source_context'] = source_context
        with self.database.transaction() as db:
            db.execute('INSERT INTO candidates(id,created_at,state,record) VALUES (?,?,?,?)',
                       (record['id'], record['created_at'], record['state'], json.dumps(record)))
            job_id = (source_context or {}).get('engineering_job_id')
            if job_id:
                from .engineering_jobs import EngineeringJobs
                jobs = EngineeringJobs(self.database)
                job = jobs._get(db, job_id)
                if job['lease_owner'] != source_context.get('lease_owner'):
                    raise ValueError('Engineering lease mismatch')
                job['candidate_id'] = record['id']
                job['candidate_ids'].append(record['id'])
                jobs._save(db, job, 'candidate_created')
        return record

    def get(self, candidate_id, *, internal=False, recover=True):
        """Read a record; observational audit callers disable expired-lease recovery."""
        if not valid_id(candidate_id):
            return None
        with self.database.transaction() as db:
            row = db.execute('SELECT record,busy FROM candidates WHERE id=?', (candidate_id,)).fetchone()
            if row is None:
                return None
            record = json.loads(row['record'])
            if recover and row['busy'] and record.get('lease_until', float('inf')) < time.time():
                from .candidate_provider import failure_evidence
                # Older records may predate the durable failure-history field.
                self.save(record)
                record.update(state='rejected', error='Previous worker was interrupted.',
                              tested_fingerprint=None, reviewer_result=None,
                              failure=failure_evidence(
                                  record.get('improvement', {}).get('implementation_provider', 'openai'),
                                  'worker_failed', record.get('stage', 'planning'),
                                  record.get('source_context', {}).get('attempt', 1)))
                db.execute('UPDATE candidates SET busy=0 WHERE id=?', (candidate_id,))
                self.save(record, 'candidate_interrupted')
                row = {'busy': 0}
            record['busy'] = bool(row['busy'])
            if record.get('improvement'):
                record['improvement'].update(review_result=record['reviewer_result'],
                    confidence='supported_by_tests_and_review' if record['state'] == 'passed' and (internal or not record['busy']) else 'insufficient')
            if not internal:
                record.pop('manifest', None)
                record.pop('lease_until', None)
            record['events'] = [dict(row) for row in db.execute(
                'SELECT timestamp,event FROM candidate_events WHERE candidate_id=? ORDER BY sequence', (candidate_id,))]
            return record

    def save(self, record, event=None):
        if record.get('failure'):
            from .candidate_provider import FailureEvidence, project_failure
            normalized = project_failure(record['failure'])
            failure = {key: normalized[key] for key in FailureEvidence.model_fields}
            failures = record.setdefault('failures', [])
            if event in {'tests_failed', 'review_finished', 'candidate_failed', 'proposal_no_changes',
                         'worker_stopped', 'candidate_rejected', 'candidate_interrupted'} or failure not in failures:
                failures.append(failure)
        data = {key: value for key, value in record.items() if key not in {'busy', 'events'}}
        with self.database.transaction() as db:
            db.execute('UPDATE candidates SET state=?,record=? WHERE id=?',
                       (record['state'], json.dumps(data, allow_nan=False), record['id']))
            if event:
                db.execute('INSERT INTO candidate_events(candidate_id,timestamp,event) VALUES (?,?,?)',
                           (record['id'], timestamp(), event))
                job_id = record.get('source_context', {}).get('engineering_job_id')
                if job_id:
                    from .engineering_jobs import EngineeringJobs
                    jobs = EngineeringJobs(self.database)
                    job = jobs._get(db, job_id)
                    if job['lease_owner'] and job['lease_owner'] != record['source_context'].get('lease_owner'):
                        raise ValueError('Engineering lease mismatch')
                    stage = ('testing' if event.startswith(('test', 'baseline')) else
                             'reviewing' if event in {'review_finished', 'independent_review',
                                                     'review_requires_baseline_comparison'} else
                             'implementing' if event in {'generate_proposal', 'apply_edits'} else None)
                    if stage and job['lease_owner']:
                        job.update(status=stage, current_stage=stage)
                        jobs._save(db, job, event)

        # Reflection is derived after the authoritative state/event commits.
        from .experience import CANDIDATE_EVENTS, ExperienceStore, attempt_capture
        if event in CANDIDATE_EVENTS:
            attempt_capture(lambda: ExperienceStore(self.database).capture_candidate(data, event))

    def claim(self, candidate_id):
        with self.database.transaction() as db:
            self.get(candidate_id, internal=True)
            row = db.execute('SELECT record,busy FROM candidates WHERE id=?', (candidate_id,)).fetchone()
            if row:
                record = json.loads(db.execute('SELECT record FROM candidates WHERE id=?', (candidate_id,)).fetchone()[0])
                record['lease_until'] = time.time() + 301
                encoded = json.dumps(record)
            else:
                encoded = '{}'
            changed = db.execute('UPDATE candidates SET busy=1,record=? WHERE id=? AND busy=0',
                                 (encoded, candidate_id)).rowcount
            if changed != 1:
                raise ValueError('Candidate is busy or unavailable')

    def release(self, candidate_id):
        with self.database.transaction() as db:
            db.execute('UPDATE candidates SET busy=0 WHERE id=?', (candidate_id,))
