"""Production perimeter, persistence, and immutable-container deployment contracts."""
import base64
import json
import logging
import os
from pathlib import Path
import functools
import secrets

from fastapi.testclient import TestClient
import pytest

import main
from core.candidate_store import CandidateStore
from core.deployment import Settings, JsonFormatter
from core import owner_login
from tools.candidate_edit import CandidatePolicy
from tools import ToolDenied
from test_self_edit import setup


OWNER_USERNAME, OWNER_PASSWORD = 'Owner', 'correct horse battery'


@functools.cache
def owner_hash():
    # Real production-grade parameters (n=2**15, r=8, p=1), computed once.
    return owner_login.make_hash(OWNER_USERNAME, OWNER_PASSWORD)


@pytest.fixture
def production(tmp_path):
    return {'JARVIS_MODE': 'production', 'OPENAI_API_KEY': 'test-provider-key',
            'JARVIS_AUTH_TOKEN': secrets.token_urlsafe(32),
            'JARVIS_OWNER_LOGIN_HASH': owner_hash(),
            'JARVIS_PUBLIC_ORIGIN': 'https://jarvis.example.com',
            'JARVIS_STATE_DIR': str(tmp_path / 'state'),
            'JARVIS_DB_PATH': str(tmp_path / 'state/jarvis.sqlite3')}


def browser_session(client, settings):
    """Give a TestClient a real server-side owner session (as after /login)."""
    client.cookies.set(owner_login.SESSION_COOKIE,
                       owner_login.LoginStore(settings.database_path).succeed(settings.owner_login_hash))


@pytest.mark.parametrize('field,value', [
    ('JARVIS_MODE', 'prod'), ('JARVIS_PORT', 'abc'), ('JARVIS_PORT', '0'),
    ('JARVIS_PORT', '65536'), ('JARVIS_HOST', ''),
    ('OPENAI_API_KEY', ''), ('JARVIS_AUTH_TOKEN', ''), ('JARVIS_AUTH_TOKEN', 'short'),
    ('JARVIS_AUTH_TOKEN', ' ' * 40), ('JARVIS_PUBLIC_ORIGIN', ''),
    ('JARVIS_PUBLIC_ORIGIN', 'http://jarvis.example.com'),
    ('JARVIS_PUBLIC_ORIGIN', 'https://user:pass@jarvis.example.com'),
    ('JARVIS_PUBLIC_ORIGIN', 'https://jarvis.example.com/path'),
    ('JARVIS_DB_PATH', ':memory:'), ('JARVIS_DB_PATH', 'state.db'),
    ('JARVIS_DB_PATH', '/elsewhere/jarvis.db'), ('JARVIS_STATE_DIR', 'relative')])
def test_production_config_validation(production, field, value):
    with pytest.raises(ValueError):
        Settings.from_env(dict(production, **{field: value}))


def test_production_secrets_not_in_settings_repr(production):
    settings = Settings.from_env(production)
    assert production['JARVIS_AUTH_TOKEN'] not in repr(settings)
    assert production['OPENAI_API_KEY'] not in repr(settings)
    assert settings.database_path.parent == settings.state_dir
    assert Settings.from_env({}).mode == 'development'


@pytest.mark.parametrize('path', ['/', '/tasks', '/activity', '/runtime', '/conversations', '/memories?scope=user&scope_id=x',
                                  '/candidates/00000000-0000-0000-0000-000000000000/apply', '/docs', '/openapi.json'])
def test_all_sensitive_routes_require_authentication(production, path):
    with TestClient(main.create_app(settings=Settings.from_env(production)), base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {'detail': 'Authentication required'}
        assert 'www-authenticate' not in response.headers
        assert response.headers['cache-control'] == 'no-store'
        assert client.get(path, headers={'Authorization': 'Bearer wrong'}).status_code == 401


@pytest.mark.parametrize('header', ['Basic invalid', 'Basic /w==', 'Basic ' + base64.b64encode(b'wrong:wrong').decode(),
                                    'Bearer', 'Bearer ', 'Token abc', 'basic'])
def test_malformed_auth_fails_closed(production, header):
    with TestClient(main.create_app(settings=Settings.from_env(production)), base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        response = client.get('/', headers={'Authorization': header})
        assert response.status_code == 401 and 'www-authenticate' not in response.headers


def test_http_basic_is_removed_even_with_the_right_token(production):
    # The old popup login (username jarvis, password = token) no longer authenticates.
    settings = Settings.from_env(production)
    basic = 'Basic ' + base64.b64encode(('jarvis:' + production['JARVIS_AUTH_TOKEN']).encode()).decode()
    with TestClient(main.create_app(settings=settings), base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        for path in ('/', '/tasks', '/conversations'):
            response = client.get(path, headers={'Authorization': basic})
            assert response.status_code == 401 and 'www-authenticate' not in response.headers
        # A valid session cannot rescue a request that also carries a bad Authorization header.
        browser_session(client, settings)
        assert client.get('/tasks', headers={'Authorization': basic}).status_code == 401
        assert client.get('/tasks').status_code == 200


def test_session_browser_and_bearer_api_auth_and_csrf(production):
    settings = Settings.from_env(production)
    with TestClient(main.create_app(settings=settings), base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        browser_session(client, settings)
        assert client.get('/').status_code == 200
        client.cookies.clear()
        headers = {'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']}
        assert client.get('/tasks', headers=headers).status_code == 200
        assert client.post('/conversations', headers=headers).status_code == 200
        assert client.post('/conversations', headers=dict(headers, Origin='https://evil.example')).status_code == 403
        assert client.get('/tasks', headers=dict(headers, **{'Sec-Fetch-Site': 'cross-site'})).status_code == 403
        assert client.get('/tasks', headers=dict(headers, Host='evil.example')).status_code == 400
        assert client.post('/conversations', headers=dict(headers, Origin=production['JARVIS_PUBLIC_ORIGIN'])).status_code == 200
        assert client.post('/candidates', json={}).status_code == 401
        assert client.post('/candidates/00000000-0000-0000-0000-000000000000/apply', json={}).status_code == 401


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_cross_site_top_level_ui_navigation_after_login(production, method):
    headers = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
               'Sec-Fetch-Dest': 'document'}
    settings = Settings.from_env(production)
    with TestClient(main.create_app(settings=settings), follow_redirects=False,
                    base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        unauthenticated = client.request(method, '/', headers=headers)
        assert unauthenticated.status_code == 303 and unauthenticated.headers['location'] == '/login'
        client.cookies.set(owner_login.SESSION_COOKIE, secrets.token_urlsafe(32))
        assert client.request(method, '/', headers=headers).status_code == 303
        browser_session(client, settings)
        assert client.request(method, '/', headers={**headers, 'Host': 'evil.example'}).status_code == 400
        response = client.request(method, '/', headers=headers)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/html')
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-frame-options'] == 'DENY'
        if method == 'HEAD':
            assert response.content == b''


@pytest.mark.parametrize('method,path', [('GET', '/'), ('GET', '/tasks'), ('POST', '/conversations')])
def test_authenticated_same_origin_browser_requests(production, method, path):
    headers = {'Origin': production['JARVIS_PUBLIC_ORIGIN'], 'Sec-Fetch-Site': 'same-origin',
               'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}
    settings = Settings.from_env(production)
    with TestClient(main.create_app(settings=settings),
                    base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        browser_session(client, settings)
        assert client.request(method, path, headers=headers).status_code == 200


@pytest.mark.parametrize('method,path,metadata', [
    # A forged navigation tuple cannot exempt mutations or API reads.
    ('POST', '/conversations', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
    ('POST', '/', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
    ('PUT', '/', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
    ('PATCH', '/', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
    ('DELETE', '/', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
    ('GET', '/tasks', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
    ('HEAD', '/tasks', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}),
    ('GET', '/', {'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}),
    ('HEAD', '/', {'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}),
    ('POST', '/conversations', {'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}),
    ('POST', '/conversations', {'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'empty'}),
    ('GET', '/', {'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'image'}),
    ('GET', '/', {'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'document'}),
    ('GET', '/', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'empty'}),
    ('GET', '/', {'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'iframe'}),
    ('GET', '/', {'Sec-Fetch-Mode': 'navigate'}),
    ('GET', '/', {'Sec-Fetch-Dest': 'document'}),
    ('GET', '/', {}),
    # Even an exact Origin cannot override explicit cross-site mutation metadata.
    ('POST', '/conversations', {'Origin': 'https://jarvis.example.com'}),
])
def test_cross_site_unsafe_or_non_navigation_requests_are_blocked(production, method, path, metadata):
    headers = {'Sec-Fetch-Site': 'cross-site', **metadata}
    settings = Settings.from_env(production)
    with TestClient(main.create_app(settings=settings),
                    base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        browser_session(client, settings)
        response = client.request(method, path, headers=headers)
        assert response.status_code == 403
        if method == 'HEAD':
            assert response.content == b''
        else:
            assert response.json() == {'detail': 'Cross-origin request denied'}


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'https://jarvis.example.com/'])
@pytest.mark.parametrize('method,path,site', [('POST', '/conversations', 'same-origin'),
                                             ('GET', '/', 'cross-site'),
                                             ('HEAD', '/', 'cross-site')])
def test_explicit_mismatched_origin_cannot_be_overridden_by_fetch_metadata(production, origin, method, path, site):
    headers = {'Origin': origin, 'Sec-Fetch-Site': site,
               'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
    settings = Settings.from_env(production)
    with TestClient(main.create_app(settings=settings),
                    base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        browser_session(client, settings)
        assert client.request(method, path, headers=headers).status_code == 403


def test_health_is_minimal_and_checks_database(production, monkeypatch):
    application = main.create_app(settings=Settings.from_env(production))
    with TestClient(application) as client:
        assert client.get('/health').json() == {'status': 'ok'}
        def unavailable():
            raise OSError('sensitive database details')
        monkeypatch.setattr(application.state.store.database, 'transaction', unavailable)
        response = client.get('/health')
        assert response.status_code == 503
        assert 'sensitive' not in response.text


def test_runtime_reports_production_mode(production):
    application = main.create_app(settings=Settings.from_env(production))
    with TestClient(application, base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        response = client.get('/runtime', headers={'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']})
        assert response.json() == {'mode': 'production'}


def test_restart_keeps_all_persistent_records_and_closes_db(production):
    settings = Settings.from_env(production)
    first = main.create_app(settings=settings)
    with TestClient(first):
        store = first.state.store
        task = store.create_task('Persistent goal')
        store.add_activity(task['id'], 'Persistent event', 'test')
        store.add_assistant_message(task['id'], 'Persistent reply')
        memory = store.memory.create_memory(scope='user', scope_id='owner', content='Persistent memory')
        candidates = CandidateStore(store.database)
        record = candidates.create('candidate goal', ['docs/self-edit.md'], ['tests'])
        record['apply'] = {'status': 'denied', 'error': 'test audit'}
        candidates.save(record, 'audit_saved')
    assert first.state.store.database._connection is None
    second = main.create_app(settings=settings)
    with TestClient(second):
        store = second.state.store
        assert store.get_task(task['id'])['goal'] == 'Persistent goal'
        assert store.memory.get_conversation(task['conversation_id'])
        assert store.memory.conversation_history(task['conversation_id'])[-1]['content'] == 'Persistent reply'
        assert store.memory.get_memory(memory['id'])['content'] == 'Persistent memory'
        recovered = CandidateStore(store.database).get(record['id'])
        assert recovered['apply']['status'] == 'denied' and recovered['events'][0]['event'] == 'audit_saved'
        with store.database.transaction() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 4
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_unsafe_storage_and_startup_failure(production, tmp_path, monkeypatch):
    root = Path(main.__file__).parent
    with pytest.raises(ValueError):
        Settings.from_env(dict(production, JARVIS_STATE_DIR=str(root), JARVIS_DB_PATH=str(root / 'state.db')))
    settings = Settings.from_env(production)
    settings.state_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((ValueError, RuntimeError)):
        with TestClient(main.create_app(settings=settings)):
            pass


def test_startup_requires_writable_state(production, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('read-only volume')
    monkeypatch.setattr('core.deployment.tempfile.TemporaryFile', denied)
    with pytest.raises(PermissionError):
        with TestClient(main.create_app(settings=Settings.from_env(production))):
            pass


def test_startup_rejects_corrupt_database(production):
    settings = Settings.from_env(production)
    settings.state_dir.mkdir()
    settings.database_path.write_bytes(b'not a sqlite database')
    with pytest.raises(Exception, match='not a database'):
        with TestClient(main.create_app(settings=settings)):
            pass


def test_container_paths_and_deployment_controls_remain_protected():
    from core.engineering_authority import assess_action
    root = Path(main.__file__).parent
    # Deployment controls are editable only inside an isolated candidate;
    # applying or promoting them always needs owner approval.
    for path in ('core/deployment.py', 'core/deployment_server.py', 'core/deployment_healthcheck.py'):
        CandidatePolicy().edit_path(path)
        assert assess_action('candidate_apply', phase='prepare', changed_paths=[path]).allowed
        decision = assess_action('candidate_apply', changed_paths=[path])
        assert decision.approval_required and 'authority_expansion' in decision.escalation
    # Container build files stay outside the candidate edit scope entirely, and
    # would be major_change if any other path ever applied them.
    for path in ('Dockerfile', 'compose.yaml'):
        with pytest.raises(ToolDenied):
            CandidatePolicy().edit_path(path)
        assert 'major_change' in assess_action('candidate_apply', changed_paths=[path]).escalation
    assert assess_action('production_promote').escalation == ['major_change']
    for path in ('../var/lib/jarvis/jarvis.sqlite3', '/var/lib/jarvis', 'core/../../var/lib/jarvis'):
        with pytest.raises(ToolDenied):
            CandidatePolicy().edit_path(path)
    dockerfile = (root / 'Dockerfile').read_text()
    compose = (root / 'compose.yaml').read_text()
    assert 'USER 10001:10001' in dockerfile
    assert 'COPY . ' not in dockerfile and 'COPY .git' not in dockerfile
    assert 'state:/var/lib/jarvis' in compose and 'read_only: true' in compose
    assert 'docker.sock' not in compose and 'privileged:' not in compose
    assert '127.0.0.1:' in compose and 'no-new-privileges:true' in compose


def test_structured_request_logs_omit_secrets(production, caplog):
    caplog.set_level(logging.INFO, logger='jarvis.http')
    with TestClient(main.create_app(settings=Settings.from_env(production)), base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        client.get('/tasks?private=query-secret', headers={'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']})
    records = [r for r in caplog.records if r.name == 'jarvis.http']
    data = json.loads(JsonFormatter().format(records[-1]))
    assert data['route'] == '/tasks' and data['status'] == 200
    assert 'query-secret' not in json.dumps(data) and production['JARVIS_AUTH_TOKEN'] not in json.dumps(data)


def test_server_graceful_shutdown_and_no_proxy_header_trust(monkeypatch):
    from core import deployment_server
    monkeypatch.setattr(deployment_server.Settings, 'from_env', lambda: Settings())
    monkeypatch.setattr(deployment_server, 'configure_logging', lambda: None)
    monkeypatch.setattr(deployment_server.os, 'umask', lambda value: None)
    calls = []
    monkeypatch.setattr(deployment_server.uvicorn, 'run', lambda *args, **kwargs: calls.append(kwargs))
    deployment_server.main()
    assert calls[0]['workers'] == 1 and calls[0]['timeout_graceful_shutdown'] == 360
    assert not calls[0]['proxy_headers'] and not calls[0]['reload']


@pytest.mark.parametrize('field,value', [
    ('JARVIS_HOST', 'http://localhost'), ('JARVIS_HOST', 'bad host'),
    ('JARVIS_AUTH_TOKEN', 'a' * 31 + '\x00'),
    ('JARVIS_PUBLIC_ORIGIN', 'https://host:bad'),
    ('JARVIS_PUBLIC_ORIGIN', 'https://host:0'),
    ('JARVIS_PUBLIC_ORIGIN', 'https://bad host'),
    ('JARVIS_PUBLIC_ORIGIN', 'https://host\\other'),
    ('JARVIS_STATE_DIR', '/')])
def test_additional_invalid_production_configuration(production, field, value):
    with pytest.raises(ValueError):
        Settings.from_env(dict(production, **{field: value}))


def test_production_client_does_not_load_dotenv(monkeypatch):
    import sys
    from types import SimpleNamespace
    from models import openai_client
    monkeypatch.setenv('JARVIS_MODE', 'production')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-provider-key')
    monkeypatch.setitem(sys.modules, 'dotenv', SimpleNamespace(
        load_dotenv=lambda: pytest.fail('Production loaded local .env')))
    sentinel = object()
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=lambda **kwargs: sentinel))
    openai_client.get_client.cache_clear()
    try:
        assert openai_client.get_client() is sentinel
    finally:
        openai_client.get_client.cache_clear()


def test_container_without_git_fails_closed_before_candidate_code(setup):
    # A container-like immutable application directory intentionally has no Git
    # metadata. Never invent a baseline or fall back to unsandboxed execution.
    from core.self_edit import SelfEditService
    source, database, _ = setup
    image_source = source.parent / 'image'
    (image_source / 'docs').mkdir(parents=True)
    (image_source / 'docs/note.md').write_text('image content\n')
    service = SelfEditService(database, source_root=image_source,
        candidate_root=source.parent / 'persistent-candidates',
        model=lambda *args: pytest.fail('Missing baseline must not invoke a model'),
        tester=lambda *args: pytest.fail('Missing baseline must not invoke tests'))
    result = service.start('Update docs', ['docs/note.md'], ['tests'])
    assert result['state'] == 'rejected'
    assert result['error'] == 'Source requires a standalone Git checkout'
    assert (image_source / 'docs/note.md').read_text() == 'image content\n'


def test_health_probe_uses_loopback_and_ignores_proxy_env(monkeypatch):
    from core import deployment_healthcheck
    calls = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Opener:
        def open(self, url, timeout):
            calls.append((url, timeout))
            return Response()
    def build(handler):
        assert handler.proxies == {}
        return Opener()
    monkeypatch.setattr(deployment_healthcheck.urllib.request, 'build_opener', build)
    monkeypatch.setenv('JARVIS_PORT', '8765')
    deployment_healthcheck.main()
    assert calls == [('http://127.0.0.1:8765/health', 3)]


def test_docker_context_excludes_local_credentials():
    root = Path(main.__file__).parent
    ignore = (root / '.dockerignore').read_text().splitlines()
    assert '.env' in ignore and '**/.env*' in ignore and '.git' in ignore
    dockerfile = (root / 'Dockerfile').read_text()
    for line in dockerfile.splitlines():
        if line.startswith(('ARG ', 'ENV ', 'COPY ')):
            assert 'OPENAI_API_KEY' not in line and 'JARVIS_AUTH_TOKEN' not in line
            assert '.env' not in line


@pytest.mark.skipif(Path.cwd() == Path('/workspace'), reason='Network process smoke test runs outside the no-network candidate sandbox')
def test_real_server_health_auth_shutdown_and_process_restart(production, tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    environment = dict(os.environ, **production)
    environment.update(JARVIS_PUBLIC_ORIGIN=origin, JARVIS_HOST='127.0.0.1', JARVIS_PORT=str(port))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, *, authenticated=True, data=None):
        headers = {'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']} if authenticated else {}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(origin + path, headers=headers,
            data=json.dumps(data).encode() if data is not None else None)
        with opener.open(req, timeout=3) as response:
            return json.load(response)
    conversation = None
    for iteration in range(2):
        log_path = tmp_path / f'server-{iteration}.log'
        with log_path.open('w') as output:
            process = subprocess.Popen([sys.executable, '-m', 'core.deployment_server'],
                cwd=Path(main.__file__).parent, env=environment, stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        assert request('/health', authenticated=False) == {'status': 'ok'}
                        break
                    except (urllib.error.URLError, OSError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            pytest.fail('Server did not become healthy')
                        time.sleep(0.05)
                with pytest.raises(urllib.error.HTTPError) as denied:
                    request('/', authenticated=False)
                assert denied.value.code == 401
                if iteration == 0:
                    conversation = request('/conversations', data={})['id']
                else:
                    assert request('/conversations/' + conversation)['id'] == conversation
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    pytest.fail('Server did not shut down gracefully')
        # Uvicorn re-raises the received signal after completing lifespan drain.
        assert process.returncode in {0, -15}
        logs = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert any(entry['event'] == 'shutdown_complete' for entry in logs)
        assert production['JARVIS_AUTH_TOKEN'] not in log_path.read_text()


def test_candidate_snapshot_provisions_only_verified_deployment_dependencies(tmp_path):
    from core.self_edit import CandidateRepository
    from tools import RepositoryTools
    root = Path(main.__file__).parent
    expected = RepositoryTools(root)._runtime_deployment_files()
    assert set(expected) == {'core/deployment.py', 'core/deployment_server.py',
                             'core/deployment_healthcheck.py', 'tests/test_deployment.py',
                             'core/deployment_release.py', 'core/deployment_runtime.py',
                             'core/deployment_tools.py', 'tests/test_deployment_release.py',
                             'core/deployment_status.py', 'tests/test_deployment_status.py'}
    worktree = tmp_path / 'worktree'
    for name, data in expected.items():
        path = worktree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    # A candidate tests its own (possibly edited) copies in the sandbox; the host
    # never injects trusted runtime bytes into it, and nothing reaches the host.
    repository = CandidateRepository(worktree, source_root=root, check=lambda: 30)
    assert repository._runtime_deployment_files() == {}
    for name, data in expected.items():
        assert repository.read_text(name).encode() == data
    (worktree / 'core/deployment.py').write_text('# modified deployment perimeter\n')
    assert repository.read_text('core/deployment.py') == '# modified deployment perimeter\n'
    assert (root / 'core/deployment.py').read_bytes() == expected['core/deployment.py']
    from core.engineering_authority import assess_action
    decision = assess_action('candidate_apply', changed_paths=list(expected))
    assert decision.approval_required and 'authority_expansion' in decision.escalation
    assert CandidateRepository(worktree, source_root=tmp_path, check=lambda: 30)._runtime_deployment_files() == {}
    # Non-root repositories still receive no deployment dependencies.
    assert RepositoryTools(worktree)._runtime_deployment_files() == {}


def test_candidate_deployment_dependency_links_are_denied(tmp_path):
    from core.self_edit import CandidateRepository
    from tools import RepositoryTools
    root = Path(main.__file__).parent
    for name, data in RepositoryTools(root)._runtime_deployment_files().items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    path = tmp_path / 'core/deployment.py'
    path.unlink()
    path.symlink_to(root / 'core/deployment.py')
    repository = CandidateRepository(tmp_path, source_root=root, check=lambda: 30)
    # Linked deployment files are never read, snapshotted or followed to the host.
    with pytest.raises(ToolDenied):
        repository.read_text('core/deployment.py')
    with pytest.raises(ToolDenied):
        repository._read_bytes('core/deployment.py')
    # Hardlink a tmp-local copy; never link (and so mutate nlink of) the source tree.
    outside = tmp_path / 'outside_deployment_server.py'
    hard = tmp_path / 'core/deployment_server.py'
    hard.rename(outside)
    os.link(outside, hard)
    with pytest.raises(ToolDenied):
        repository.read_text('core/deployment_server.py')
