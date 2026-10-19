"""Trusted deployment configuration and perimeter; never candidate-editable."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import re
import secrets
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import owner_login

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
    owner_login_hash: str = field(default='', repr=False)

    def __post_init__(self):
        if self.auth_token and (not 32 <= len(self.auth_token) <= 512 or
                any(not 33 <= ord(c) <= 126 for c in self.auth_token)):
            raise ValueError('Owner authentication requires a random ASCII token of at least 32 characters')
        if self.owner_login_hash:
            owner_login.parse_hash(self.owner_login_hash, production=self.mode == 'production')

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
        return cls(mode, host, port, state, db, token, origin,
                   owner_login_hash=env.get(owner_login.ENV_KEY, ''))

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
    """Single-owner authentication on every route except minimal readiness health.
    Tools use Bearer <JARVIS_AUTH_TOKEN>; the owner's browser uses the /login form
    and a server-side session cookie. There is no HTTP Basic and no WWW-Authenticate."""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.logins = owner_login.LoginStore(settings.database_path)

    def expected_origin(self, scope, headers):
        return self.settings.public_origin or (scope['scheme'] + '://' + headers.get(b'host', b'').decode('latin1'))

    def same_origin(self, scope, headers):
        origin = headers.get(b'origin')
        return origin is not None and origin.decode('latin1') == self.expected_origin(scope, headers)

    def session_id(self, headers):
        return owner_login.cookies(headers.get(b'cookie', b'').decode('latin1')).get(owner_login.SESSION_COOKIE, '')

    async def has_session(self, headers):
        session = self.session_id(headers)
        if not session or not self.settings.owner_login_hash:
            return False
        return await run_in_threadpool(self.logins.valid, session, self.settings.owner_login_hash)

    def page(self, status, message=''):
        csrf = secrets.token_urlsafe(32)
        response = HTMLResponse(owner_login.login_page(csrf, message), status_code=status,
                                headers={'Content-Security-Policy': owner_login.LOGIN_CSP})
        response.headers.append('set-cookie', owner_login.set_cookie(owner_login.CSRF_COOKIE, csrf, 3600))
        return response

    async def login(self, scope, receive, headers):
        if scope['method'] in {'GET', 'HEAD'}:
            return self.page(200)
        if scope['method'] != 'POST':
            return PlainTextResponse('Method not allowed', status_code=405, headers={'Allow': 'GET, HEAD, POST'})
        if not self.same_origin(scope, headers):
            return JSONResponse({'detail': 'Cross-origin request denied'}, status_code=403)
        if not headers.get(b'content-type', b'').split(b';')[0].strip() == b'application/x-www-form-urlencoded':
            return PlainTextResponse('Unsupported form encoding', status_code=415)
        body, more = b'', True
        while more:
            message = await receive()
            if message['type'] != 'http.request':
                return PlainTextResponse('Request incomplete', status_code=400)
            body += message.get('body', b'')
            more = message.get('more_body', False)
            if len(body) > 8192:
                return PlainTextResponse('Form too large', status_code=413)
        try:
            form = parse_qs(body.decode('utf-8'), keep_blank_values=True, strict_parsing=bool(body), max_num_fields=8)
        except (UnicodeError, ValueError):
            return PlainTextResponse('Invalid form', status_code=400)
        field = lambda name: form[name][0] if len(form.get(name, ())) == 1 else ''
        cookie = owner_login.cookies(headers.get(b'cookie', b'').decode('latin1')).get(owner_login.CSRF_COOKIE, '')
        supplied = field('csrf')
        if not cookie or not supplied or not hmac.compare_digest(cookie.encode(), supplied.encode()):
            return self.page(403, 'Your sign-in form expired. Please try again.')
        stored = self.settings.owner_login_hash
        if not stored:
            return PlainTextResponse('Password sign-in is not set up on this server.', status_code=503)
        wait = await run_in_threadpool(self.logins.begin_attempt)
        if wait:
            return PlainTextResponse(f'Too many failed sign-in attempts. Try again in {wait} seconds.',
                                     status_code=429, headers={'Retry-After': str(wait)})
        if not await run_in_threadpool(owner_login.verify, stored, field('username'), field('password')):
            return self.page(401, 'Wrong username or password.')
        session = await run_in_threadpool(self.logins.succeed, stored)
        response = RedirectResponse('/', status_code=303)
        response.headers.append('set-cookie', owner_login.set_cookie(
            owner_login.SESSION_COOKIE, session, owner_login.SESSION_SECONDS))
        response.headers.append('set-cookie', owner_login.set_cookie(owner_login.CSRF_COOKIE, '', 0))
        return response

    async def logout(self, scope, headers):
        if scope['method'] != 'POST':
            return PlainTextResponse('Method not allowed', status_code=405, headers={'Allow': 'POST'})
        if not await self.has_session(headers):
            return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        if not self.same_origin(scope, headers):
            return JSONResponse({'detail': 'Cross-origin request denied'}, status_code=403)
        await run_in_threadpool(self.logins.delete, self.session_id(headers))
        response = RedirectResponse('/login', status_code=303)
        response.headers.append('set-cookie', owner_login.set_cookie(owner_login.SESSION_COOKIE, '', 0))
        return response

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
                        (b'x-frame-options', b'DENY'), (b'referrer-policy', b'same-origin')]
            await send(message)

        try:
            if (self.settings.mode == 'production' or self.settings.auth_token) and scope['path'] != '/health':
                headers = dict(scope['headers'])
                expected_host = urlsplit(self.settings.public_origin).netloc.lower().encode()
                if self.settings.public_origin and headers.get(b'host', b'').lower() != expected_host:
                    return await JSONResponse({'detail': 'Invalid host'}, status_code=400)(scope, receive, logged_send)
                if scope['path'] == '/login':
                    return await (await self.login(scope, receive, headers))(scope, receive, logged_send)
                if scope['path'] == '/logout':
                    return await (await self.logout(scope, headers))(scope, receive, logged_send)
                bearer, cookie_auth = False, False
                auth = headers.get(b'authorization', b'').decode('latin1')
                if auth:
                    # Any Authorization header must be a valid Bearer token; Basic is gone.
                    scheme, _, value = auth.partition(' ')
                    bearer = bool(scheme.lower() == 'bearer' and value and self.settings.auth_token and
                                  hmac.compare_digest(hashlib.sha256(value.encode('latin1')).digest(),
                                                      hashlib.sha256(self.settings.auth_token.encode()).digest()))
                else:
                    cookie_auth = await self.has_session(headers)
                if not bearer and not cookie_auth:
                    if (not auth and scope['path'] == '/' and scope['method'] in {'GET', 'HEAD'} and
                            headers.get(b'sec-fetch-mode') == b'navigate'):
                        return await RedirectResponse('/login', status_code=303)(scope, receive, logged_send)
                    return await JSONResponse({'detail': 'Authentication required'}, status_code=401)(scope, receive, logged_send)
                origin = headers.get(b'origin')
                expected_origin = self.expected_origin(scope, headers)
                # External links may carry cross-site metadata. Permit only
                # top-level reads of the UI document; never extend this
                # exception to API routes or mutations.
                ui_navigation = (scope['path'] == '/' and scope['method'] in {'GET', 'HEAD'} and
                                 headers.get(b'sec-fetch-mode') == b'navigate' and
                                 headers.get(b'sec-fetch-dest') == b'document')
                if ((origin is not None and origin.decode('latin1') != expected_origin) or
                        (headers.get(b'sec-fetch-site') == b'cross-site' and not ui_navigation) or
                        # Cookie-authenticated mutations need an exact Origin; a missing one is refused.
                        (cookie_auth and scope['method'] not in {'GET', 'HEAD', 'OPTIONS'} and origin is None)):
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
