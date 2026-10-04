"""Informational deployment projections; never an authorization source.

Production reads only the controller's bounded, sanitized state-volume snapshot.
It needs no host journal, controller configuration, Git or Docker capability.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat

from tools.policy import ToolDenied

STATUS_PATH = Path('/var/lib/jarvis/deployment-status.json')
MAX_BYTES = 100_000
STATUSES = {'building', 'prechecking', 'switching', 'verifying', 'deployed',
            'rolling_back', 'rolled_back', 'failed', 'rollback_failed'}


def token(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def timestamp(value):
    if not token(value, r'\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|\+00:00)'):
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def identity(value):
    value = value if isinstance(value, dict) else {}
    return dict(revision=token(value.get('revision'), r'[0-9a-f]{40}'),
                image=token(value.get('image'), r'sha256:[0-9a-f]{64}'))


def evidence(value):
    value = value if isinstance(value, dict) else {}
    at = timestamp(value.get('at'))
    expiry = datetime.fromisoformat(at) + timedelta(hours=24) if at else None
    tests = value.get('tests', [])
    return dict(commit=token(value.get('commit'), r'[0-9a-f]{40}'), at=at,
                expires_at=expiry.isoformat() if expiry else None,
                passed=value.get('passed') if type(value.get('passed')) is bool else None,
                fresh=bool(at and value.get('passed') is True and
                           datetime.fromisoformat(at) <= datetime.now(timezone.utc) < expiry),
                tests=[dict(phase=t['phase'], passed=t['passed']) for t in tests[:2]
                       if isinstance(t, dict) and t.get('phase') in {'python', 'ui'}
                       and type(t.get('passed')) is bool] if isinstance(tests, list) else [])


def release(value):
    value = value if isinstance(value, dict) else {}
    rollback = value.get('rollback')
    return dict(id=token(value.get('id'), r'[0-9a-f]{32}'),
                commit=token(value.get('commit'), r'[0-9a-f]{40}'),
                image=identity(value)['image'],
                status=value.get('status') if value.get('status') in STATUSES else None,
                requested_at=timestamp(value.get('requested_at')),
                updated_at=timestamp(value.get('updated_at')),
                completed_at=timestamp(value.get('completed_at')),
                previous=identity(value.get('previous')),
                rollback=dict(**identity(rollback),
                              verified=rollback.get('verified') is True,
                              at=timestamp(rollback.get('at'))) if isinstance(rollback, dict) else None,
                evidence=evidence(value.get('evidence')))


def projection(current, records, observed_at):
    # Do not copy arbitrary strings: no messages, paths, approvals, logs, config,
    # conversation fingerprints, exception text or user-supplied metadata.
    return dict(version=1, observed_at=timestamp(observed_at), current=identity(current),
                history=[release(r) for r in records[-10:]][::-1])


def redact(value):
    encoded = json.dumps(value)
    for key in ('OPENAI_API_KEY', 'JARVIS_AUTH_TOKEN'):
        secret = os.environ.get(key)
        if secret:
            encoded = encoded.replace(json.dumps(secret)[1:-1], '[redacted]')
    return json.loads(encoded)


class SnapshotInspector:
    def __init__(self, path=None):
        self.path = path if path is not None else STATUS_PATH

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError()
                value = json.loads(stream.read(MAX_BYTES + 1))
            if value.get('version') != 1 or not isinstance(value.get('history'), list):
                raise ValueError()
            result = projection(value.get('current'), value['history'][::-1], value.get('observed_at'))
            return redact(result)
        except Exception:
            raise ToolDenied('Deployment status snapshot unavailable', reason='deployment_status_unavailable') from None

    def status(self):
        data = self.load()
        return dict(current=data['current'], latest=data['history'][0] if data['history'] else None,
                    observed_at=data['observed_at'], source='controller_snapshot',
                    note='Last controller observation, not a live health probe. Snapshot never authorizes deployment.')

    def history(self):
        return self.load()['history']

    def inspect(self, identifier):
        for record in self.history():
            if record['id'] == identifier:
                return record
        raise ToolDenied('Deployment not present in bounded status history')

    def preview(self, commit):
        if not token(commit, r'[0-9a-f]{40}'):
            raise ToolDenied('Full commit required')
        record = next((r for r in self.history() if r['commit'] == commit), None)
        return dict(commit=commit, evidence=record['evidence'] if record else None,
                    eligible=None, source='controller_snapshot',
                    note='Live eligibility requires the host controller to check clean HEAD and fresh validation.')


class ControllerInspector(SnapshotInspector):
    def __init__(self, service):
        self.service = service

    def load(self):
        from .deployment_release import now
        return self.service.runtime.redact(projection(self.service.runtime.current(),
                                                     self.service.load()['records'], now()))

    def status(self):
        result = super().status()
        result.update(source='host_controller', note='Observed container identity; not a health probe.')
        return result

    def preview(self, commit):
        value = self.service.preview(commit)
        return self.service.runtime.redact(dict(commit=value['commit'], eligible=value['eligible'],
                                               evidence=evidence(value.get('evidence')),
                                               current=identity(value.get('previous'))))


def configured_inspector():
    if os.environ.get('JARVIS_MODE', 'development') == 'production':
        return SnapshotInspector()
    from .deployment_release import configured_service
    return ControllerInspector(configured_service())
