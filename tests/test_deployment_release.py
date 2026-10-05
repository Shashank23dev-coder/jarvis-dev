"""Release transaction and Docker contracts: no access to production or real daemon."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3

import pytest
from fastapi.testclient import TestClient

from core.deployment_release import ReleaseConfig, ReleaseService, approval, approved_commit, now
from core.deployment_runtime import DOCKER, DockerRuntime, FIXED_ENV
from core.deployment_tools import DeploymentTools
from tools import ToolDenied, ToolRegistry


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class Runtime:
    """Stateful adapter models containers sharing an independently retained volume."""
    def __init__(self):
        self.current_value = dict(container_id='old-container', image='sha256:old', revision=None)
        self.previous = copy.deepcopy(self.current_value)
        self.state = {'conversation': 'persistent user data'}
        self.images = {'sha256:old'}
        self.calls = []
        self.fail = None

    def current(self):
        return copy.deepcopy(self.current_value)

    def redact(self, result):
        return result

    def assert_current(self, previous):
        assert previous == self.current_value

    def image(self, sha):
        return 'sha256:new' if 'sha256:new' in self.images else None

    def build(self, sha, context):
        assert (context / 'main.py').read_text() == 'print("committed")\n'
        assert not (context / '.git').exists() and not (context / '.env').exists()
        self.images.add('sha256:new')
        self.calls.append('build')
        return 'sha256:new'

    def verify(self, name, port, *, public=False, baseline=None):
        self.calls.append('verify')
        if self.fail in {'health', 'smoke'} and self.current_value['image'] == 'sha256:new':
            raise RuntimeError('SECRET diagnostic')
        if self.fail == 'rollback' and 'restore' in self.calls:
            raise RuntimeError('SECRET diagnostic')
        assert not baseline or baseline == ['conversation-hash']
        assert self.state == {'conversation': 'persistent user data'}
        with sqlite3.connect(self.database) as db:
            assert db.execute('SELECT * FROM conversations').fetchall() == [('conversation', 'persistent user data')]
        return ['conversation-hash']

    def precheck(self, record):
        self.calls.append('precheck')
        if self.fail == 'precheck':
            raise RuntimeError('SECRET precheck output')

    def switch(self, record):
        self.calls.append('switch')
        self.current_value = dict(container_id='new-container', image=record['image'], revision=record['commit'])
        if self.fail in {'startup', 'rollback'}:
            raise RuntimeError('SECRET startup output')

    def restore(self, record):
        self.calls.append('restore')
        self.current_value = copy.deepcopy(self.previous)

    def cleanup(self, record):
        self.calls.append('cleanup')


@pytest.fixture
def release(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    git(root, 'init')
    git(root, 'config', 'user.email', 'test@localhost')
    git(root, 'config', 'user.name', 'Test')
    for name in ('Dockerfile', 'compose.yaml', '.dockerignore', 'requirements.txt'):
        (root / name).write_text('fixture\n')
    (root / 'main.py').write_text('print("committed")\n')
    (root / 'core').mkdir()
    (root / 'core/database.py').write_text('# database fixture\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Reviewed release')
    sha = git(root, 'rev-parse', 'HEAD')
    runtime = Runtime()
    # State is independent of both container identities and the Git checkout.
    runtime.database = tmp_path / 'persistent.sqlite3'
    with sqlite3.connect(runtime.database) as db:
        db.execute('CREATE TABLE conversations (id TEXT PRIMARY KEY, content TEXT)')
        db.execute('INSERT INTO conversations VALUES (?, ?)', ('conversation', 'persistent user data'))
    service = ReleaseService(ReleaseConfig(str(root), str(tmp_path / 'journal'), str(tmp_path / '.env')), runtime)
    with service.lock():
        data = service.load()
        data['evidence'][sha] = dict(commit=sha, at=now(), passed=True,
                                      tests=[dict(phase='python', passed=True), dict(phase='ui', passed=True)],
                                      review='Automated context check; human review at approval')
        service.save(data)
    return service, runtime, sha


@pytest.mark.parametrize('message', ['yes', 'deploy it', 'approved', 'Approve and deploy commit {sha} to production.',
                                      'Please Approve and deploy commit {sha} to production',
                                      'Earlier user said: Approve and deploy commit {sha} to production',
                                      'Approve and deploy commit {sha} to production\nDo not deploy'])
def test_exact_approval_required(release, message):
    service, runtime, sha = release
    with pytest.raises(ToolDenied):
        service.deploy(sha, message.format(sha=sha))
    assert not runtime.calls
    assert approved_commit(approval(sha)) == sha


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'staged'])
def test_dirty_tree_rejected(release, change):
    service, runtime, sha = release
    root = service.config.source
    (root / ('main.py' if change == 'tracked' else 'new.txt')).write_text('dirty')
    if change == 'staged':
        git(root, 'add', '.')
    with pytest.raises(ToolDenied, match='clean'):
        service.deploy(sha, approval(sha))
    assert not runtime.calls


@pytest.mark.parametrize('sha', ['HEAD', 'a' * 40, '0' * 39, 'A' * 40])
def test_nonexistent_or_abbreviated_commit_rejected(release, sha):
    service, runtime, _ = release
    with pytest.raises(ToolDenied):
        service.preview(sha)
    assert not runtime.calls


def test_stale_and_wrong_commit_approval(release):
    service, runtime, sha = release
    git(service.config.source, 'commit', '--allow-empty', '-m', 'New HEAD')
    new = git(service.config.source, 'rev-parse', 'HEAD')
    with pytest.raises(ToolDenied, match='stale'):
        service.deploy(sha, approval(sha))
    with pytest.raises(ToolDenied, match='approval'):
        service.deploy(new, approval(sha))
    assert not runtime.calls


def test_preparation_without_production_config_or_runtime_access(release, monkeypatch):
    from core.deployment_release import preparation_service
    service, runtime, sha = release
    monkeypatch.delenv('JARVIS_DEPLOY_CONFIG', raising=False)
    prepared = preparation_service(source=service.config.source, state=service.config.journal.parent)
    assert prepared.config.source == service.config.source
    # Existing fresh evidence can be reused; no runtime queries are needed.
    result = service.prepare()
    assert result['prepared'] and result['commit'] == sha and not runtime.calls


@pytest.mark.parametrize('code,timed_out,kind', [(1, False, 'test_failure'),
    (2, False, 'test_environment'), (-9, True, 'environment_timeout')])
def test_validation_failure_is_test_or_harness_evidence(release, monkeypatch, code, timed_out, kind):
    from tools import CommandResult
    service, runtime, sha = release
    monkeypatch.setattr('tools.sandbox.run_tests', lambda *args, **kwargs:
        CommandResult(('pytest',), code, 'fixture failure', timed_out=timed_out))
    result = service.validate(sha)
    assert not result['passed'] and {t['failure_kind'] for t in result['tests']} == {kind}
    assert service.load()['evidence'][sha] == result
    assert not runtime.calls


@pytest.mark.parametrize('failure', ['fixture', 'backend'])
def test_broken_harness_is_distinct_from_security_denial(release, monkeypatch, failure):
    from tools import CommandResult
    service, runtime, sha = release
    def run(*args, **kwargs):
        if failure == 'backend':
            raise ToolDenied('Sandbox backend unavailable')
        return CommandResult(('pytest',), 1, 'ERROR at setup of test_fixture')
    monkeypatch.setattr('tools.sandbox.run_tests', run)
    result = service.validate(sha)
    assert not result['passed']
    assert {r['failure_kind'] for r in result['tests']} == {
        'test_environment' if failure == 'backend' else 'test_fixture_failure'}
    assert not runtime.calls


def test_concurrent_deployment_rejected(release):
    service, runtime, sha = release
    other = ReleaseService(service.config, runtime)
    with service.lock(), pytest.raises(ToolDenied, match='active'):
        other.deploy(sha, approval(sha))
    assert not runtime.calls


def test_success_persistence_previous_image_audit_and_idempotency(release):
    service, runtime, sha = release
    preview = service.preview(sha)
    assert preview['eligible'] and preview['changed_files'] is None
    assert preview['commit'] == sha and preview['message'] == 'Reviewed release'
    assert preview['required_confirmation'] == approval(sha)
    result = service.deploy(sha, approval(sha))
    assert result['status'] == 'deployed'
    assert [r['phase'] for r in result['verification']] == ['previous', 'pre_switch', 'post_switch']
    assert runtime.state == {'conversation': 'persistent user data'}
    assert runtime.images == {'sha256:old', 'sha256:new'}
    assert result['approval'] == approval(sha) and result['previous'] == runtime.previous
    assert service.inspect(result['id']) == result
    assert service.history() == [result]
    assert service.deploy(sha, approval(sha)) == result
    assert runtime.calls.count('build') == runtime.calls.count('switch') == 1
    with sqlite3.connect(runtime.database) as db:
        assert db.execute('SELECT * FROM conversations').fetchall() == [('conversation', 'persistent user data')]


@pytest.mark.parametrize('failure', ['health', 'smoke', 'startup'])
def test_failure_restores_previous_and_preserves_state(release, failure):
    service, runtime, sha = release
    runtime.fail = failure
    result = service.deploy(sha, approval(sha))
    assert result['status'] == 'rolled_back'
    assert result['rollback']['verified']
    assert runtime.current_value == runtime.previous
    assert runtime.state == {'conversation': 'persistent user data'}
    assert 'sha256:old' in runtime.images
    assert runtime.calls[-2:] == ['restore', 'verify']
    assert 'SECRET' not in json.dumps(service.history())
    with sqlite3.connect(runtime.database) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT * FROM conversations').fetchall() == [('conversation', 'persistent user data')]


def test_failed_rollback_blocks_further_mutations(release):
    service, runtime, sha = release
    runtime.fail = 'rollback'
    result = service.deploy(sha, approval(sha))
    assert result['status'] == 'rollback_failed' and not result['rollback']['verified']
    with pytest.raises(ToolDenied, match='reconciliation'):
        service.deploy(sha, approval(sha))


def test_precheck_failure_does_not_stop_production(release):
    service, runtime, sha = release
    runtime.fail = 'precheck'
    assert service.deploy(sha, approval(sha))['status'] == 'failed'
    assert 'switch' not in runtime.calls and 'restore' not in runtime.calls


def test_interrupted_switch_recovers_only_with_exact_original_approval(release):
    service, runtime, sha = release
    def crash(record):
        Runtime.switch(runtime, record)
        raise KeyboardInterrupt()
    runtime.switch = crash
    with pytest.raises(KeyboardInterrupt):
        service.deploy(sha, approval(sha))
    assert service.history()[0]['status'] == 'switching'
    before = runtime.calls[:]
    service.inspect(service.history()[0]['id'])
    service.history()
    assert runtime.calls == before  # Reads never recover.
    with pytest.raises(ToolDenied):
        service.deploy('b' * 40, approval('b' * 40))
    recovered = ReleaseService(service.config, runtime).deploy(sha, approval(sha))
    assert recovered['status'] == 'rolled_back' and recovered['rollback']['verified']


def test_expired_evidence_denied(release):
    service, runtime, sha = release
    with service.lock():
        data = service.load()
        data['evidence'][sha]['at'] = '2000-01-01T00:00:00+00:00'
        service.save(data)
    assert not service.preview(sha)['eligible']
    with pytest.raises(ToolDenied, match='validation'):
        service.deploy(sha, approval(sha))


def test_git_context_excludes_secrets_state_and_links(release, tmp_path):
    service, _, sha = release
    root = service.config.source
    for name in ('.env', 'core/.env', 'core/private_key', 'core/state/test.py', 'core/data.sqlite3',
                 'core/credentials.json', '.ssh/id_rsa', 'sandbox/local.py'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('DO_NOT_COPY')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Fixtures')
    sha = git(root, 'rev-parse', 'HEAD')
    target = tmp_path / 'export'
    target.mkdir()
    service.snapshot(sha, target)
    assert not any(b'DO_NOT_COPY' in p.read_bytes() for p in target.rglob('*') if p.is_file())
    (root / 'core/link.py').symlink_to('/etc/passwd')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Bad link')
    with pytest.raises(ToolDenied, match='links'):
        service.snapshot(git(root, 'rev-parse', 'HEAD'), target)


def test_tools_gate_current_message_and_exact_argument(release):
    service, runtime, sha = release
    normal = DeploymentTools('Inspect deployment', lambda: service)
    names = normal.bind(ToolRegistry()).schemas()
    assert {'preview_deployment', 'inspect_deployment', 'deployment_history'} <= names.keys()
    assert 'approve_and_deploy_commit' not in names
    authorized = DeploymentTools(approval(sha), lambda: service)
    assert 'approve_and_deploy_commit' in authorized.bind(ToolRegistry()).schemas()
    with pytest.raises(ToolDenied):
        authorized.deploy('b' * 40)
    with pytest.raises(ToolDenied):
        normal.deploy(sha)
    authorized.deploy(sha)
    assert 'deployed' in authorized.final_response('model invented failure')


def test_api_approval_and_read_only_history(release):
    import main
    service, runtime, sha = release
    app = main.create_app(database_path=':memory:')
    app.dependency_overrides[main.get_release_service] = lambda: service
    from core.deployment_status import ControllerInspector
    inspector = ControllerInspector(service)
    app.dependency_overrides[main.get_release_inspector] = lambda: inspector
    with TestClient(app) as client:
        assert client.get('/deployments/preview/' + sha).status_code == 200
        assert client.post('/deployments/' + sha + '/deploy', json={'confirmation': 'yes'}).status_code == 409
        result = client.post('/deployments/' + sha + '/deploy', json={'confirmation': approval(sha)}).json()
        assert result['status'] == 'deployed'
        assert client.get('/deployments/' + result['id']).json() == inspector.inspect(result['id'])
        assert client.get('/deployments').json() == inspector.history()


def container_info(runtime, *, image='sha256:old', port=8000, volume='jarvis_state'):
    return dict(Id='old-container', Image=image, Name='/jarvis-jarvis-1', State={'Running': True},
        Config=dict(User='10001:10001', Env=[k + '=' + v for k, v in {**FIXED_ENV, **runtime.secrets()}.items()], Labels={}),
        Mounts=[dict(Type='volume', Name=volume, Destination='/var/lib/jarvis', RW=True)],
        HostConfig=dict(ReadonlyRootfs=True, CapDrop=['ALL'], CapAdd=[], Privileged=False,
            SecurityOpt=['no-new-privileges:true'], Memory=1073741824, NanoCpus=2000000000,
            PidsLimit=256, Init=True, NetworkMode='bridge', PidMode='', IpcMode='private',
            Tmpfs={'/tmp': 'size=256m,mode=1777'},
            PortBindings={'8000/tcp': [dict(HostIp='127.0.0.1', HostPort=str(port))]}))


@pytest.fixture
def docker_runtime(release, monkeypatch):
    service, _, _ = release
    runtime = DockerRuntime(service.config)
    monkeypatch.setattr(runtime, 'secrets', lambda: dict(OPENAI_API_KEY='SECRET_PROVIDER',
        JARVIS_AUTH_TOKEN='SECRET_TOKEN', JARVIS_PUBLIC_ORIGIN='https://jarvis.example.com'))
    return runtime


@pytest.mark.parametrize('field,value', [('ReadonlyRootfs', False), ('CapDrop', []), ('Privileged', True),
    ('SecurityOpt', []), ('Memory', 0), ('PidsLimit', 0), ('NetworkMode', 'host'),
    ('PortBindings', {'8000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8000'}]})])
def test_security_and_public_port_drift_rejected(docker_runtime, field, value):
    info = container_info(docker_runtime)
    docker_runtime.security(info, 'jarvis_state', 8000)
    info['HostConfig'][field] = value
    with pytest.raises(ToolDenied):
        docker_runtime.security(info, 'jarvis_state', 8000)


def test_runtime_command_preserves_security_state_and_no_secret_argv(docker_runtime, monkeypatch):
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'run':
            env_file = Path(args[args.index('--env-file') + 1])
            assert env_file.stat().st_mode & 0o077 == 0
            assert 'SECRET_TOKEN' in env_file.read_text()
        return ''
    monkeypatch.setattr(docker_runtime, 'docker', docker)
    monkeypatch.setattr(docker_runtime, 'inspect_container', lambda name: container_info(docker_runtime))
    docker_runtime.run('jarvis-jarvis-1', 'sha256:old', 'jarvis_state', 8000, 'release-id')
    args = calls[0]
    assert '127.0.0.1:8000:8000' in args and '0.0.0.0:8000:8000' not in args
    assert '--read-only' in args and '--cap-drop=ALL' in args and '--user=10001:10001' in args
    assert 'type=volume,src=jarvis_state,dst=/var/lib/jarvis' in args
    assert not any('SECRET' in a or 'docker.sock' in a for a in args)
    assert not Path(args[args.index('--env-file') + 1]).exists()


@pytest.mark.parametrize('failure', ['health', 'smoke', 'auth', 'persistence'])
def test_real_verifier_checks_health_authenticated_smoke_and_persistence(docker_runtime, monkeypatch, failure):
    monkeypatch.setattr(docker_runtime, 'inspect_container', lambda name: container_info(docker_runtime))
    def request(url, path, *, authenticated=True):
        if path == '/health':
            return (503, None) if failure == 'health' else (200, {'status': 'ok'})
        if not authenticated:
            return (200 if failure == 'auth' else 401), None
        return (403, None) if failure == 'smoke' else (200, {'conversations': []})
    monkeypatch.setattr(docker_runtime, 'request', request)
    ticks = iter([0, 61])
    monkeypatch.setattr('core.deployment_runtime.time.monotonic', lambda: next(ticks))
    with pytest.raises(ToolDenied, match='verification'):
        docker_runtime.verify('jarvis-jarvis-1', 8000, public=True, baseline=['existing'])


def test_runtime_switch_retains_prior_container_and_image(docker_runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(docker_runtime, 'docker', lambda *args, **kw: calls.append(args))
    monkeypatch.setattr(docker_runtime, 'run', lambda *args: calls.append(('run', *args)))
    docker_runtime.switch(dict(id='abc', image='sha256:new', previous=dict(container_id='old', image='sha256:old')))
    assert calls[0] == ('image', 'tag', 'sha256:old', 'jarvis-rollback:abc')
    assert calls[1][0] == 'stop' and calls[2] == ('rename', 'old', 'jarvis-previous-abc')
    assert calls[3][3] == 'jarvis_state'
    assert all('rm' not in call and 'prune' not in call for call in calls)


def test_controller_disabled_in_production(monkeypatch):
    monkeypatch.setenv('JARVIS_MODE', 'production')
    with pytest.raises(ToolDenied, match='development-only'):
        ReleaseConfig.configured()


def test_validation_binds_complete_suites_to_git_snapshot_and_invalidates_on_error(release, monkeypatch):
    from tools import CommandResult
    service, _, sha = release
    calls = []
    def run(repo, snapshot, paths, *, ui=False):
        assert (snapshot / 'main.py').read_text() == 'print("committed")\n'
        assert not (snapshot / '.git').exists()
        calls.append((paths, ui))
        return CommandResult((), 0, 'SECRET_TEST_OUTPUT', sandbox={'backend': 'fixture'})
    monkeypatch.setattr('tools.sandbox.run_tests', run)
    result = service.validate(sha)
    assert result['passed'] and calls == [(['tests'], False), (['tests/ui.test.cjs'], True)]
    assert 'SECRET_TEST_OUTPUT' not in json.dumps(service.load())
    def fail(*args, **kwargs):
        raise ToolDenied('validation setup failed')
    monkeypatch.setattr('tools.sandbox.run_tests', fail)
    with pytest.raises(ToolDenied):
        service.validate(sha)
    assert sha not in service.load()['evidence']


def test_known_baseline_changed_files(release):
    service, runtime, sha = release
    runtime.current_value['revision'] = sha
    (service.config.source / 'main.py').write_text('next commit\n')
    git(service.config.source, 'add', '.')
    git(service.config.source, 'commit', '-m', 'Next release')
    preview = service.preview(git(service.config.source, 'rev-parse', 'HEAD'))
    assert preview['changed_files'] == ['main.py']


def test_runtime_redacts_known_secrets(docker_runtime):
    result = docker_runtime.redact({'message': 'SECRET_TOKEN and SECRET_PROVIDER', 'image': 'sha256:test'})
    assert result['message'] == '[redacted] and [redacted]'
    assert result['image'] == 'sha256:test'


def test_mounts_cannot_include_docker_socket_or_host_tmp(docker_runtime):
    for destination in ('/var/run/docker.sock', '/tmp'):
        info = container_info(docker_runtime)
        info['Mounts'].append(dict(Type='bind', Source='/host', Destination=destination, RW=True))
        with pytest.raises(ToolDenied):
            docker_runtime.security(info, 'jarvis_state', 8000)


def test_real_restore_uses_prior_container_and_never_removes_state(docker_runtime, monkeypatch):
    previous = container_info(docker_runtime)
    previous['Name'] = '/jarvis-previous-abc'
    current = container_info(docker_runtime, image='sha256:new')
    current['Id'] = 'new-container'
    current['Config']['Labels']['jarvis.release'] = 'abc'
    containers = {previous['Id']: previous, 'jarvis-jarvis-1': current}
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'rename':
            previous['Name'] = '/' + args[2]
        return ''
    monkeypatch.setattr(docker_runtime, 'inspect_container', lambda name: containers.get(name))
    monkeypatch.setattr(docker_runtime, 'docker', docker)
    monkeypatch.setattr(docker_runtime, 'cleanup', lambda record: None)
    docker_runtime.restore(dict(id='abc', previous=dict(container_id='old-container', image='sha256:old')))
    assert calls == [('stop', '--time=390', 'new-container'), ('rm', '--force', 'new-container'),
                     ('rename', 'old-container', 'jarvis-jarvis-1'), ('start', 'old-container')]
    assert not any('volume' in call for call in calls)


def test_prior_conversation_never_grants_deployment_capability():
    from core.agent import AgentRunner
    from core.task_store import TaskStore
    from test_agent import ScriptedModel, call, final
    store = TaskStore(':memory:')
    first = store.create_task(approval('a' * 40))
    current = store.create_task('deploy it', conversation_id=first['conversation_id'])
    model = ScriptedModel(call('approve_and_deploy_commit', {'commit': 'a' * 40}), final('Denied'))
    AgentRunner(model=model).run(current['goal'], task_id=current['id'], task_store=store)
    assert 'approve_and_deploy_commit' not in {t['name'] for t in model.requests[0]['tools']}
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'unknown_tool'


def test_database_migration_rejected_before_production_stop(release, docker_runtime, monkeypatch):
    service, _, sha = release
    (service.config.source / 'core/database.py').write_text('new schema')
    git(service.config.source, 'add', '.')
    git(service.config.source, 'commit', '-m', 'Migration')
    sha = git(service.config.source, 'rev-parse', 'HEAD')
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        return 'old-hash  /app/core/database.py'
    monkeypatch.setattr(docker_runtime, 'docker', docker)
    with pytest.raises(ToolDenied, match='migration'):
        docker_runtime.precheck(dict(id='abc', commit=sha, database_hash='new-hash',
                                      previous=dict(container_id='old-container')))
    assert [call[0] for call in calls] == ['exec']


def test_git_replacement_objects_cannot_change_approved_source(release, tmp_path):
    service, _, sha = release
    root = service.config.source
    original = git(root, 'rev-parse', sha + ':main.py')
    replacement = subprocess.check_output(['git', '-C', str(root), 'hash-object', '-w', '--stdin'],
                                          input=b'print("unapproved replacement")\n').decode().strip()
    git(root, 'replace', original, replacement)
    target = tmp_path / 'export-replaced'
    target.mkdir()
    service.snapshot(sha, target)
    assert (target / 'main.py').read_text() == 'print("committed")\n'


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_hidden_index_changes_are_rejected(release, flag):
    service, _, sha = release
    git(service.config.source, 'update-index', flag, 'main.py')
    (service.config.source / 'main.py').write_text('uncommitted hidden change')
    with pytest.raises(ToolDenied, match='index'):
        service.deploy(sha, approval(sha))


def test_build_uses_reviewed_context_labels_and_immutable_image(docker_runtime, monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'Dockerfile').write_bytes((root / 'Dockerfile').read_bytes())
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('image', 'inspect'):
            return json.dumps([{'Id': 'sha256:built', 'Config': {'Volumes': {'/var/lib/jarvis': {}}}}])
        return ''
    monkeypatch.setattr(docker_runtime, 'docker', docker)
    assert docker_runtime.build('a' * 40, tmp_path) == 'sha256:built'
    assert calls[0][0] == 'build' and calls[0][-1] == str(tmp_path)
    assert 'org.opencontainers.image.revision=' + 'a' * 40 in calls[0]
    assert 'jarvis-release:' + 'a' * 40 in calls[0]
    assert not any('SECRET' in arg or arg in {'--secret', '--build-arg', '--ssh'} for arg in calls[0])
    (tmp_path / 'Dockerfile').write_text('FROM unreviewed')
    with pytest.raises(ToolDenied, match='Dockerfile'):
        docker_runtime.build('a' * 40, tmp_path)


@pytest.mark.parametrize('outcome', ['success', 'failure', 'optional', 'timeout', 'interrupt'])
def test_docker_private_home_is_writable_isolated_and_always_removed(docker_runtime, monkeypatch,
                                                                  tmp_path, outcome):
    operator = tmp_path / 'operator'
    for name in ('.ssh/id_rsa', '.docker/config.json', '.env', 'state/private.db'):
        path = operator / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('OPERATOR_SECRET')
    for key, value in {'HOME': str(operator), 'DOCKER_CONFIG': str(operator / '.docker'),
                       'XDG_CONFIG_HOME': str(operator), 'XDG_CACHE_HOME': str(operator),
                       'BUILDX_CONFIG': str(operator), 'SSH_AUTH_SOCK': 'PRIVATE_SOCKET',
                       'OPENAI_API_KEY': 'OPERATOR_SECRET'}.items():
        monkeypatch.setenv(key, value)
    homes = []
    def run(argv, *, stdin, capture_output, timeout, env):
        assert argv == [*DOCKER, 'build', '--network=none', str(tmp_path)]
        assert stdin == subprocess.DEVNULL and capture_output and timeout == 7
        assert set(env) == {'PATH', 'HOME'} and env['PATH'] == '/usr/bin:/bin'
        home = Path(env['HOME'])
        homes.append(home)
        assert home.resolve() != operator.resolve() and not home.is_relative_to(tmp_path)
        assert home.stat().st_mode & 0o777 == 0o700
        assert home.stat().st_uid == os.getuid() and not list(home.iterdir())
        # Reproduce the build client's home-relative directory creation.
        config = home / '.docker/buildx'
        config.mkdir(parents=True)
        (config / 'activity').write_text('private build metadata')
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(argv, timeout)
        if outcome == 'interrupt':
            raise KeyboardInterrupt()
        return subprocess.CompletedProcess(argv, 0 if outcome == 'success' else 1,
                                           b'built', b'PRIVATE_DIAGNOSTIC')
    monkeypatch.setattr('core.deployment_runtime.subprocess.run', run)
    def invoke():
        return docker_runtime.docker('build', '--network=none', str(tmp_path), timeout=7,
                                     optional=outcome == 'optional')
    if outcome == 'success':
        assert invoke() == 'built'
        assert invoke() == 'built'
        assert homes[0] != homes[1]
    elif outcome == 'optional':
        assert invoke() is None
    else:
        error = {'failure': ToolDenied, 'timeout': subprocess.TimeoutExpired,
                 'interrupt': KeyboardInterrupt}[outcome]
        with pytest.raises(error) as exc:
            invoke()
        assert 'PRIVATE_DIAGNOSTIC' not in str(exc.value)
    assert homes and all(not home.exists() for home in homes)
    assert all(path.read_text() == 'OPERATOR_SECRET' for path in operator.rglob('*') if path.is_file())


@pytest.mark.skipif(os.environ.get('JARVIS_DOCKER_TESTS') != '1',
                    reason='Set JARVIS_DOCKER_TESTS=1 for real Docker build regression')
def test_live_docker_build_nonexistent_home_failure_and_private_home_fix(docker_runtime, tmp_path,
                                                                      monkeypatch):
    # No production image/container, credentials, base-image pull or network needed.
    (tmp_path / 'Dockerfile').write_text('FROM scratch\nCOPY marker /marker\n')
    (tmp_path / 'marker').write_text(tmp_path.name)
    args = ['build', '--network=none', '--pull=false', '--quiet', str(tmp_path)]
    legacy = subprocess.run([*DOCKER, *args], stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=60, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'})
    assert legacy.returncode != 0
    assert b'/nonexistent' in legacy.stderr and b'permission denied' in legacy.stderr.lower()
    original_run = subprocess.run
    homes = []
    def run(*args, **kwargs):
        homes.append(Path(kwargs['env']['HOME']))
        assert homes[-1].stat().st_mode & 0o777 == 0o700
        return original_run(*args, **kwargs)
    monkeypatch.setattr('core.deployment_runtime.subprocess.run', run)
    image = docker_runtime.docker(*args, timeout=60).strip()
    try:
        assert image.startswith('sha256:')
        assert homes and all(not home.exists() for home in homes)
    finally:
        if image.startswith('sha256:'):
            docker_runtime.docker('image', 'rm', image)
    assert all(not home.exists() for home in homes)


@pytest.mark.parametrize('fixture_path', ['tests/test_self_edit.py', 'tests/test_recall.py'])
def test_exact_reviewed_synthetic_key_fixtures_export(release, tmp_path, fixture_path):
    service, _, _ = release
    content = (Path(__file__).resolve().parents[1] / fixture_path).read_bytes()
    target = service.config.source / fixture_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    git(service.config.source, 'add', '.')
    git(service.config.source, 'commit', '-m', 'Reviewed synthetic fixture')
    export = tmp_path / 'export'
    export.mkdir()
    service.snapshot(git(service.config.source, 'rev-parse', 'HEAD'), export)
    assert (export / fixture_path).read_bytes() == content


@pytest.mark.parametrize('key_type', ['', 'RSA ', 'OPENSSH ', 'EC ', 'DSA ', 'ENCRYPTED '])
@pytest.mark.parametrize('path', ['core/example.py', 'tests/test_arbitrary.py',
                                 'tests/test_self_edit.py', 'tests/test_recall.py'])
def test_unapproved_key_material_rejected_in_every_scope(release, tmp_path, key_type, path):
    service, _, _ = release
    # Construct synthetic PEM-shaped material without embedding a marker in this
    # test source. Even an approved filename cannot authorize different bytes.
    marker = '-----BEGIN ' + key_type + 'PRIVATE KEY' + '-----'
    content = 'value = """' + marker + '\n' + 'MIIE' + 'A' * 256 + '\n"""\n'
    target = service.config.source / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(service.config.source, 'add', '.')
    git(service.config.source, 'commit', '-m', 'Unapproved key fixture')
    export = tmp_path / 'export'
    export.mkdir()
    with pytest.raises(ToolDenied, match='Private key material'):
        service.snapshot(git(service.config.source, 'rev-parse', 'HEAD'), export)


@pytest.mark.parametrize('fixture_path', ['tests/test_self_edit.py', 'tests/test_recall.py'])
@pytest.mark.parametrize('change', ['append_key', 'one_byte', 'relocate'])
def test_fixture_changes_are_independent_of_production_key_protection(release, tmp_path, fixture_path, change):
    service, _, _ = release
    content = (Path(__file__).resolve().parents[1] / fixture_path).read_bytes()
    if change == 'append_key':
        content += b'\nkey = """-----BEGIN ' + b'PRIVATE KEY' + b'-----\nMIIE' + b'A' * 256 + b'\n"""\n'
    elif change == 'one_byte':
        content += b'\n'
    else:
        fixture_path = 'tests/test_unapproved.py'
    target = service.config.source / fixture_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    git(service.config.source, 'add', '.')
    git(service.config.source, 'commit', '-m', 'Altered synthetic fixture')
    export = tmp_path / 'export'
    export.mkdir()
    if change == 'append_key':
        with pytest.raises(ToolDenied, match='Private key material'):
            service.snapshot(git(service.config.source, 'rev-parse', 'HEAD'), export)
    else:
        service.snapshot(git(service.config.source, 'rev-parse', 'HEAD'), export)
        assert (export / fixture_path).read_bytes() == content
