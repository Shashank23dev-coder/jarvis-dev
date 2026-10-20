"""Server-side pending actions: at most one per conversation, created only by host
code, confirmed once by a short closed-list reply within ten minutes. Confirmation
returns the action exactly as stored and never re-parses the original request.
The session binding comes from the in-process context (like current_owner), never
a request header; only a hash is stored, in its own table of the Jarvis SQLite file."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import re
import sqlite3
import time
import unicodedata
from uuid import uuid4

from tools.policy import ToolDenied
from .database import default_database_path, text_value

TTL_SECONDS = 600
# Pause and resume need no confirmation; settings changes are not allowed in M2.
KINDS = frozenset({'land', 'apply', 'deploy', 'discard', 'cancel_job'})
CONFIRMATIONS = frozenset({
    'yes', 'yes do it', 'yes go ahead', 'yes please', 'yes confirm', 'yes confirmed',
    'yeah', 'yep', 'go ahead', 'go ahead please', 'do it', 'please do it', 'ok do it',
    'confirm', 'confirmed', 'haan', 'han', 'haan ji', 'ji haan', 'haan kar do',
    'han kar do', 'haan karo', 'kar do', 'theek hai kar do', 'thik hai kar do',
    'theek hai karo', 'haan theek hai kar do',
})
MAX_REPLY = 64
MAX_PAYLOAD = 4096

# Set only by the authenticated HTTP perimeter for the duration of one request.
current_session = ContextVar('pending_session_binding', default=None)


def session_binding():
    value = current_session.get()
    if not isinstance(value, str) or not value:
        raise ToolDenied('Authenticated owner session required', reason='session_required')
    return value


def session_hash(binding):
    if not isinstance(binding, str) or not binding:
        raise ValueError('Session binding required')
    return hashlib.sha256(b'jarvis-pending-action-session\0' + binding.encode('utf-8')).hexdigest()


def normalize_reply(text):
    if not isinstance(text, str) or len(text) > MAX_REPLY:
        return ''
    text = unicodedata.normalize('NFKC', text).casefold().strip()
    text = re.sub(r'[\W_]+$', '', text)  # trailing punctuation, emoji, whitespace
    return ' '.join(text.replace(',', ' ').split())


def classify_reply(text):
    """'confirm' only for a short closed-list confirmation; anything else cancels."""
    return 'confirm' if normalize_reply(text) in CONFIRMATIONS else 'cancel'


@dataclass(frozen=True)
class PendingAction:
    id: str
    conversation_id: str
    session_hash: str
    kind: str
    payload: dict
    summary: str
    risk: str
    undo: str
    created_at: float
    expires_at: float
    state: str


@dataclass(frozen=True)
class Outcome:
    status: str  # confirmed | cancelled | refused | verified
    action: PendingAction | None = None
    reason: str = ''


COLUMNS = 'id, conversation_id, session_hash, kind, payload, summary, risk, undo, created_at, expires_at, state'


class PendingActions:
    def __init__(self, path=None, clock=time.time):
        self.path = str(path if path is not None else default_database_path())
        self.clock = clock

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute('''CREATE TABLE IF NOT EXISTS pending_actions (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, session_hash TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, summary TEXT NOT NULL, risk TEXT NOT NULL,
            undo TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending', 'confirmed', 'executed', 'cancelled', 'expired')))''')
        connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS pending_actions_one_per_conversation
            ON pending_actions(conversation_id) WHERE state = 'pending' ''')
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.execute('COMMIT')
        except BaseException:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            raise
        finally:
            connection.close()

    @staticmethod
    def _action(row):
        values = list(row)
        values[4] = json.loads(values[4])
        return PendingAction(*values)

    def _pending_row(self, connection, conversation_id):
        return connection.execute(f'SELECT {COLUMNS} FROM pending_actions WHERE conversation_id = ? AND state = ?',
                                  (conversation_id, 'pending')).fetchone()

    def create(self, kind, payload, summary, risk, undo, session_binding, conversation_id):
        """Host code only. Cancels any earlier pending action in the conversation."""
        if kind not in KINDS:
            raise ValueError('Unsupported pending action kind')
        if not isinstance(payload, dict):
            raise ValueError('Payload must be an object')
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except TypeError:
            raise ValueError('Payload must be plain JSON') from None
        if len(encoded) > MAX_PAYLOAD or json.loads(encoded) != payload:
            raise ValueError('Payload must be small, plain JSON')
        summary, risk, undo = (text_value(value, 2000) for value in (summary, risk, undo))
        conversation_id = text_value(conversation_id, 200)
        bound = session_hash(session_binding)
        now = self.clock()
        action = PendingAction(str(uuid4()), conversation_id, bound, kind, json.loads(encoded),
                               summary, risk, undo, now, now + TTL_SECONDS, 'pending')
        with self._transaction() as connection:
            connection.execute('UPDATE pending_actions SET state = ? WHERE conversation_id = ? AND state = ?',
                               ('cancelled', conversation_id, 'pending'))
            connection.execute(f'INSERT INTO pending_actions({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               (action.id, conversation_id, bound, kind, encoded, summary, risk, undo,
                                action.created_at, action.expires_at, 'pending'))
        return action

    def get_pending(self, conversation_id, now=None):
        now = self.clock() if now is None else now
        connection = self._connect()
        try:
            row = self._pending_row(connection, conversation_id)
        finally:
            connection.close()
        return self._action(row) if row and row[9] > now else None

    def cancel(self, conversation_id):
        with self._transaction() as connection:
            cursor = connection.execute('UPDATE pending_actions SET state = ? WHERE conversation_id = ? AND state = ?',
                                        ('cancelled', conversation_id, 'pending'))
        return cursor.rowcount == 1

    def confirm(self, conversation_id, session_binding, message_text, now=None):
        """Single use: atomically pending -> confirmed. A reply that is not a valid
        confirmation cancels the pending action instead."""
        now = self.clock() if now is None else now
        reply = classify_reply(message_text)
        with self._transaction() as connection:
            row = self._pending_row(connection, conversation_id)
            if row is None:
                return Outcome('refused', reason='no_pending_action')
            action = self._action(row)
            if not _same_session(action.session_hash, session_binding):
                return Outcome('refused', reason='session_mismatch')
            if reply != 'confirm':
                connection.execute('UPDATE pending_actions SET state = ? WHERE id = ? AND state = ?',
                                   ('cancelled', action.id, 'pending'))
                return Outcome('cancelled', replace(action, state='cancelled'), 'not_a_confirmation')
            if action.expires_at <= now:
                connection.execute('UPDATE pending_actions SET state = ? WHERE id = ? AND state = ?',
                                   ('expired', action.id, 'pending'))
                return Outcome('refused', reason='expired')
            cursor = connection.execute('UPDATE pending_actions SET state = ? WHERE id = ? AND state = ?',
                                        ('confirmed', action.id, 'pending'))
            if cursor.rowcount != 1:
                return Outcome('refused', reason='already_used')
            return Outcome('confirmed', replace(action, state='confirmed'))

    def verify(self, action, session_binding, now=None):
        """Called by the worker right before execution: re-checks the stored record,
        binding and expiry, then atomically claims it (confirmed -> executed) once."""
        now = self.clock() if now is None else now
        if not isinstance(action, PendingAction):
            return Outcome('refused', reason='mismatch')
        with self._transaction() as connection:
            row = connection.execute(f'SELECT {COLUMNS} FROM pending_actions WHERE id = ?', (action.id,)).fetchone()
            if row is None:
                return Outcome('refused', reason='unknown')
            stored = self._action(row)
            if stored.state != 'confirmed':
                return Outcome('refused', reason='already_used' if stored.state == 'executed' else 'not_confirmed')
            if stored.expires_at <= now:
                return Outcome('refused', reason='expired')
            if not _same_session(stored.session_hash, session_binding):
                return Outcome('refused', reason='session_mismatch')
            if stored != action:
                return Outcome('refused', reason='mismatch')
            cursor = connection.execute('UPDATE pending_actions SET state = ? WHERE id = ? AND state = ?',
                                        ('executed', stored.id, 'confirmed'))
            if cursor.rowcount != 1:
                return Outcome('refused', reason='already_used')
            return Outcome('verified', stored)


def _same_session(stored_hash, binding):
    try:
        return hmac.compare_digest(stored_hash, session_hash(binding))
    except ValueError:
        return False
