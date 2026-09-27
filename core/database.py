"""Lazy SQLite connections and transactional, repeatable schema initialization."""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import UUID


def timestamp():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def valid_id(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def text_value(value, limit=32000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError('Expected nonempty bounded text')
    return value


def default_database_path():
    # Host configuration only; never an API or model argument. Keep private data
    # outside the repository and therefore outside read tools/test snapshots.
    return Path(os.environ.get('JARVIS_DB_PATH',
        str(Path.home() / '.local/state/jarvis/jarvis.sqlite3')))


SCHEMA = (
    "CREATE TABLE conversations (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        goal TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
        steps TEXT NOT NULL DEFAULT '[]', response TEXT, agent TEXT)""",
    """CREATE TABLE messages (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        task_id TEXT REFERENCES tasks(id), role TEXT NOT NULL CHECK(role IN ('user','assistant')),
        content TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_id, role))""",
    "CREATE INDEX messages_conversation ON messages(conversation_id, sequence)",
    """CREATE TABLE activity (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id), event TEXT NOT NULL)""",
    "CREATE INDEX activity_task ON activity(task_id, sequence)",
    """CREATE TABLE memories (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('user','project')),
        scope_id TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX memories_scope ON memories(scope, scope_id)",
)


class Database:
    def __init__(self, path=':memory:'):
        self.path = ':memory:' if str(path) == ':memory:' else str(Path(path).resolve())
        if self.path != ':memory:' and Path(self.path).resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError('Database must be outside the repository')
        self._connection = None
        self._lock = RLock()
        self._depth = 0

    def _connect(self):
        if self._connection is None:
            if self.path != ':memory:':
                Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            try:
                connection.execute('BEGIN IMMEDIATE')
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version > 4:
                    raise ValueError('Unsupported database schema version')
                if version == 0:
                    for statement in SCHEMA:
                        connection.execute(statement)
                    connection.execute('PRAGMA user_version=1')
                if version < 2:
                    connection.execute('''CREATE TABLE candidates (
                        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                        state TEXT NOT NULL, busy INTEGER NOT NULL DEFAULT 0,
                        record TEXT NOT NULL)''')
                    connection.execute('''CREATE TABLE candidate_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        candidate_id TEXT NOT NULL REFERENCES candidates(id),
                        timestamp TEXT NOT NULL, event TEXT NOT NULL)''')
                    connection.execute('PRAGMA user_version=2')
                if version < 3:
                    connection.execute('''CREATE TABLE experiences (
                        id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
                        source_task_id TEXT REFERENCES tasks(id),
                        source_candidate_id TEXT REFERENCES candidates(id),
                        task_type TEXT NOT NULL, outcome TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        record TEXT NOT NULL CHECK(length(record)<=8000))''')
                    connection.execute('CREATE INDEX experiences_type_time ON experiences(task_type,created_at)')
                    connection.execute('PRAGMA user_version=3')
                if version < 4:
                    connection.execute('''CREATE TABLE experience_quality (
                        experience_id TEXT PRIMARY KEY REFERENCES experiences(id) ON DELETE CASCADE,
                        retrieval_count INTEGER NOT NULL DEFAULT 0,
                        helpful_count INTEGER NOT NULL DEFAULT 0,
                        neutral_count INTEGER NOT NULL DEFAULT 0,
                        misleading_count INTEGER NOT NULL DEFAULT 0,
                        confidence_score REAL NOT NULL DEFAULT 0.5 CHECK(confidence_score BETWEEN 0.05 AND 0.95),
                        last_used_at TEXT, last_evaluated_at TEXT, last_signal_at TEXT,
                        generation INTEGER NOT NULL DEFAULT 0)''')
                    connection.execute('INSERT INTO experience_quality(experience_id) SELECT id FROM experiences')
                    connection.execute('''CREATE TABLE experience_uses (
                        experience_id TEXT NOT NULL REFERENCES experiences(id) ON DELETE CASCADE,
                        task_id TEXT NOT NULL REFERENCES tasks(id), generation INTEGER NOT NULL,
                        used_at TEXT NOT NULL, snapshot TEXT NOT NULL CHECK(length(snapshot)<=8000),
                        evaluated INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(experience_id,task_id))''')
                    connection.execute('CREATE INDEX experience_uses_task ON experience_uses(task_id)')
                    connection.execute('''CREATE TABLE experience_evaluations (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                        experience_id TEXT NOT NULL REFERENCES experiences(id) ON DELETE CASCADE,
                        task_id TEXT REFERENCES tasks(id), generation INTEGER NOT NULL,
                        source TEXT NOT NULL CHECK(source IN ('automatic','owner','reset')),
                        verdict TEXT NOT NULL CHECK(verdict IN ('helpful','neutral','misleading','reset')),
                        evidence TEXT NOT NULL CHECK(length(evidence)<=2000), created_at TEXT NOT NULL)''')
                    connection.execute('CREATE INDEX experience_evaluations_history ON experience_evaluations(experience_id,sequence)')
                    connection.execute('PRAGMA user_version=4')
                connection.commit()
            except Exception:
                connection.rollback()
                connection.close()
                raise
            self._connection = connection
        return self._connection

    @contextmanager
    def transaction(self):
        with self._lock:
            connection = self._connect()
            outermost = self._depth == 0
            if outermost:
                connection.execute('BEGIN IMMEDIATE')
            self._depth += 1
            try:
                yield connection
                if outermost:
                    connection.commit()
            except Exception:
                if outermost:
                    connection.rollback()
                raise
            finally:
                self._depth -= 1

    def initialize(self):
        with self.transaction():
            pass

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
