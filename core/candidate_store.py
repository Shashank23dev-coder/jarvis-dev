"""Durable candidate state and operation leases; no raw model reasoning."""

import json
import time
from uuid import uuid4
from .database import timestamp, valid_id


class CandidateStore:
    def __init__(self, database):
        self.database = database

    def create(self, goal, paths, tests):
        record = dict(id=str(uuid4()), created_at=timestamp(), state='created', goal=goal,
                      inspect_paths=paths, targeted_tests=tests, branch=None, worktree=None,
                      base_commit=None, files_changed=[], diff='', diff_summary={},
                      test_results=[], reviewer_result=None, iterations=0, tool_calls=0,
                      elapsed_seconds=0, error=None, manifest=None, fingerprint=None,
                      tested_fingerprint=None)
        with self.database.transaction() as db:
            db.execute('INSERT INTO candidates(id,created_at,state,record) VALUES (?,?,?,?)',
                       (record['id'], record['created_at'], record['state'], json.dumps(record)))
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
                record.update(state='rejected', error='Previous worker was interrupted.',
                              tested_fingerprint=None, reviewer_result=None)
                db.execute('UPDATE candidates SET busy=0,state=?,record=? WHERE id=?',
                           (record['state'], json.dumps(record), candidate_id))
                row = {'busy': 0}
            record['busy'] = bool(row['busy'])
            if not internal:
                record.pop('manifest', None)
                record.pop('lease_until', None)
            record['events'] = [dict(row) for row in db.execute(
                'SELECT timestamp,event FROM candidate_events WHERE candidate_id=? ORDER BY sequence', (candidate_id,))]
            return record

    def save(self, record, event=None):
        data = {key: value for key, value in record.items() if key not in {'busy', 'events'}}
        with self.database.transaction() as db:
            db.execute('UPDATE candidates SET state=?,record=? WHERE id=?',
                       (record['state'], json.dumps(data, allow_nan=False), record['id']))
            if event:
                db.execute('INSERT INTO candidate_events(candidate_id,timestamp,event) VALUES (?,?,?)',
                           (record['id'], timestamp(), event))

        # Reflection is derived after the authoritative state/event commits.
        from .experience import CANDIDATE_EVENTS, ExperienceStore, attempt_capture
        if event in CANDIDATE_EVENTS:
            attempt_capture(lambda: ExperienceStore(self.database).capture_candidate(data, event))

    def claim(self, candidate_id):
        with self.database.transaction() as db:
            row = db.execute('SELECT record,busy FROM candidates WHERE id=?', (candidate_id,)).fetchone()
            if row and row['busy']:
                record = json.loads(row['record'])
                if record.get('lease_until', float('inf')) < time.time():
                    record.update(state='rejected', error='Previous worker was interrupted.',
                                  tested_fingerprint=None, reviewer_result=None)
                    db.execute('UPDATE candidates SET busy=0,state=?,record=? WHERE id=?',
                               (record['state'], json.dumps(record), candidate_id))
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
