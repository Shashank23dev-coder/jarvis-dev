"""SQLite task/activity storage with explicit writes and compatible task records."""

import json
from uuid import uuid4

from .database import Database, text_value, timestamp, valid_id
from .memory import MemoryStore
from .experience import ExperienceStore


class TaskStore:
    def __init__(self, path=':memory:'):
        self.database = Database(path)
        self.memory = MemoryStore(self)
        self.experiences = ExperienceStore(self.database)
        # Preserve references returned to existing Python callers; SQLite remains
        # authoritative. Mutations must go through update_task, not these views.
        self.tasks = {}

    timestamp = staticmethod(timestamp)

    def close(self):
        self.database.close()
        self.tasks.clear()

    def create_task(self, goal: str, conversation_id=None):
        text_value(goal)
        if conversation_id is not None and self.memory.get_conversation(conversation_id) is None:
            raise ValueError('Unknown conversation')
        task_id, now = str(uuid4()), self.timestamp()
        with self.database.transaction() as db:
            if conversation_id is None:
                conversation_id = str(uuid4())
                db.execute('INSERT INTO conversations VALUES (?,?)', (conversation_id, now))
            db.execute('INSERT INTO tasks(id,conversation_id,goal,status,created_at) VALUES (?,?,?,?,?)',
                       (task_id, conversation_id, goal, 'created', now))
            db.execute('INSERT INTO messages(id,conversation_id,task_id,role,content,created_at) VALUES (?,?,?,?,?,?)',
                       (str(uuid4()), conversation_id, task_id, 'user', goal, now))
            event = dict(type='task_created', message='Task created', timestamp=self.timestamp())
            db.execute('INSERT INTO activity(task_id,event) VALUES (?,?)', (task_id, json.dumps(event)))
        return self.get_task(task_id)

    def get_task(self, task_id: str):
        if not valid_id(task_id):
            return None
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            task['steps'] = json.loads(task['steps'])
            if task['agent'] is None:
                del task['agent']
            else:
                task['agent'] = json.loads(task['agent'])
            if task['response'] is None:
                del task['response']
            task['activity'] = [json.loads(event[0]) for event in db.execute(
                'SELECT event FROM activity WHERE task_id=? ORDER BY sequence', (task_id,))]
            cached = self.tasks.setdefault(task_id, {})
            cached.clear()
            cached.update(task)
            return cached

    def list_tasks(self):
        with self.database.transaction() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM tasks ORDER BY rowid')]
            return [self.get_task(task_id) for task_id in ids]

    def update_task(self, task_id, **changes):
        if not changes or set(changes) - {'status', 'steps', 'response', 'agent'}:
            raise ValueError('Invalid task fields')
        for key, value in changes.items():
            if key in {'status', 'response'}:
                text_value(value)
            elif key == 'steps' and (not isinstance(value, list) or any(not isinstance(step, dict) for step in value)):
                raise ValueError('Invalid steps')
            elif key == 'agent' and not isinstance(value, dict):
                raise ValueError('Invalid agent metadata')
        values = {key: json.dumps(value, allow_nan=False) if key in {'steps', 'agent'} else value
                  for key, value in changes.items()}
        with self.database.transaction() as db:
            task = self.get_task(task_id)
            if task is None:
                raise ValueError('Unknown task')
            db.execute('UPDATE tasks SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?',
                       (*values.values(), task_id))
            if 'response' in changes:
                self.add_assistant_message(task_id, changes['response'])
        return self.get_task(task_id)

    def add_assistant_message(self, task_id, content):
        text_value(content)
        with self.database.transaction() as db:
            task = self.get_task(task_id)
            if task is None:
                raise ValueError('Unknown task')
            db.execute('''INSERT INTO messages(id,conversation_id,task_id,role,content,created_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(task_id,role) DO UPDATE SET content=excluded.content''',
                (str(uuid4()), task['conversation_id'], task_id, 'assistant', content, self.timestamp()))

    def _append_activity(self, task_id, event):
        with self.database.transaction() as db:
            if self.get_task(task_id) is None:
                raise ValueError('Unknown task')
            db.execute('INSERT INTO activity(task_id,event) VALUES (?,?)',
                       (task_id, json.dumps(event, allow_nan=False)))
        return self.get_task(task_id)

    def add_activity(self, task_id: str, message: str, activity_type: str = 'info'):
        text_value(message)
        text_value(activity_type, 100)
        if self.get_task(task_id) is None:
            return None
        return self._append_activity(task_id, dict(type=activity_type, message=message, timestamp=self.timestamp()))

    def add_tool_activity(self, task_id: str, *, invocation_id: str, tool_name: str,
                          status: str, started_at: str, duration_ms: float,
                          summary: str, error_code: str | None = None):
        """Append host-generated metadata only; never raw arguments or outputs."""
        import math
        from datetime import datetime
        if not valid_id(invocation_id) or status not in {'started', 'completed', 'failed'}:
            raise ValueError('Invalid tool event')
        if type(duration_ms) not in (int, float) or not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError('Invalid duration')
        datetime.fromisoformat(started_at)
        text_value(tool_name, 100)
        text_value(summary)
        if error_code is not None:
            text_value(error_code, 100)
        now = self.timestamp()
        return self._append_activity(task_id, {
            'type': 'tool_' + status, 'message': f'{tool_name}: {summary}',
            'timestamp': now, 'task_id': task_id, 'invocation_id': invocation_id,
            'tool_name': tool_name, 'status': status, 'started_at': started_at,
            'completed_at': now if status != 'started' else None,
            'duration_ms': duration_ms, 'summary': summary, 'error_code': error_code,
        })

    def set_status(self, task_id: str, status: str):
        if self.get_task(task_id) is None:
            return None
        return self.update_task(task_id, status=status)

    def add_brain_runtime(self, task_id, *, enabled, selected_provider):
        """Record effective task-local settings, never raw environment values."""
        from models.provider import PROVIDER_NAMES
        if (type(enabled) is not bool or selected_provider not in PROVIDER_NAMES | {'legacy', None}
                or (not enabled and selected_provider != 'legacy')
                or (enabled and selected_provider == 'legacy')):
            raise ValueError('Invalid Brain runtime metadata')
        return self._append_activity(task_id, {
            'type': 'brain_runtime', 'task_id': task_id, 'timestamp': self.timestamp(),
            'message': 'JarvisBrain runtime settings recorded.',
            'enabled': enabled, 'selected_provider': selected_provider,
        })

    def add_engineering_action(self, task_id, action):
        from .candidate_action import EngineeringAction
        action = EngineeringAction.model_validate(action)
        task = self.get_task(task_id)
        if task is None:
            raise ValueError('Unknown task')
        action.check(task['goal'], {'task_id': task_id, 'conversation_id': task['conversation_id']})
        return self._append_activity(task_id, {
            'type': 'engineering_action', 'task_id': task_id, 'timestamp': self.timestamp(),
            'message': 'Bounded engineering action selected.',
            'action': action.action, 'provider': action.provider, 'scope': action.scope,
            'workspace': action.workspace, 'stop_after': action.stop_after,
        })

    def add_routing_activity(self, task_id, *, provider, reason, iteration, status):
        """Closed metadata schema: never persist prompts, outputs or reasoning."""
        from models.provider import PROVIDER_NAMES
        if (provider not in PROVIDER_NAMES | {'legacy'}
                or reason not in {'configured_provider', 'provider_unavailable',
                                  'provider_error', 'provider_incomplete', 'legacy_continuation', 'brain_disabled'}
                or type(iteration) is not int or not 1 <= iteration <= 64
                or status not in {'started', 'completed', 'failed', 'incomplete'}):
            raise ValueError('Invalid routing metadata')
        return self._append_activity(task_id, {
            'type': 'brain_route', 'message': 'JarvisBrain provider turn ' + status + '.',
            'timestamp': self.timestamp(), 'task_id': task_id, 'provider': provider,
            'reason': reason, 'iteration': iteration, 'status': status,
        })
