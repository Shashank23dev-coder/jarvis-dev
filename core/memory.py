"""Explicit memory categories and bounded, read-only recall interfaces."""

from uuid import uuid4
from .database import text_value, timestamp, valid_id


class MemoryStore:
    def __init__(self, task_store):
        self.task_store = task_store
        self.database = task_store.database

    def create_conversation(self):
        record = {'id': str(uuid4()), 'created_at': timestamp()}
        with self.database.transaction() as db:
            db.execute('INSERT INTO conversations VALUES (:id, :created_at)', record)
        return record

    def get_conversation(self, conversation_id):
        if not valid_id(conversation_id):
            return None
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
            return dict(row) if row else None

    def list_conversations(self):
        with self.database.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM conversations ORDER BY rowid')]

    def conversation_history(self, conversation_id, *, through_task=None):
        if self.get_conversation(conversation_id) is None:
            raise ValueError('Unknown conversation')
        with self.database.transaction() as db:
            cutoff = None
            if through_task is not None:
                row = db.execute("SELECT sequence FROM messages WHERE task_id=? AND role='user' AND conversation_id=?",
                                 (through_task, conversation_id)).fetchone()
                if row is None:
                    raise ValueError('Unknown conversation task')
                cutoff = row[0]
            rows = db.execute('SELECT * FROM messages WHERE conversation_id=? AND (? IS NULL OR sequence<=?) ORDER BY sequence',
                              (conversation_id, cutoff, cutoff))
            return [dict(row) for row in rows]

    def prompt_history(self, conversation_id, *, through_task, limit=40, max_bytes=64000):
        """Bound current-chat context without dropping/truncating the active request."""
        from .recall import encoded, safe_record
        if (type(limit) is not int or not 1 <= limit <= 100 or
                type(max_bytes) is not int or not 1024 <= max_bytes <= 128000):
            raise ValueError('Invalid current-chat context limits')
        with self.database.transaction() as db:
            row = db.execute("SELECT sequence FROM messages WHERE conversation_id=? AND task_id=? AND role='user'",
                             (conversation_id, through_task)).fetchone()
            if row is None:
                raise ValueError('Unknown conversation task')
            cutoff = row['sequence']
            rows = db.execute('SELECT * FROM messages WHERE conversation_id=? AND sequence<=? '
                              'ORDER BY sequence DESC LIMIT ?', (conversation_id, cutoff, limit)).fetchall()
        selected, used = [], 2
        for row in rows:
            current = row['sequence'] == cutoff
            if not current and not safe_record(row['content']):
                continue
            size = len(encoded({'role': row['role'], 'content': row['content']})) + 1
            if current or used + size <= max_bytes:
                selected.append(dict(row))
                used += size
        return list(reversed(selected))

    def search_messages(self, query, **options):
        from .recall import RecallService
        return RecallService(self.database).search_messages(query, **options)

    def search_memories(self, query, **options):
        from .recall import RecallService
        return RecallService(self.database).search_memories(query, **options)

    def recent_history(self, **options):
        from .recall import RecallService
        return RecallService(self.database).recent_history(**options)

    def task_history(self, conversation_id=None):
        if conversation_id is not None and self.get_conversation(conversation_id) is None:
            raise ValueError('Unknown conversation')
        return [task for task in self.task_store.list_tasks()
                if conversation_id is None or task['conversation_id'] == conversation_id]

    @staticmethod
    def _scope(scope, scope_id):
        if scope not in ('user', 'project'):
            raise ValueError('Invalid memory scope')
        text_value(scope_id, 200)

    def create_memory(self, *, scope, scope_id, content):
        self._scope(scope, scope_id)
        text_value(content)
        record = dict(id=str(uuid4()), scope=scope, scope_id=scope_id,
                      content=content, created_at=timestamp())
        with self.database.transaction() as db:
            db.execute('INSERT INTO memories VALUES (:id,:scope,:scope_id,:content,:created_at)', record)
        return record

    def get_memory(self, memory_id):
        if not valid_id(memory_id):
            return None
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM memories WHERE id=?', (memory_id,)).fetchone()
            return dict(row) if row else None

    def list_memories(self, *, scope, scope_id):
        self._scope(scope, scope_id)
        with self.database.transaction() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM memories WHERE scope=? AND scope_id=? ORDER BY rowid', (scope, scope_id))]

    def delete_memory(self, memory_id):
        if not valid_id(memory_id):
            return False
        with self.database.transaction() as db:
            return db.execute('DELETE FROM memories WHERE id=?', (memory_id,)).rowcount == 1
