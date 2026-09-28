"""Operator-run host broker. Production never receives this process's authority."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from tools.policy import ToolDenied
from .candidate_bridge import Request, project, MAX_BYTES
from .candidate_tools import CandidateTools
from .database import Database
from .self_edit import SelfEditService

SOURCE = Path('/home/ubuntu/jarvis-dev')
STATE = Path('/home/ubuntu/.local/state/jarvis-chat-candidates')
CONTAINER = 'jarvis-jarvis-1'


class Broker:
    def __init__(self, service):
        self.service = service

    def execute(self, raw):
        request = Request.model_validate(raw).checked()
        context = dict(source='production_chat', task_id=request.task_id,
                       conversation_id=request.conversation_id, request_id=request.request_id)
        # Recheck current-message authority on the host, independent of the model.
        allowed = CandidateTools(request.goal, self.service.store.database)
        if request.action not in allowed.allowed:
            raise ToolDenied('Request lacks candidate authority')
        if request.action == 'create':
            # Durable idempotency by source task, including crash/retry requests.
            with self.service.store.database.transaction() as db:
                rows = db.execute("SELECT record FROM candidates WHERE json_extract(record, '$.source_context.task_id') = ? LIMIT 1", (request.task_id,)).fetchall()
            for row in rows:
                existing = json.loads(row['record'])
                prior = existing.get('source_context', {})
                if prior.get('source') == 'production_chat' and prior.get('task_id') == request.task_id:
                    if prior.get('conversation_id') != request.conversation_id:
                        raise ToolDenied('Source task mismatch')
                    return project(self.service.store.get(existing['id']))
            record = self.service.start(request.goal, **request.arguments, source_context=context)
        else:
            identifier = request.arguments['candidate_id']
            existing = self.service.store.get(identifier, internal=True)
            if not existing or existing.get('source_context', {}).get('source') != 'production_chat':
                raise ToolDenied('Candidate is not owned by production chat')
            if identifier not in allowed.ids and existing['source_context'].get('task_id') != request.task_id:
                raise ToolDenied('Candidate outside request scope')
            if existing.get('source_root') != str(self.service.source):
                raise ToolDenied('Candidate source mismatch')
            operations = existing.setdefault('chat_operations', [])
            if any(item['request_id'] == request.request_id for item in operations):
                return project(existing)
            if len(operations) >= 64:
                raise ToolDenied('Candidate operation audit limit reached')
            operations.append(context | {'action': request.action})
            self.service.store.save(existing, 'production_chat_' + request.action)
            handler = {'inspect': self.service.inspect, 'tests': self.service.tests,
                       'review': self.service.review, 'discard': self.service.discard}[request.action]
            record = handler(identifier)
        return project(record)


def transport(operation, payload=None):
    data = json.dumps(dict(operation=operation, payload=payload)).encode()
    with tempfile.TemporaryDirectory(prefix='jarvis-broker-home-') as home:
        result = subprocess.run(['/usr/bin/docker', '--host=unix:///var/run/docker.sock',
                                 'exec', '-i', CONTAINER, 'python', '-B', '-m', 'core.candidate_bridge'],
                                input=data, capture_output=True, timeout=10,
                                env={'PATH': '/usr/bin:/bin', 'HOME': home})
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise ToolDenied('Candidate mailbox unavailable')
    return json.loads(result.stdout)


def main():
    import fcntl
    if os.environ.get('JARVIS_MODE') != 'development' or Path(__file__).resolve().parents[1] != SOURCE:
        raise ToolDenied('Broker requires the authorized development runtime')
    if SOURCE.resolve() != SOURCE or not (SOURCE / '.git').is_dir():
        raise ToolDenied('Invalid development checkout')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.resolve() != STATE or STATE.stat().st_mode & 0o077:
        raise ToolDenied('Unsafe broker state directory')
    fd = os.open(STATE / 'broker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    service = SelfEditService(Database(STATE / 'candidates.sqlite3'), source_root=SOURCE,
                              candidate_root=STATE / 'candidates')
    broker = Broker(service)
    while True:
        try:
            raw = transport('receive')
            if raw:
                try:
                    record = broker.execute(raw)
                    response = dict(request_id=raw['request_id'], ok=True, record=record)
                except Exception:
                    response = dict(request_id=raw['request_id'], ok=False)
                transport('respond', response)
        except Exception:
            # Fixed diagnostic only. Neither Docker output nor requests are logs.
            pass
        time.sleep(1)


if __name__ == '__main__':
    main()
