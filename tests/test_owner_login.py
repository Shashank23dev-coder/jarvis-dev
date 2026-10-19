"""Owner username/password login: hash format, CLI, login form, CSRF, sessions,
lockout, logout, the Bearer/Basic boundary and the release tool handover."""
import builtins
import getpass
import io
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import main
from core import owner_login
from core.deployment import Settings
from core.deployment_runtime import DockerRuntime, FIXED_ENV
from tools import ToolDenied
from test_deployment import production, owner_hash, browser_session, OWNER_USERNAME, OWNER_PASSWORD
from test_deployment_release import container_info

ORIGIN = 'https://jarvis.example.com'


def client_for(production):
    settings = Settings.from_env(production)
    client = TestClient(main.create_app(settings=settings), base_url=ORIGIN, follow_redirects=False)
    return client, settings


def middleware(client):
    # The single DeploymentMiddleware instance inside the built middleware stack.
    app = client.app.middleware_stack
    while app is not None and type(app).__name__ != 'DeploymentMiddleware':
        app = getattr(app, 'app', None)
    assert app is not None
    return app


def csrf_from(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def submit(client, username, password, *, origin=ORIGIN, csrf=None, fresh=True):
    if fresh:
        page = client.get('/login')
        csrf = csrf_from(page) if csrf is None else csrf
    headers = {'Origin': origin} if origin is not None else {}
    return client.post('/login', data={'username': username, 'password': password, 'csrf': csrf or ''}, headers=headers)


# --- hash format and validation -------------------------------------------------

def test_hash_format_and_round_trip():
    value = owner_hash()
    scheme, n, r, p, salt, digest = value.split('$')
    assert (scheme, n, r, p) == ('scrypt', '32768', '8', '1')
    for part in (salt, digest):
        assert re.fullmatch(r'[A-Za-z0-9_-]+', part)
    parsed = owner_login.parse_hash(value, production=True)
    assert len(parsed[3]) == 16 and len(parsed[4]) == 32
    assert owner_login.verify(value, OWNER_USERNAME, OWNER_PASSWORD)
    # Username is case-insensitive and NFKC-normalized; the password is exact.
    assert owner_login.verify(value, 'OWNER', OWNER_PASSWORD)
    assert owner_login.verify(value, '\uff2fwner', OWNER_PASSWORD)  # fullwidth O
    assert not owner_login.verify(value, OWNER_USERNAME, OWNER_PASSWORD.upper())
    assert not owner_login.verify(value, 'someone', OWNER_PASSWORD)
    assert not owner_login.verify(value, OWNER_USERNAME + '\0', OWNER_PASSWORD[1:])
    # Neither the username nor the password appears in the stored value.
    assert OWNER_USERNAME.lower() not in value.lower() and OWNER_PASSWORD not in value
    assert owner_login.make_hash('a', 'b' * 12) != owner_login.make_hash('a', 'b' * 12)  # random salt


def test_verify_runs_scrypt_once_for_wrong_username_and_wrong_password(monkeypatch):
    stored = owner_login.make_hash('owner', 'x' * 12, n=16)
    calls = []
    real = owner_login.hashlib.scrypt
    monkeypatch.setattr(owner_login.hashlib, 'scrypt', lambda *a, **k: calls.append(k) or real(*a, **k))
    assert not owner_login.verify(stored, 'wrong-user', 'x' * 12)
    assert not owner_login.verify(stored, 'owner', 'wrong-password')
    assert owner_login.verify(stored, 'owner', 'x' * 12)
    assert len(calls) == 3 and all(k['maxmem'] == 64 * 1024 * 1024 for k in calls)


def small(n=16, r=8, p=1, salt=16, digest=32):
    b64 = owner_login._b64encode
    return f'scrypt${n}${r}${p}${b64(os.urandom(salt))}${b64(os.urandom(digest))}'


@pytest.mark.parametrize('value', [
    'plain-password', 'scrypt$32768$8$1$abc', 'pbkdf2$32768$8$1$' + 'A' * 22 + '$' + 'A' * 43,
    small().replace('$', '$=', 1), small() + '=', small() + '$extra', small(n=1000), small(n=0),
    small(salt=4), small(digest=8), 'scrypt$32768$8$1$' + 'A' * 22 + '=$' + 'A' * 43,
    'scrypt$32768$8$1$' + 'A' * 21 + 'B$' + 'A' * 43,  # non-canonical base64
    small(n=2 ** 17, r=8),  # exceeds the 64 MB bound
])
def test_invalid_hash_values_rejected(production, value):
    with pytest.raises(ValueError) as error:
        Settings.from_env(dict(production, JARVIS_OWNER_LOGIN_HASH=value))
    assert value not in str(error.value)
    with pytest.raises(ValueError):
        Settings(owner_login_hash=value)


@pytest.mark.parametrize('kwargs', [dict(n=2 ** 14), dict(r=4), dict(p=2), dict(salt=8), dict(digest=16)])
def test_production_requires_strong_parameters(production, kwargs):
    value = small(**{'n': 2 ** 15, **kwargs})
    with pytest.raises(ValueError):
        Settings.from_env(dict(production, JARVIS_OWNER_LOGIN_HASH=value))
    # Outside production, smaller parameters are allowed for tests.
    assert Settings(owner_login_hash=value).owner_login_hash == value


def test_hash_is_optional_and_never_in_repr(production):
    settings = Settings.from_env(production)
    assert settings.owner_login_hash == owner_hash()
    assert owner_hash() not in repr(settings) and owner_hash().split('$')[-1] not in repr(settings)
    without = dict(production)
    without.pop('JARVIS_OWNER_LOGIN_HASH')
    assert Settings.from_env(without).owner_login_hash == ''


# --- CLI ------------------------------------------------------------------------

@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / 'jarvis.env'
    path.write_text('OPENAI_API_KEY=provider\nJARVIS_OWNER_LOGIN_HASH=old\nJARVIS_AUTH_TOKEN=token\n')
    path.chmod(0o600)
    return path


def run_cli(path, username='Owner', passwords=('long enough password',) * 2, monkeypatch=None):
    out, err = io.StringIO(), io.StringIO()
    prompts = []
    answers = iter(passwords)
    def secret(prompt):
        prompts.append(prompt)
        return next(answers)
    code = owner_login.main(['set', '--env-file', str(path)], ask=lambda prompt: username,
                            secret=secret, out=out, err=err)
    return code, out.getvalue(), err.getvalue(), prompts


def test_cli_defaults_use_input_and_getpass():
    defaults = owner_login.main.__kwdefaults__
    assert defaults['ask'] is builtins.input and defaults['secret'] is getpass.getpass


def test_cli_writes_only_the_hash_line_atomically_and_privately(env_file, monkeypatch):
    replaced = []
    real = os.replace
    monkeypatch.setattr(owner_login.os, 'replace', lambda a, b: replaced.append((a, b)) or real(a, b))
    inode = env_file.stat().st_ino
    code, out, err, prompts = run_cli(env_file)
    assert code == 0 and out == 'Login hash saved.\n' and err == ''
    assert prompts == ['Password: ', 'Repeat password: ']
    lines = env_file.read_text().splitlines()
    assert lines[0] == 'OPENAI_API_KEY=provider' and lines[2] == 'JARVIS_AUTH_TOKEN=token'
    assert len(lines) == 3 and lines[1].startswith('JARVIS_OWNER_LOGIN_HASH=scrypt$')
    value = lines[1].split('=', 1)[1]
    owner_login.parse_hash(value, production=True)
    assert owner_login.verify(value, 'owner', 'long enough password')
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # Atomic: a temp file in the same directory replaced the original.
    assert len(replaced) == 1 and Path(replaced[0][0]).parent == env_file.parent
    assert env_file.stat().st_ino != inode
    assert sorted(p.name for p in env_file.parent.iterdir()) == ['jarvis.env']
    assert value not in out + err and 'Owner' not in out + err and 'long enough password' not in out + err


def test_cli_appends_when_missing_and_keeps_other_bytes(tmp_path):
    path = tmp_path / '.env'
    path.write_text('A=1\n# comment\nB="two"')
    path.chmod(0o600)
    assert run_cli(path)[0] == 0
    text = path.read_text()
    assert text.startswith('A=1\n# comment\nB="two"\nJARVIS_OWNER_LOGIN_HASH=scrypt$') and text.endswith('\n')
    assert run_cli(path)[0] == 0
    assert path.read_text().count('JARVIS_OWNER_LOGIN_HASH=') == 1


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o700, 0o400])
def test_cli_refuses_wrong_mode(env_file, mode):
    env_file.chmod(mode)
    before = env_file.read_bytes()
    code, out, err, prompts = run_cli(env_file)
    env_file.chmod(0o600)
    assert code == 1 and out == '' and 'mode 600' in err and prompts == []
    assert env_file.read_bytes() == before


def test_cli_refuses_symlink(env_file, tmp_path):
    link = tmp_path / 'link.env'
    link.symlink_to(env_file)
    before = env_file.read_bytes()
    code, out, err, _ = run_cli(link)
    assert code == 1 and out == '' and link.is_symlink() and env_file.read_bytes() == before
    with pytest.raises(ValueError):
        owner_login.write_env_hash(link, owner_hash())


@pytest.mark.parametrize('username,passwords,message', [
    ('Owner', ('short', 'short'), 'at least 12'),
    ('Owner', ('long enough password', 'different password!'), 'do not match'),
    ('   ', ('long enough password',) * 2, 'Username'),
])
def test_cli_rejects_bad_input_without_echo(env_file, username, passwords, message):
    before = env_file.read_bytes()
    code, out, err, _ = run_cli(env_file, username=username, passwords=passwords)
    assert code == 1 and out == '' and message in err
    for value in passwords:
        assert value not in err
    assert env_file.read_bytes() == before


def test_cli_write_failure_leaves_file_and_no_temp(env_file, monkeypatch):
    before = env_file.read_bytes()
    def broken(*args):
        raise OSError('disk full')
    monkeypatch.setattr(owner_login.os, 'replace', broken)
    code, out, err, _ = run_cli(env_file)
    assert code == 1 and 'nothing was changed' in err and 'disk full' not in err
    assert env_file.read_bytes() == before
    assert sorted(p.name for p in env_file.parent.iterdir()) == ['jarvis.env']


# --- login page, CSRF and origin ---------------------------------------------------

def test_login_page_fields_and_headers(production):
    client, _ = client_for(production)
    with client:
        response = client.get('/login')
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/html')
    page = response.text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert '<form method="post" action="/login">' in page
    assert re.search(r'<label for="username">.*?<input id="username" name="username"[^>]*autocomplete="username"', page, re.S)
    assert re.search(r'<label for="password">.*?<input id="password" name="password" type="password"[^>]*autocomplete="current-password"', page, re.S)
    assert '<button type="submit">' in page and 'min-height: 48px' in page
    assert re.search(r'<input type="hidden" name="csrf" value="[A-Za-z0-9_-]{43}">', page)
    assert '<script' not in page and 'http://' not in page and 'https://' not in page and '@import' not in page
    assert response.headers['content-security-policy'] == (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
    cookie = response.headers['set-cookie']
    assert cookie.startswith('__Host-jarvis_csrf=' + csrf_from(response) + ';')
    for attribute in ('Path=/', 'Secure', 'HttpOnly', 'SameSite=Strict'):
        assert attribute in cookie
    assert 'Domain' not in cookie
    # Security headers apply to /login too.
    assert response.headers['x-frame-options'] == 'DENY' and response.headers['cache-control'] == 'no-store'
    assert 'www-authenticate' not in response.headers


def test_login_page_host_check(production):
    client, _ = client_for(production)
    with client:
        assert client.get('/login', headers={'Host': 'evil.example'}).status_code == 400
        assert client.post('/login', headers={'Host': 'evil.example', 'Origin': ORIGIN}).status_code == 400


@pytest.mark.parametrize('case', ['missing_field', 'wrong_field', 'missing_cookie', 'swapped'])
def test_login_csrf_required(production, case):
    client, settings = client_for(production)
    with client:
        page = client.get('/login')
        token = csrf_from(page)
        if case == 'missing_cookie':
            client.cookies.clear()
        if case == 'swapped':
            client.cookies.clear()
            client.cookies.set(owner_login.CSRF_COOKIE, secrets.token_urlsafe(32))
        csrf = {'missing_field': '', 'wrong_field': secrets.token_urlsafe(32)}.get(case, token)
        response = submit(client, OWNER_USERNAME, OWNER_PASSWORD, csrf=csrf, fresh=False)
        assert response.status_code == 403 and 'expired' in response.text
        assert owner_login.SESSION_COOKIE not in response.headers.get('set-cookie', '')
        # A CSRF failure is not a password guess.
        assert owner_login.LoginStore(settings.database_path).failures()[0] == 0


@pytest.mark.parametrize('origin', [None, 'https://evil.example', 'null', ORIGIN + '/', 'http://jarvis.example.com'])
def test_login_requires_exact_origin(production, origin):
    client, _ = client_for(production)
    with client:
        response = submit(client, OWNER_USERNAME, OWNER_PASSWORD, origin=origin)
        assert response.status_code == 403
        assert response.json() == {'detail': 'Cross-origin request denied'}
        assert owner_login.SESSION_COOKIE not in response.headers.get('set-cookie', '')


def test_login_rejects_other_methods_and_encodings(production):
    client, _ = client_for(production)
    with client:
        assert client.put('/login', headers={'Origin': ORIGIN}).status_code == 405
        token = csrf_from(client.get('/login'))
        response = client.post('/login', json={'username': OWNER_USERNAME, 'password': OWNER_PASSWORD, 'csrf': token},
                               headers={'Origin': ORIGIN})
        assert response.status_code == 415
        response = client.post('/login', content=b'x' * 9000, headers={
            'Origin': ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded'})
        assert response.status_code == 413


# --- login, sessions, lockout ------------------------------------------------------

def test_correct_login_sets_session_cookie_and_opens_ui(production):
    client, settings = client_for(production)
    with client:
        response = submit(client, OWNER_USERNAME, OWNER_PASSWORD)
        assert response.status_code == 303 and response.headers['location'] == '/'
        cookies = response.headers.get_list('set-cookie')
        session = [c for c in cookies if c.startswith(owner_login.SESSION_COOKIE + '=')]
        assert len(session) == 1
        value = session[0].split(';')[0].split('=', 1)[1]
        assert re.fullmatch(r'[A-Za-z0-9_-]{43}', value)  # 32 random bytes
        for attribute in ('Path=/', 'Secure', 'HttpOnly', 'SameSite=Strict', 'Max-Age=1209600'):
            assert attribute in session[0]
        assert 'domain' not in session[0].lower()
        assert client.get('/', headers={'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}).status_code == 200
        assert client.get('/conversations').status_code == 200
        # Only the sha256 of the session id is stored, with a 14-day expiry and login fingerprint.
        with sqlite3.connect(settings.database_path) as db:
            rows = db.execute('SELECT id_hash, expires_at, login_fingerprint FROM owner_sessions').fetchall()
        import hashlib
        import time
        assert rows[0][0] == hashlib.sha256(value.encode()).hexdigest() and value not in str(rows)
        assert abs(rows[0][1] - (time.time() + 14 * 24 * 3600)) < 60
        assert rows[0][2] == owner_login.fingerprint(owner_hash())


def test_wrong_username_and_wrong_password_get_the_same_response(production):
    client, _ = client_for(production)
    with client:
        wrong_user = submit(client, 'intruder', OWNER_PASSWORD)
        wrong_pass = submit(client, OWNER_USERNAME, 'not the password')
        empty = submit(client, '', '')
    for response in (wrong_user, wrong_pass, empty):
        assert response.status_code == 401
        assert 'Wrong username or password.' in response.text
        assert re.sub(r'value="[^"]+"', '', response.text) == re.sub(r'value="[^"]+"', '', wrong_user.text)
        assert owner_login.SESSION_COOKIE not in response.headers.get('set-cookie', '')
        assert 'www-authenticate' not in response.headers
        assert 'intruder' not in response.text and 'not the password' not in response.text


def test_login_never_logs_credentials(production, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    client, _ = client_for(production)
    with client:
        submit(client, 'logged-user-name', 'logged-password-value')
        submit(client, OWNER_USERNAME, OWNER_PASSWORD)
    text = '\n'.join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for value in ('logged-user-name', 'logged-password-value', OWNER_PASSWORD, owner_hash()):
        assert value not in text


def test_lockout_backoff_persists_and_resets_on_success(production, monkeypatch):
    now = [1_000_000.0]
    checked = []
    real_verify = owner_login.verify
    monkeypatch.setattr(owner_login, 'verify', lambda *a: checked.append(1) or real_verify(*a))

    def locked(client, retry):
        # While locked even the right password is refused, and never checked.
        checked.clear()
        response = submit(client, OWNER_USERNAME, OWNER_PASSWORD)
        assert response.status_code == 429 and response.headers['retry-after'] == retry
        assert response.headers['content-type'].startswith('text/plain') and 'Try again' in response.text
        assert checked == [] and owner_login.SESSION_COOKIE not in response.headers.get('set-cookie', '')

    client, settings = client_for(production)
    with client:
        middleware(client).logins.clock = lambda: now[0]
        for _ in range(5):
            assert submit(client, OWNER_USERNAME, 'wrong password').status_code == 401
        locked(client, '60')
    # The counter survives a restart (new app, same database).
    client, settings = client_for(production)
    with client:
        middleware(client).logins.clock = lambda: now[0]
        locked(client, '60')
        now[0] += 30
        locked(client, '30')
        now[0] += 31
        for _ in range(5):
            assert submit(client, OWNER_USERNAME, 'wrong password').status_code == 401  # failures 6-10
        locked(client, '120')
        now[0] += 121
        for _ in range(5):
            assert submit(client, OWNER_USERNAME, 'wrong password').status_code == 401
        locked(client, '240')
        now[0] += 241
        checked.clear()
        assert submit(client, OWNER_USERNAME, OWNER_PASSWORD).status_code == 303
        assert checked == [1]
        assert owner_login.LoginStore(settings.database_path).failures() == (0, 0.0)
        # After the reset it again takes five failures to lock.
        for _ in range(4):
            assert submit(client, OWNER_USERNAME, 'wrong password').status_code == 401
        assert submit(client, OWNER_USERNAME, OWNER_PASSWORD).status_code == 303


def test_lock_schedule_doubles_up_to_fifteen_minutes():
    assert [owner_login.lock_seconds(n) for n in range(1, 5)] == [0, 0, 0, 0]
    assert [owner_login.lock_seconds(n) for n in (5, 10, 15, 20, 25, 30, 50)] == [60, 120, 240, 480, 900, 900, 900]
    assert owner_login.lock_seconds(6) == 0


def test_session_expires_after_fourteen_days(production):
    client, settings = client_for(production)
    now = [2_000_000.0]
    with client:
        middleware(client).logins.clock = lambda: now[0]
        assert submit(client, OWNER_USERNAME, OWNER_PASSWORD).status_code == 303
        now[0] += 14 * 24 * 3600 - 5
        assert client.get('/conversations').status_code == 200
        now[0] += 10
        assert client.get('/conversations').status_code == 401
        response = client.get('/', headers={'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        assert response.status_code == 303 and response.headers['location'] == '/login'


def test_password_change_logs_out_every_session(production):
    client, settings = client_for(production)
    with client:
        browser_session(client, settings)
        assert client.get('/conversations').status_code == 200
        session = client.cookies[owner_login.SESSION_COOKIE]
    changed = dict(production, JARVIS_OWNER_LOGIN_HASH=owner_login.make_hash(OWNER_USERNAME, 'a brand new password'))
    client, _ = client_for(changed)
    with client:
        client.cookies.set(owner_login.SESSION_COOKIE, session)
        assert client.get('/conversations').status_code == 401
    # Removing the hash entirely also ends cookie sessions; Bearer is unaffected.
    removed = {k: v for k, v in production.items() if k != 'JARVIS_OWNER_LOGIN_HASH'}
    client, _ = client_for(removed)
    with client:
        client.cookies.set(owner_login.SESSION_COOKIE, session)
        assert client.get('/conversations').status_code == 401
        assert submit(client, OWNER_USERNAME, OWNER_PASSWORD).status_code == 503
        assert client.get('/conversations', headers={
            'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']}).status_code == 200


def test_login_without_configured_hash_is_unavailable(production):
    removed = {k: v for k, v in production.items() if k != 'JARVIS_OWNER_LOGIN_HASH'}
    client, settings = client_for(removed)
    with client:
        response = submit(client, OWNER_USERNAME, OWNER_PASSWORD)
        assert response.status_code == 503 and 'not set up' in response.text


def test_forged_or_malformed_session_cookies_fail(production):
    client, _ = client_for(production)
    with client:
        for value in (secrets.token_urlsafe(32), 'x', "' OR 1=1 --", ''):
            client.cookies.set(owner_login.SESSION_COOKIE, value)
            assert client.get('/conversations').status_code == 401


# --- logout ----------------------------------------------------------------------

def test_logout_deletes_session_and_clears_cookie(production):
    client, settings = client_for(production)
    with client:
        assert submit(client, OWNER_USERNAME, OWNER_PASSWORD).status_code == 303
        session = client.cookies[owner_login.SESSION_COOKIE]
        assert client.post('/logout').status_code == 403  # missing Origin
        assert client.post('/logout', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/logout').status_code == 405
        response = client.post('/logout', headers={'Origin': ORIGIN})
        assert response.status_code == 303 and response.headers['location'] == '/login'
        cleared = response.headers['set-cookie']
        assert cleared.startswith(owner_login.SESSION_COOKIE + '=;') and 'Max-Age=0' in cleared
        for attribute in ('Path=/', 'Secure', 'HttpOnly', 'SameSite=Strict'):
            assert attribute in cleared
        with sqlite3.connect(settings.database_path) as db:
            assert db.execute('SELECT count(*) FROM owner_sessions').fetchone()[0] == 0
        # The old cookie value is dead even if a client kept it.
        client.cookies.set(owner_login.SESSION_COOKIE, session)
        assert client.get('/conversations').status_code == 401
        assert client.post('/logout', headers={'Origin': ORIGIN}).status_code == 401


def test_ui_has_a_log_out_button_posting_to_logout():
    page = (Path(main.__file__).parent / 'static/index.html').read_text()
    assert re.search(r'<form method="post" action="/logout"[^>]*>\s*<button type="submit"[^>]*>Log out</button>', page)


# --- middleware boundary -------------------------------------------------------------

def test_bearer_still_works_and_cookie_mutations_need_origin(production):
    client, settings = client_for(production)
    bearer = {'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']}
    with client:
        assert client.get('/conversations', headers=bearer).status_code == 200
        assert client.post('/conversations', headers=bearer).status_code == 200  # tools send no Origin
        assert client.post('/conversations', headers=dict(bearer, Origin='https://evil.example')).status_code == 403
        browser_session(client, settings)
        assert client.get('/conversations').status_code == 200
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            denied = client.request(method, '/conversations')
            assert denied.status_code == 403 and denied.json() == {'detail': 'Cross-origin request denied'}
            assert client.request(method, '/conversations', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/conversations', headers={'Origin': ORIGIN}).status_code == 200


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_unauthenticated_navigation_redirects_but_apis_get_401_json(production, method):
    client, _ = client_for(production)
    with client:
        response = client.request(method, '/', headers={'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        assert response.status_code == 303 and response.headers['location'] == '/login'
        assert 'www-authenticate' not in response.headers
        for path in ('/', '/conversations', '/tasks', '/docs'):
            response = client.request(method, path)
            assert response.status_code == 401 and 'www-authenticate' not in response.headers
            if method == 'GET':
                assert response.json() == {'detail': 'Authentication required'}
        # Navigation to an API route is not redirected.
        assert client.request(method, '/conversations', headers={'Sec-Fetch-Mode': 'navigate'}).status_code == 401
        assert client.post('/', headers={'Sec-Fetch-Mode': 'navigate', 'Origin': ORIGIN}).status_code == 401


def test_health_is_unchanged(production):
    client, _ = client_for(production)
    with client:
        response = client.get('/health', headers={'Host': 'anything.example'})
        assert response.status_code == 200 and response.json() == {'status': 'ok'}
        assert response.text == '{"status":"ok"}' and 'set-cookie' not in response.headers


# --- release tool handover ---------------------------------------------------------

@pytest.fixture
def runtime_env(tmp_path):
    def make(with_hash):
        path = tmp_path / ('with.env' if with_hash else 'without.env')
        text = ('OPENAI_API_KEY=SECRET_PROVIDER\nJARVIS_AUTH_TOKEN=' + 'T' * 40 +
                '\nJARVIS_PUBLIC_ORIGIN=https://jarvis.example.com\n')
        if with_hash:
            text += 'JARVIS_OWNER_LOGIN_HASH=' + owner_hash() + '\n'
        path.write_text(text)
        path.chmod(0o600)
        return DockerRuntime(SimpleNamespace(env_file=path, container='jarvis-jarvis-1', volume='jarvis_state'))
    return make


def info_with(runtime, env):
    info = container_info(runtime)
    info['Config']['Env'] = [k + '=' + v for k, v in {**FIXED_ENV, **env}.items()]
    return info


def test_secrets_reads_optional_hash(runtime_env):
    assert set(runtime_env(False).secrets()) == {'OPENAI_API_KEY', 'JARVIS_AUTH_TOKEN', 'JARVIS_PUBLIC_ORIGIN'}
    assert runtime_env(True).secrets()['JARVIS_OWNER_LOGIN_HASH'] == owner_hash()


def test_secrets_rejects_invalid_or_empty_hash(runtime_env):
    runtime = runtime_env(True)
    path = runtime.config.env_file
    for bad in ('JARVIS_OWNER_LOGIN_HASH=\n', 'JARVIS_OWNER_LOGIN_HASH=scrypt$16$8$1$' + 'A' * 22 + '$' + 'A' * 43 + '\n'):
        text = '\n'.join(l for l in path.read_text().splitlines() if not l.startswith('JARVIS_OWNER')) + '\n' + bad
        path.write_text(text)
        with pytest.raises((ToolDenied, ValueError)):
            runtime.secrets()


def test_current_container_without_hash_passes_precheck_comparison(runtime_env):
    runtime = runtime_env(True)
    base = {k: v for k, v in runtime.secrets().items() if k != 'JARVIS_OWNER_LOGIN_HASH'}
    old = info_with(runtime, base)
    runtime.security(old, 'jarvis_state', 8000)  # current()/restore() handover
    runtime.inspect_container = lambda name: old
    assert runtime.current()['container_id'] == 'old-container'
    with pytest.raises(ToolDenied):
        runtime.security(old, 'jarvis_state', 8000, new=True)
    # The three original keys are still enforced on the running container.
    with pytest.raises(ToolDenied):
        runtime.security(info_with(runtime, dict(base, JARVIS_AUTH_TOKEN='X' * 40)), 'jarvis_state', 8000)


def test_new_container_must_carry_the_file_hash_exactly(runtime_env):
    runtime = runtime_env(True)
    env = runtime.secrets()
    runtime.security(info_with(runtime, env), 'jarvis_state', 8000, new=True)
    other = owner_login.make_hash('someone', 'another password')
    with pytest.raises(ToolDenied):
        runtime.security(info_with(runtime, dict(env, JARVIS_OWNER_LOGIN_HASH=other)), 'jarvis_state', 8000, new=True)
    # Without a hash in the file, a new container must not carry one either.
    plain = runtime_env(False)
    plain.security(info_with(plain, plain.secrets()), 'jarvis_state', 8000, new=True)
    with pytest.raises(ToolDenied):
        plain.security(info_with(plain, dict(plain.secrets(), JARVIS_OWNER_LOGIN_HASH=other)), 'jarvis_state', 8000, new=True)


def test_run_passes_hash_only_when_present_and_checks_new_container(runtime_env, monkeypatch):
    for with_hash in (True, False):
        runtime = runtime_env(with_hash)
        written = []
        def docker(*args, **kwargs):
            if args[0] == 'run':
                written.append(Path(args[args.index('--env-file') + 1]).read_text())
                assert not any('scrypt$' in a for a in args)
            return ''
        monkeypatch.setattr(runtime, 'docker', docker)
        started = info_with(runtime, runtime.secrets())
        started['Image'] = 'sha256:new'
        monkeypatch.setattr(runtime, 'inspect_container', lambda name: started)
        runtime.run('jarvis-jarvis-1', 'sha256:new', 'jarvis_state', 8000, 'release-id')
        assert ('JARVIS_OWNER_LOGIN_HASH=' + owner_hash() + '\n' in written[0]) == with_hash
        assert ('JARVIS_OWNER_LOGIN_HASH' in written[0]) == with_hash
    # A started container that dropped the hash is refused.
    runtime = runtime_env(True)
    monkeypatch.setattr(runtime, 'docker', lambda *a, **k: '')
    base = {k: v for k, v in runtime.secrets().items() if k != 'JARVIS_OWNER_LOGIN_HASH'}
    missing = info_with(runtime, base)
    missing['Image'] = 'sha256:new'
    monkeypatch.setattr(runtime, 'inspect_container', lambda name: missing)
    with pytest.raises(ToolDenied):
        runtime.run('jarvis-jarvis-1', 'sha256:new', 'jarvis_state', 8000, 'release-id')


def test_redact_includes_the_hash(runtime_env):
    runtime = runtime_env(True)
    result = runtime.redact({'message': 'leak ' + owner_hash(), 'origin': 'https://jarvis.example.com'})
    assert result['message'] == 'leak [redacted]' and result['origin'] == 'https://jarvis.example.com'


def test_restore_to_an_older_container_without_the_hash(runtime_env, monkeypatch):
    runtime = runtime_env(True)
    base = {k: v for k, v in runtime.secrets().items() if k != 'JARVIS_OWNER_LOGIN_HASH'}
    previous = info_with(runtime, base)
    previous['Name'] = '/jarvis-previous-abc'
    current = info_with(runtime, runtime.secrets())
    current.update(Id='new-container', Image='sha256:new')
    current['Config']['Labels'] = {'jarvis.release': 'abc'}
    containers = {previous['Id']: previous, 'jarvis-jarvis-1': current}
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'rename':
            previous['Name'] = '/' + args[2]
        return ''
    monkeypatch.setattr(runtime, 'inspect_container', lambda name: containers.get(name))
    monkeypatch.setattr(runtime, 'docker', docker)
    monkeypatch.setattr(runtime, 'cleanup', lambda record: None)
    runtime.restore(dict(id='abc', previous=dict(container_id='old-container', image='sha256:old')))
    assert calls[-1] == ('start', 'old-container')


# --- referrer policy and real browser form POSTs (login-origin-fix) ---------------
# Regression: production sent Referrer-Policy: no-referrer, which makes browsers send
# Origin: null on the login form POST, so the strict origin check refused every login.

LIVE_ORIGIN = "https://shashank-jarvis.duckdns.org"


def test_production_responses_use_same_origin_referrer_policy(production):
    client, _ = client_for(production)
    with client:
        for path in ("/login", "/"):
            assert client.get(path).headers["referrer-policy"] == "same-origin"


def live_client(production):
    settings = Settings.from_env(dict(production, JARVIS_PUBLIC_ORIGIN=LIVE_ORIGIN))
    return TestClient(main.create_app(settings=settings), base_url=LIVE_ORIGIN, follow_redirects=False)


def browser_post(client, origin):
    page = client.get("/login")
    headers = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
               "Content-Type": "application/x-www-form-urlencoded"}
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/login", headers=headers,
                       data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD, "csrf": csrf_from(page)})


def test_real_browser_login_post_passes_origin_check(production):
    with live_client(production) as client:
        response = browser_post(client, LIVE_ORIGIN)
        assert response.status_code == 303 and response.headers["location"] == "/"


@pytest.mark.parametrize("origin", ["null", None])
def test_real_browser_login_post_with_null_origin_still_refused(production, origin):
    with live_client(production) as client:
        response = browser_post(client, origin)
        assert response.status_code == 403 and response.json() == {"detail": "Cross-origin request denied"}
