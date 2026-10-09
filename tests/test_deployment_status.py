"""Read-only production chat uses public metadata, never host controller access."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from core.agent import AgentRunner
from core.deployment_release import approval, now
from core.deployment_status import SnapshotInspector, projection, configured_inspector
from core.deployment_tools import DeploymentTools
from core.task_store import TaskStore
from tools import RepositoryTools, ToolDenied, ToolRegistry
from test_agent import ScriptedModel, call, final
from test_deployment_release import release, docker_runtime

SHA = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
IDENTIFIER = 'c' * 32


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    record = dict(id=IDENTIFIER, commit=SHA, image=IMAGE, status='deployed',
                  updated_at=now(), message='SECRET_MESSAGE', approval='SECRET_APPROVAL',
                  error='SECRET_LOG', changed_files=['.env'], baseline=['SECRET_FINGERPRINT'],
                  evidence=dict(commit=SHA, at=now(), passed=True, review='SECRET_REVIEW',
                                tests=[dict(phase='python', passed=True, stdout='SECRET_OUTPUT')]),
                  rollback=dict(verified=True, image=IMAGE, revision=SHA, at=now(), error='SECRET_ERROR'))
    raw = dict(version=1, current=dict(revision=SHA, image=IMAGE, env='SECRET_ENV'),
               history=[record], observed_at=now(), controller_config='SECRET_CONFIG')
    path = tmp_path / 'status.json'
    path.write_text(json.dumps(raw))
    monkeypatch.setattr('core.deployment_status.STATUS_PATH', path)
    monkeypatch.setenv('JARVIS_MODE', 'production')
    monkeypatch.setenv('JARVIS_DEPLOY_CONFIG', '/must-not-be-read')
    return path


@pytest.mark.parametrize('name,args', [('deployment_status', {}), ('deployment_history', {}),
    ('inspect_deployment', {'deployment_id': IDENTIFIER}), ('preview_deployment', {'commit': SHA})])
def test_ordinary_production_chat_can_inspect_and_audits(snapshot, monkeypatch, name, args):
    def forbidden():
        pytest.fail('Read-only production chat invoked host controller')
    monkeypatch.setattr('core.deployment_tools.configured_service', forbidden)
    store = TaskStore(':memory:')
    task = store.create_task('Show me the current production deployment status and the latest deployment release')
    model = ScriptedModel(call(name, args), final('Inspected deployment metadata.'))
    result = AgentRunner(model=model).run(task['goal'], task_id=task['id'], task_store=store)
    assert result.status == 'completed'
    response = json.loads(model.requests[1]['input'][-1]['output'])
    assert response['ok'] and SHA in response['data']['text']
    assert 'SECRET_' not in response['data']['text']
    tools = {t['name'] for t in model.requests[0]['tools']}
    assert 'approve_and_deploy_commit' not in tools and 'rollback' not in tools
    assert any(a['type'] == 'tool_completed' for a in store.get_task(task['id'])['activity'])
    assert 'SECRET_' not in json.dumps(store.get_task(task['id'])['activity'])


def test_snapshot_latest_history_rollback_and_unknown_live_eligibility(snapshot):
    reader = configured_inspector()
    status = reader.status()
    assert status['current'] == dict(revision=SHA, image=IMAGE)
    assert status['latest']['id'] == IDENTIFIER and status['observed_at']
    assert status['source'] == 'controller_snapshot'
    assert reader.inspect(IDENTIFIER)['rollback']['verified'] is True
    assert reader.history()[0] == status['latest']
    assert reader.preview(SHA)['eligible'] is None
    assert reader.preview(SHA)['evidence']['fresh'] is True
    assert reader.preview('d' * 40)['evidence'] is None


def test_expired_validation_is_not_reported_fresh(snapshot):
    data = json.loads(snapshot.read_text())
    data['history'][0]['evidence']['at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    snapshot.write_text(json.dumps(data))
    assert configured_inspector().preview(SHA)['evidence']['fresh'] is False


def test_status_shows_each_python_batch_ui_and_inventory_failure(snapshot):
    data = json.loads(snapshot.read_text())
    data['history'][0]['evidence']['tests'] = [
        dict(phase='python', shard='python-1', passed=True, failure_kind=None),
        dict(phase='python', shard='python-2', passed=True, failure_kind=None),
        dict(phase='python', shard='python-3', passed=False, failure_kind='test_failure',
             stdout='SECRET_OUTPUT'),
        dict(phase='ui', shard='ui', passed=True, failure_kind=None),
        dict(phase='python', shard='inventory', passed=False,
             failure_kind='test_inventory_changed'),
    ]
    snapshot.write_text(json.dumps(data))
    tests = configured_inspector().status()['latest']['evidence']['tests']
    assert [(test['shard'], test['passed'], test['failure_kind']) for test in tests] == [
        ('python-1', True, None), ('python-2', True, None),
        ('python-3', False, 'test_failure'), ('ui', True, None),
        ('inventory', False, 'test_inventory_changed'),
    ]
    assert 'SECRET_OUTPUT' not in json.dumps(tests)


def test_known_secrets_redacted_even_in_allowed_fields(snapshot, monkeypatch):
    monkeypatch.setenv('JARVIS_AUTH_TOKEN', SHA)
    monkeypatch.setenv('OPENAI_API_KEY', 'b' * 64)
    text = json.dumps(configured_inspector().status())
    assert SHA not in text and 'b' * 64 not in text and '[redacted]' in text


@pytest.mark.parametrize('message', ['deploy it', 'yes', 'approved', approval(SHA) + '.',
                                     'Please ' + approval(SHA), approval(SHA) + '\n'])
def test_vague_messages_cannot_mutate(snapshot, message):
    tools = DeploymentTools(message)
    registry = tools.bind(ToolRegistry())
    assert 'approve_and_deploy_commit' not in registry.schemas()
    with pytest.raises(ToolDenied):
        tools.deploy(SHA)


def test_exact_approval_binds_only_that_commit_and_production_still_cannot_mutate(snapshot):
    tools = DeploymentTools(approval(SHA))
    assert 'approve_and_deploy_commit' in tools.bind(ToolRegistry()).schemas()
    with pytest.raises(ToolDenied, match='outside'):
        tools.deploy('d' * 40)
    with pytest.raises(ToolDenied, match='development-only'):
        tools.deploy(SHA)


@pytest.mark.parametrize('path', ['.env', '.ssh/id_rsa', '.docker/config.json',
                                 'state/releases.json', '../releases.json', '/var/lib/jarvis/deployment-status.json'])
def test_inspection_does_not_loosen_repository_policy(snapshot, tmp_path, path):
    registry = DeploymentTools('Show deployment status').bind(ToolRegistry(RepositoryTools(tmp_path)))
    store = TaskStore(':memory:')
    task = store.create_task('Read a protected path')
    result = registry.execute('read_text', json.dumps({'path': path}), task_id=task['id'], task_store=store)
    assert not result.ok and result.error.code == 'denied'


@pytest.mark.parametrize('kind', ['missing', 'symlink', 'fifo', 'oversize', 'malformed'])
def test_bad_snapshot_fails_closed_without_diagnostics(snapshot, kind):
    snapshot.unlink()
    if kind == 'symlink':
        snapshot.symlink_to('/etc/passwd')
    elif kind == 'fifo':
        os.mkfifo(snapshot)
    elif kind == 'oversize':
        snapshot.write_text('SECRET' * 20_000)
    elif kind == 'malformed':
        snapshot.write_text('SECRET_MALFORMED')
    with pytest.raises(ToolDenied, match='snapshot unavailable'):
        configured_inspector().status()


def test_projection_bounds_history_and_discards_sensitive_strings():
    records = [dict(id=f'{i:032x}', status='failed', error='SECRET', message='SECRET') for i in range(25)]
    value = projection({}, records, now())
    assert len(value['history']) == 10
    assert value['history'][0]['id'] == f'{24:032x}'
    assert 'SECRET' not in json.dumps(value)


def test_status_dependencies_remain_trusted_and_candidate_protected():
    from core.self_edit import CandidatePolicy
    repository = RepositoryTools(Path(__file__).resolve().parents[1])
    from core.engineering_authority import assess_action
    for path in ('core/deployment_status.py', 'tests/test_deployment_status.py'):
        # Host test snapshots still provision the trusted runtime copy.
        assert path in repository._runtime_deployment_files()
        # Editable only in an isolated candidate; applying it needs approval.
        CandidatePolicy().check(path)
        decision = assess_action('candidate_apply', changed_paths=[path])
        assert decision.approval_required and 'authority_expansion' in decision.escalation
    for path in ('../core/deployment_status.py', '/var/lib/jarvis/status.json'):
        with pytest.raises(ToolDenied):
            CandidatePolicy().check(path)


def test_controller_publishes_verified_rollback(release, monkeypatch):
    service, runtime, sha = release
    published = []
    runtime.fail = 'health'
    monkeypatch.setattr(runtime, 'publish_status', published.append, raising=False)
    result = service.deploy(sha, approval(sha))
    assert result['status'] == 'rolled_back'
    assert published[-1]['history'][0]['rollback']['verified'] is True
    assert published[-1]['history'][0]['status'] == 'rolled_back'


def test_controller_publishes_terminal_result_without_changing_transaction(release, monkeypatch):
    service, runtime, sha = release
    published = []
    monkeypatch.setattr(runtime, 'publish_status', published.append, raising=False)
    result = service.deploy(sha, approval(sha))
    assert result['status'] == 'deployed'
    assert published[-1]['history'][0]['status'] == 'deployed'
    assert result['status_publication_failed'] is False
    assert 'approval' not in json.dumps(published)


def test_publication_failure_is_audited_without_rollback(release, monkeypatch):
    service, runtime, sha = release
    def fail(value):
        raise RuntimeError('SECRET')
    monkeypatch.setattr(runtime, 'publish_status', fail, raising=False)
    result = service.deploy(sha, approval(sha))
    assert result['status'] == 'deployed' and result['status_publication_failed']
    assert 'restore' not in runtime.calls and 'SECRET' not in json.dumps(service.history())


def test_status_publisher_sends_only_projection_and_writes_atomically(docker_runtime, monkeypatch, tmp_path):
    tmp_path = tmp_path / 'published'
    tmp_path.mkdir()
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        assert args[:4] == ('exec', docker_runtime.config.container, 'python', '-c')
        assert 'SECRET' not in args[-1]
        # Execute the exact writer with only its fixed state root relocated.
        script = args[4].replace("Path('/var/lib/jarvis')", f'Path({str(tmp_path)!r})')
        subprocess.run(['python3', '-c', script, args[5]], check=True, capture_output=True)
    monkeypatch.setattr(docker_runtime, 'docker', docker)
    value = dict(version=1, current=dict(image=IMAGE, revision=SHA), observed_at=now(),
                 history=[dict(id=IDENTIFIER, status='deployed', error='SECRET_TOKEN')], secret='SECRET_PROVIDER')
    docker_runtime.publish_status(value)
    target = tmp_path / 'deployment-status.json'
    assert target.stat().st_mode & 0o077 == 0
    assert len(list(tmp_path.iterdir())) == 1
    assert SnapshotInspector(target).status()['current']['image'] == IMAGE


def test_production_read_api_is_separate_from_mutation_dependency(snapshot, monkeypatch):
    monkeypatch.setenv('JARVIS_MODE', 'development')
    import main
    # Perimeter is independently tested; this test exercises the real production reader.
    from core.deployment import Settings
    app = main.create_app(settings=Settings(database_path=Path(':memory:')))
    monkeypatch.setenv('JARVIS_MODE', 'production')
    with TestClient(app) as client:
        assert client.get('/deployments/status').json()['latest']['id'] == IDENTIFIER
        assert client.get('/deployments').json()[0]['id'] == IDENTIFIER
        assert client.get('/deployments/' + IDENTIFIER).status_code == 200
        assert client.get('/deployments/preview/' + SHA).json()['eligible'] is None
        assert client.post('/deployments/' + SHA + '/deploy', json={'confirmation': approval(SHA)}).status_code == 409
