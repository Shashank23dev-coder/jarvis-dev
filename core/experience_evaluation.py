"""Host-only reuse accounting. Associations are evidence, never causal proof."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from .database import timestamp, valid_id
from .recall import as_utc, encoded, safe_record


def confidence(helpful, misleading):
    # Six prior observations soften any single result. Neutral/retrievals add no votes.
    return max(0.05, min(0.95, (3 + helpful) / (6 + helpful + 1.5 * misleading)))


def family_key(goal, kind):
    from .experience import topic_terms
    return hashlib.sha256(encoded([kind, sorted(topic_terms(goal))]).encode()).hexdigest()


class ExperienceEvaluation:
    def __init__(self, database, *, now=None):
        self.database = database
        self.now = now

    def quality(self, identifier):
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM experience_quality WHERE experience_id=?', (identifier,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.pop('experience_id')
        return result

    def effective(self, quality, created_at):
        age = max(0, ((self.now or datetime.now(timezone.utc)) -
                      as_utc(quality['last_signal_at'] or created_at)).total_seconds() / 86400)
        # Smooth 180-day half-life, with a floor. Reading/retrieving never refreshes this.
        return 0.05 + (quality['confidence_score'] - 0.05) / (1 + age / 180)

    def track(self, task_id, results):
        """Called only when lessons are about to be sent to the model, once per task."""
        from .experience import task_type
        with self.database.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not task or task['status'] != 'running' or not task_type(task['goal']) or not safe_record(task['goal']):
                return
            for result in results[:5]:
                row = db.execute('SELECT record FROM experiences WHERE id=?', (result['id'],)).fetchone()
                if row is None:
                    continue
                lesson = json.loads(row['record'])
                if (not safe_record(lesson['goal']) or lesson['source_task_id'] == task_id or
                        as_utc(lesson['created_at']) > as_utc(task['created_at'])):
                    continue
                quality = self.quality(lesson['id'])
                # Freeze the evidence actually injected, not a later revised candidate lesson.
                snapshot = {key: result[key] for key in ('task_type', 'outcome', 'evidence')}
                snapshot['family'] = family_key(task['goal'], task_type(task['goal']))
                now = timestamp()
                inserted = db.execute('INSERT OR IGNORE INTO experience_uses VALUES (?,?,?,?,?,0)',
                    (lesson['id'], task_id, quality['generation'], now, encoded(snapshot))).rowcount
                if inserted:
                    db.execute('UPDATE experience_quality SET retrieval_count=retrieval_count+1,last_used_at=? '
                               'WHERE experience_id=?', (now, lesson['id']))

    def _append(self, identifier, generation, source, verdict, evidence, *, task_id=None, event_id=None):
        with self.database.transaction() as db:
            now = timestamp()
            db.execute('INSERT INTO experience_evaluations '
                       '(id,experience_id,task_id,generation,source,verdict,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)',
                       (event_id or str(uuid4()), identifier, task_id, generation, source, verdict, encoded(evidence), now))
            if verdict == 'reset':
                return
            column = {'helpful': 'helpful_count', 'neutral': 'neutral_count', 'misleading': 'misleading_count'}[verdict]
            db.execute(f'UPDATE experience_quality SET {column}={column}+1,last_evaluated_at=? WHERE experience_id=?',
                       (now, identifier))
            if verdict != 'neutral':
                db.execute('UPDATE experience_quality SET last_signal_at=? WHERE experience_id=?', (now, identifier))
            quality = self.quality(identifier)
            db.execute('UPDATE experience_quality SET confidence_score=? WHERE experience_id=?',
                       (confidence(quality['helpful_count'], quality['misleading_count']), identifier))

    def _assess(self, task, snapshot, observations, used_at):
        evidence = {'task_status': task['status'], 'reason': 'no_matching_verification'}
        # A failure warning is not a recommendation to reproduce that failure.
        if snapshot['outcome'] not in {'succeeded', 'recovered'}:
            return 'neutral', dict(evidence, reason='historical_caution_or_unverified')
        prior = {(o['tool'], o.get('check')) for o in snapshot['evidence'].get('operations', [])
                 if o.get('test_check') and o.get('check')}
        latest = {}
        for item in observations:
            identity = (item['tool'], item.get('check'))
            if item.get('test_check') and identity in prior:
                latest[identity] = item
        if latest:
            failed = sum(not item['ok'] for item in latest.values())
            evidence.update(reason='matching_check_outcomes', matched_checks=len(latest), failed_checks=failed)
            if failed:
                return 'misleading', evidence
            final_operations = {(o['tool'], o.get('check')): o for o in observations}
            if (task['status'] == 'completed' and prior <= latest.keys() and
                    all(o['ok'] for o in final_operations.values())):
                return 'helpful', evidence
            return 'neutral', evidence
        kind = snapshot['task_type']
        operations = ({'approve_and_apply_candidate'} if kind == 'candidate_apply' else
                      {'create_self_edit_candidate', 'review_self_edit_candidate', 'rerun_candidate_tests'} if kind == 'self_edit' else set())
        candidates = {o.get('candidate_id') for o in observations if o['tool'] in operations and o.get('candidate_id')}
        outcomes = []
        gates = []
        for candidate_id in candidates:
            phase = 'apply' if kind == 'candidate_apply' else 'review'
            with self.database.transaction() as db:
                row = db.execute('SELECT record FROM experiences WHERE source_key=?',
                                 (f'candidate:{candidate_id}:{phase}',)).fetchone()
            if row:
                record = json.loads(row['record'])
                if as_utc(record['created_at']) >= as_utc(used_at) and safe_record(record['goal']):
                    outcomes.append(record['outcome'])
                    gate = record['evidence']
                    gates.append({'outcome': record['outcome'],
                                  'rollback_restored': bool(gate.get('rollback_restored')),
                                  'verdict': gate.get('verdict') if gate.get('verdict') in
                                  {'passed', 'needs_revision', 'rejected', 'applied', 'denied',
                                   'rolled_back', 'rollback_failed'} else 'unknown'})
        if outcomes:
            evidence.update(reason='host_candidate_gates', workflows=len(outcomes),
                            failed_workflows=outcomes.count('failed'), gates=gates[-8:])
            if 'failed' in outcomes:
                return 'misleading', evidence
            if task['status'] == 'completed' and all(o in {'succeeded', 'recovered'} for o in outcomes):
                return 'helpful', evidence
        return 'neutral', evidence

    def evaluate(self, task_id, observations):
        from .experience import task_type
        with self.database.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if (not task or task['status'] not in {'completed', 'failed', 'limit_reached'} or
                    not task_type(task['goal']) or not safe_record(task['goal'])):
                return
            uses = db.execute('SELECT u.*,e.record FROM experience_uses u JOIN experiences e ON e.id=u.experience_id '
                              'WHERE u.task_id=? AND u.evaluated=0', (task_id,)).fetchall()
            for usage in uses:
                quality = self.quality(usage['experience_id'])
                if quality['generation'] != usage['generation'] or not safe_record(json.loads(usage['record'])['goal']):
                    continue
                snapshot = json.loads(usage['snapshot'])
                verdict, evidence = self._assess(task, snapshot, observations, usage['used_at'])
                evidence['family'] = snapshot['family']
                self._append(usage['experience_id'], usage['generation'], 'automatic', verdict, evidence, task_id=task_id)
                db.execute('UPDATE experience_uses SET evaluated=1 WHERE experience_id=? AND task_id=?',
                           (usage['experience_id'], task_id))

    def feedback(self, identifier, verdict, request_id):
        """Explicit owner testimony; no free text or model-authored rating accepted."""
        if verdict not in {'helpful', 'misleading'} or not valid_id(request_id):
            raise ValueError('Invalid feedback')
        with self.database.transaction() as db:
            quality = self.quality(identifier)
            if quality is None:
                raise ValueError('Unknown experience')
            previous = db.execute('SELECT * FROM experience_evaluations WHERE id=?', (request_id,)).fetchone()
            if previous:
                if previous['experience_id'] != identifier or previous['verdict'] != verdict or previous['source'] != 'owner':
                    raise ValueError('Feedback request conflict')
                return quality
            self._append(identifier, quality['generation'], 'owner', verdict,
                         {'reason': 'explicit_owner_feedback', 'causality_verified': False}, event_id=request_id)
            return self.quality(identifier)

    def reset(self, identifier):
        with self.database.transaction() as db:
            quality = self.quality(identifier)
            if quality is None:
                raise ValueError('Unknown experience')
            generation = quality['generation'] + 1
            db.execute('UPDATE experience_quality SET retrieval_count=0,helpful_count=0,neutral_count=0,'
                       'misleading_count=0,confidence_score=0.5,last_used_at=NULL,last_evaluated_at=NULL,last_signal_at=NULL,generation=? '
                       'WHERE experience_id=?', (generation, identifier))
            self._append(identifier, generation, 'reset', 'reset', {'reason': 'explicit_owner_reset'})
            return self.quality(identifier)

    def history(self, identifier, *, limit=20, before=None):
        if type(limit) is not int or not 1 <= limit <= 50 or before is not None and (type(before) is not int or before < 1):
            raise ValueError('Invalid history bounds')
        with self.database.transaction() as db:
            rows = db.execute('SELECT * FROM experience_evaluations WHERE experience_id=? AND (? IS NULL OR sequence<?) '
                              'ORDER BY sequence DESC LIMIT ?', (identifier, before, before, limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            item['evidence'] = json.loads(item['evidence'])
        return {'evaluations': items, 'next_before': items[-1]['sequence'] if len(rows) > limit else None}

    def similar_help(self, identifier, family):
        # Bounded recent evidence, only from this confidence generation and topic/type family.
        with self.database.transaction() as db:
            rows = db.execute('SELECT verdict,evidence FROM experience_evaluations WHERE experience_id=? '
                'AND generation=(SELECT generation FROM experience_quality WHERE experience_id=?) '
                "AND source='automatic' ORDER BY sequence DESC LIMIT 100", (identifier, identifier)).fetchall()
        votes = [row['verdict'] for row in rows if json.loads(row['evidence']).get('family') == family]
        helpful, harmful = votes.count('helpful'), votes.count('misleading')
        return (helpful - harmful) / (4 + helpful + harmful)
