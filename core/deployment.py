"""Trusted deployment configuration and perimeter; never candidate-editable."""
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import re
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    mode: str = 'development'
    host: str = '127.0.0.1'
    port: int = 8000
    state_dir: Path = field(default_factory=lambda: Path.home() / '.local/state/jarvis')
    database_path: Path = field(default_factory=lambda: Path.home() / '.local/state/jarvis/jarvis.sqlite3')
    auth_token: str = field(default='', repr=False)
    public_origin: str = ''

    def __post_init__(self):
        if self.auth_token and (not 32 <= len(self.auth_token) <= 512 or
                any(not 33 <= ord(c) <= 126 for c in self.auth_token)):
            raise ValueError('Owner authentication requires a random ASCII token of at least 32 characters')

    @classmethod
    def from_env(cls, env=None, *, database_path=None):
        env = os.environ if env is None else env
        mode = env.get('JARVIS_MODE', 'development')
        if mode not in {'development', 'production'}:
            raise ValueError('JARVIS_MODE must be development or production')
        host = env.get('JARVIS_HOST', '127.0.0.1')
        try:
            port = int(env.get('JARVIS_PORT', '8000'))
        except ValueError:
            raise ValueError('JARVIS_PORT must be an integer') from None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
                raise ValueError('Invalid JARVIS_HOST') from None
        if not 1 <= port <= 65535:
            raise ValueError('Invalid JARVIS_PORT')
        state = Path(env.get('JARVIS_STATE_DIR', str(Path.home() / '.local/state/jarvis')))
        db = Path(database_path or env.get('JARVIS_DB_PATH', str(state / 'jarvis.sqlite3')))
        token = env.get('JARVIS_AUTH_TOKEN', '')
        origin = env.get('JARVIS_PUBLIC_ORIGIN', '').rstrip('/')
        if mode == 'production':
            if not env.get('OPENAI_API_KEY', '').strip():
                raise ValueError('OPENAI_API_KEY is required in production')
            if not 32 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
                raise ValueError('JARVIS_AUTH_TOKEN must be a random ASCII token of at least 32 characters')
            try:
                parsed = urlsplit(origin)
                origin_port = parsed.port
            except ValueError:
                raise ValueError('Invalid JARVIS_PUBLIC_ORIGIN') from None
            if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or
                    any(ord(c) < 33 or ord(c) > 126 for c in origin) or
                    origin_port == 0 or '\\' in origin or
                    parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or
                    (parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'})):
                raise ValueError('JARVIS_PUBLIC_ORIGIN must be an HTTPS origin (HTTP allowed only on loopback)')
            if not state.is_absolute() or not db.is_absolute() or str(db) == ':memory:':
                raise ValueError('Production state and database paths must be absolute and persistent')
        if db.is_absolute() and (db.resolve().is_relative_to(ROOT) or db.resolve() == ROOT):
            raise ValueError('Database must be outside the repository')
        if mode == 'production':
            if (state.resolve().is_relative_to(ROOT) or state in ROOT.parents or not db.is_relative_to(state) or
                    db == state or state.resolve() != state or db.resolve() != db):
                raise ValueError('Production database must be inside a separate, non-symlinked state directory')
        return cls(mode, host, port, state, db, token, origin)

    def check_storage(self):
        if self.mode != 'production':
            return
        # Check again at startup, including symlinks created after configuration.
        if self.state_dir.resolve() != self.state_dir or self.database_path.resolve() != self.database_path:
            raise RuntimeError('Unsafe persistent state path')
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in {self.state_dir, self.database_path.parent, self.database_path.parent / 'candidates'}:
            if directory.is_symlink():
                raise RuntimeError('Unsafe persistent state path')
            directory.mkdir(exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b'jarvis-startup-check')
                probe.flush()
                os.fsync(probe.fileno())


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(), 'level': record.levelname,
                'logger': record.name, 'event': record.getMessage()}
        for key in ('method', 'route', 'status', 'duration_ms'):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        # Never log raw request bodies, authorization headers or exception details.
        if record.exc_info:
            data['exception_type'] = record.exc_info[0].__name__
        return json.dumps(data)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('openai').setLevel(logging.WARNING)


class DeploymentMiddleware:
    """Single-owner authentication on every route except minimal readiness health."""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'websocket' and self.settings.mode == 'production':
                await send({'type': 'websocket.close', 'code': 1008})
                return
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = 500

        async def logged_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                if self.settings.mode == 'production':
                    message['headers'] = list(message.get('headers', [])) + [
                        (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                        (b'x-frame-options', b'DENY'), (b'referrer-policy', b'no-referrer')]
            await send(message)

        try:
            if (self.settings.mode == 'production' or self.settings.auth_token) and scope['path'] != '/health':
                headers = dict(scope['headers'])
                expected_host = urlsplit(self.settings.public_origin).netloc.lower().encode()
                if self.settings.public_origin and headers.get(b'host', b'').lower() != expected_host:
                    return await JSONResponse({'detail': 'Invalid host'}, status_code=400)(scope, receive, logged_send)
                supplied = ''
                auth = headers.get(b'authorization', b'').decode('latin1')
                try:
                    scheme, value = auth.split(' ', 1)
                    if scheme.lower() == 'bearer':
                        supplied = value
                    elif scheme.lower() == 'basic':
                        username, password = base64.b64decode(value, validate=True).decode('utf-8').split(':', 1)
                        if username == 'jarvis':
                            supplied = password
                except (ValueError, UnicodeError):
                    pass
                if not supplied or not self.settings.auth_token or not hmac.compare_digest(
                        hashlib.sha256(supplied.encode()).digest(),
                        hashlib.sha256(self.settings.auth_token.encode()).digest()):
                    return await JSONResponse({'detail': 'Authentication required'}, status_code=401,
                        headers={'WWW-Authenticate': 'Basic realm="Jarvis", charset="UTF-8"'})(scope, receive, logged_send)
                origin = headers.get(b'origin')
                expected_origin = self.settings.public_origin or (
                    scope['scheme'] + '://' + headers.get(b'host', b'').decode('latin1'))
                # External links may carry cross-site metadata after the Basic
                # challenge. Permit only top-level reads of the UI document;
                # never extend this exception to API routes or mutations.
                ui_navigation = (scope['path'] == '/' and scope['method'] in {'GET', 'HEAD'} and
                                 headers.get(b'sec-fetch-mode') == b'navigate' and
                                 headers.get(b'sec-fetch-dest') == b'document')
                if ((origin is not None and origin.decode('latin1') != expected_origin) or
                        (headers.get(b'sec-fetch-site') == b'cross-site' and not ui_navigation)):
                    return await JSONResponse({'detail': 'Cross-origin request denied'}, status_code=403)(scope, receive, logged_send)
            from .engineering_authority import OwnerDevAuthority, current_owner
            authority = (OwnerDevAuthority() if self.settings.auth_token
                         and scope['path'] != '/health' else None)
            binding = current_owner.set(authority)
            try:
                await self.app(scope, receive, logged_send)
            finally:
                current_owner.reset(binding)
        finally:
            route = scope.get('route')
            logging.getLogger('jarvis.http').info('request_completed', extra={
                'method': scope['method'], 'route': getattr(route, 'path', 'unmatched'),
                'status': status, 'duration_ms': round((time.monotonic() - started) * 1000, 2)})
