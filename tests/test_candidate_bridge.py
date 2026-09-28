"""Production chat delegates only bounded dev candidates; no production storage."""
import json
import os
import time
from uuid import uuid4
from pathlib import Path

import pytest

from core.candidate_bridge import Request, CandidateClient, project, read, write, transfer
from core.candidate_broker import Broker
from core.candidate_tools import CandidateTools
from core.agent import AgentRunner
from core.task_store import TaskStore
from tools import ToolRegistry, RepositoryTools, ToolDenied
from test_agent import ScriptedModel, call, final
from test_self_edit import setup, tree_bytes

ARGS = dict(inspect_paths=['src/app.py'], targeted_tests=['tests/test_app.py'])
GOAL = 'Build Phase 1 of JarvisBrain\n- Do not change production behavior by default.\nDo not apply. Do not deploy.'


def request(action='create', goal=GOAL, arguments=None, **extra):
    return dict(request_id=str(uuid4()), task_id=str(uuid4()), conversation_id=str(uuid4()),
                goal=goal, action=action, arguments=ARGS if arguments is None else arguments,
                created=time.time(), **extra)


@pytest.mark.parametrize('goal', [
    GOAL + '\nDo not create a candidate.',
    GOAL + '\nDo not edit your code.',
    'Create a self-edit candidate. Do not change production behavior by default or create candidates.',
    'Create a self-edit candidate\nDo not change production behavior by default and do not edit.',
    'Only plan\nDo not change production behavior by default.',
])
def test_production_preservation_does_not_erase_other_denials(setup, goal):
    with pytest.raises(ToolDenied):
        Broker(setup[2](), source=setup[0]).execute(request(goal=goal))


@pytest.mark.parametrize('target', ['/app', '/home/ubuntu/jarvis', '/tmp/arbitrary'])
def test_request_cannot_select_repository(setup, target):
    from pydantic import ValidationError
    broker = Broker(setup[2](), source=setup[0])
    with pytest.raises(ValidationError):
        broker.execute(request(arguments=dict(ARGS, source_root=target)))
    with pytest.raises(ValidationError):
        broker.execute(request(source_root=target))


@pytest.mark.parametrize('kind', ['production', 'arbitrary', 'symlink', 'traversal', 'mode'])
def test_misconfigured_broker_fails_closed(setup, tmp_path, monkeypatch, kind):
    source, _, factory = setup
    service = factory()
    configured = source
    if kind in {'production', 'arbitrary'}:
        service.source = tmp_path / kind
        service.source.mkdir()
    elif kind == 'symlink':
        configured = tmp_path / 'alias'
        configured.symlink_to(source, target_is_directory=True)
    elif kind == 'traversal':
        configured = source / '..' / source.name
    else:
        monkeypatch.setenv('JARVIS_MODE', 'production')
    with pytest.raises(ToolDenied):
        Broker(service, source=configured).execute(request())
    with service.store.database.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0


@pytest.mark.parametrize('path', ['.env', 'tools/policy.py', 'core/candidate_broker.py',
    '../outside.py', '/home/ubuntu/jarvis/main.py', '/tmp/arbitrary.py',
    'src/../../outside.py', 'src/./app.py', 'src//app.py', 'src\\app.py'])
def test_broker_rejects_protected_and_noncanonical_paths(setup, path):
    result = Broker(setup[2](), source=setup[0]).execute(
        request(arguments=dict(ARGS, inspect_paths=[path])))
    assert result['state'] == 'rejected'


def test_ordinary_production_chat_routes_to_dev_and_audits(setup, tmp_path, monkeypatch):
    source, db, factory = setup
    broker = Broker(factory(), source=source)
    before = tree_bytes(source)
    production = tmp_path / 'production'
    production.mkdir()
    for name in ('.env', 'state.sqlite3', 'ssh-key', 'docker-config', 'Caddyfile'):
        (production / name).write_text('PRIVATE SENTINEL')
    untouched = tree_bytes(production)
    requests = []
    def invoke(client, action, arguments):
        raw = request(action, client.goal, arguments)
        raw.update(client.context)
        requests.append(raw)
        # Separate host process has its own environment, never production mode.
        with monkeypatch.context() as host:
            host.setenv('JARVIS_MODE', 'development')
            return broker.execute(raw)
    monkeypatch.setattr(CandidateClient, 'invoke', invoke)
    monkeypatch.setenv('JARVIS_MODE', 'production')
    store = TaskStore(tmp_path / 'chat.sqlite3')
    task = store.create_task(GOAL)
    model = ScriptedModel(call('create_self_edit_candidate', ARGS), final('Applied and deployed!'))
    runner = AgentRunner(ToolRegistry(RepositoryTools(production)), model=model,
                         candidate_service_factory=lambda db: pytest.fail('Local service must never run'))
    outcome = runner.run(GOAL, task_id=task['id'], task_store=store)
    assert 'passed' in outcome.response and 'Applied and deployed' not in outcome.response
    names = {t['name'] for t in model.requests[0]['tools']}
    assert CANDIDATE_TOOLS | READ_ONLY_TOOLS == names
    assert not names & {'approve_and_apply_candidate', 'approve_and_deploy_commit'}
    data = json.loads(model.requests[1]['input'][-1]['output'])['data']
    record = factory().store.get(data['candidate_id'], internal=True)
    assert record['source_context']['task_id'] == task['id']
    assert record['source_context']['conversation_id'] == task['conversation_id']
    assert record['source_context']['source'] == 'production_chat'
    assert Path(record['worktree']).is_relative_to(tmp_path / 'candidates')
    assert (Path(record['worktree']) / 'src/app.py').read_text() == 'value = 2\n'
    assert tree_bytes(source) == before and tree_bytes(production) == untouched
    with monkeypatch.context() as host:
        host.setenv('JARVIS_MODE', 'development')
        assert broker.execute(requests[0])['id'] == record['id']
    assert 'PRIVATE SENTINEL' not in json.dumps(data)
    assert 'worktree' not in project(record)


@pytest.mark.parametrize('action', ['apply', 'deploy', 'command', 'start'])
def test_bridge_has_no_privileged_operations(setup, action):
    with pytest.raises(ToolDenied):
        Broker(setup[2](), source=setup[0]).execute(request(action))


@pytest.mark.parametrize('goal', ['Only plan to build JarvisBrain', 'Do not modify your code', 'Hello', '"Build JarvisBrain"'])
def test_host_rechecks_authority(setup, goal):
    with pytest.raises(ToolDenied):
        Broker(setup[2](), source=setup[0]).execute(request(goal=goal))


def test_protected_paths_and_sanitized_projection(setup):
    record = Broker(setup[2](), source=setup[0]).execute(request(arguments=dict(ARGS, inspect_paths=['.env'])))
    assert record['state'] == 'rejected'
    record.update(diff='secret', worktree='/private/path', goal='secret',
                  reviewer_result=dict(verdict='rejected', summary='secret'),
                  error='secret', files_changed=['secret'],
                  test_results=[dict(phase='targeted', returncode=1, stdout='secret', stderr='secret')])
    assert 'secret' not in json.dumps(project(record))


def test_mailbox_round_trip_and_links(tmp_path, monkeypatch):
    import core.candidate_bridge as bridge
    monkeypatch.setattr(bridge, 'ROOT', tmp_path / 'mailbox')
    directory = bridge.root()
    raw = request()
    path = directory / (raw['request_id'] + '.request')
    write(path, raw)
    assert transfer('receive') == raw
    transfer('respond', dict(request_id=raw['request_id'], ok=False))
    assert not path.exists()
    assert read(directory / (raw['request_id'] + '.response'))['ok'] is False
    target = tmp_path / 'private'
    target.write_text('{}')
    path.symlink_to(target)
    with pytest.raises(OSError):
        read(path)
    path.unlink()
    os.link(target, path)
    with pytest.raises(ToolDenied):
        read(path)
    assert target.read_text() == '{}'


def test_production_approval_does_not_enable_local_apply(setup, monkeypatch):
    monkeypatch.setenv('JARVIS_MODE', 'production')
    adapter = CandidateTools(f'approve and apply candidate {uuid4()} to main', setup[1])
    assert 'approve_and_apply_candidate' not in adapter.bind(ToolRegistry()).schemas()


@pytest.mark.skipif(os.environ.get('JARVIS_DOCKER_TESTS') != '1', reason='Opt-in Docker integration')
def test_broker_create_rerun_review_discard_real_docker(setup, monkeypatch):
    monkeypatch.setenv('JARVIS_MODE', 'development')
    source, _, factory = setup
    before = tree_bytes(source)
    broker = Broker(factory(tester=None), source=source)
    created = request()
    result = broker.execute(created)
    assert result['state'] == 'passed', result
    identifier = result['id']
    full = broker.service.store.get(identifier)
    assert all(t['sandbox']['backend'] == 'docker' for t in full['test_results'])
    for action, verb, state in [('tests', 'Rerun tests for', 'needs_revision'),
                                ('review', 'Review', 'passed'), ('inspect', 'Inspect', 'passed'),
                                ('discard', 'Discard', 'discarded')]:
        result = broker.execute(request(action, f'{verb} candidate {identifier}', dict(candidate_id=identifier)))
        assert result['state'] == state
    assert tree_bytes(source) == before
    full = broker.service.store.get(identifier)
    assert len(full['chat_operations']) == 4
    assert not Path(full['worktree']).exists()


CANDIDATE_TOOLS = {'create_self_edit_candidate', 'inspect_self_edit_candidate',
                   'rerun_candidate_tests', 'review_self_edit_candidate', 'discard_self_edit_candidate'}
READ_ONLY_TOOLS = {'list_files', 'read_text', 'git_status', 'git_diff', 'run_pytest',
                   'run_command', 'deployment_status', 'preview_deployment',
                   'inspect_deployment', 'deployment_history', 'inspect_current_brain'}


def test_fresh_production_http_turn_exposes_candidates_without_cached_authority(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from core.deployment import Settings
    from core.orchestrator import Orchestrator
    monkeypatch.setenv('JARVIS_MODE', 'production')
    settings = Settings.from_env(dict(JARVIS_MODE='production', OPENAI_API_KEY='test-provider-key',
        JARVIS_AUTH_TOKEN='test-auth-' + 'x' * 32, JARVIS_PUBLIC_ORIGIN='https://jarvis.example.com',
        JARVIS_STATE_DIR=str(tmp_path / 'state'), JARVIS_DB_PATH=str(tmp_path / 'state/chat.sqlite3')))
    model = ScriptedModel(final('Capabilities'), final('Ready'), final('Capabilities'))
    runner = AgentRunner(model=model)
    monkeypatch.setattr(main, 'orchestrator', Orchestrator(runner))
    app = main.create_app(settings=settings)
    with TestClient(app, base_url=settings.public_origin) as client:
        conversation = None
        for goal in ('What tools can you use?', GOAL, 'What tools can you use?'):
            payload = dict(goal=goal)
            if conversation:
                payload['conversation_id'] = conversation
            result = client.post('/task', json=payload,
                                 headers={'Authorization': 'Bearer ' + settings.auth_token})
            assert result.status_code == 200, result.text
            conversation = result.json()['conversation_id']
            names = {t['name'] for t in model.requests[-1]['tools']}
            assert names == READ_ONLY_TOOLS | CANDIDATE_TOOLS
    assert set(runner.registry.schemas()) == READ_ONLY_TOOLS - {
        'deployment_status', 'preview_deployment', 'inspect_deployment', 'deployment_history', 'inspect_current_brain'}


@pytest.mark.parametrize('tool,arguments', [
    ('create_self_edit_candidate', ARGS),
    *[(name, {'candidate_id': '00000000-0000-0000-0000-000000000001'})
      for name in sorted(CANDIDATE_TOOLS - {'create_self_edit_candidate'})]])
def test_visible_tools_do_not_expand_current_request_authority(tmp_path, monkeypatch, tool, arguments):
    monkeypatch.setenv('JARVIS_MODE', 'production')
    monkeypatch.setattr(CandidateClient, 'invoke', lambda *a: pytest.fail('Unauthorized broker call'))
    store = TaskStore(tmp_path / 'chat.sqlite3')
    goal = 'What tools can you use?'
    task = store.create_task(goal)
    model = ScriptedModel(call(tool, arguments), final())
    AgentRunner(model=model).run(goal, task_id=task['id'], task_store=store)
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'denied'


def test_absent_broker_times_out_without_local_fallback(tmp_path, monkeypatch):
    import core.candidate_bridge as bridge
    from types import SimpleNamespace
    monkeypatch.setenv('JARVIS_MODE', 'production')
    monkeypatch.setattr(bridge, 'ROOT', tmp_path / 'mailbox')
    # Advance only the mailbox clock, not global task/store deadlines.
    ticks = iter((0, 311))
    monkeypatch.setattr(bridge, 'time', SimpleNamespace(time=time.time,
        monotonic=lambda: next(ticks), sleep=lambda _: pytest.fail('Unexpected polling')))
    store = TaskStore(tmp_path / 'chat.sqlite3')
    task = store.create_task(GOAL)
    model = ScriptedModel(call('create_self_edit_candidate', ARGS), final('Unavailable'))
    runner = AgentRunner(model=model,
        candidate_service_factory=lambda _: pytest.fail('Production must not fall back locally'))
    runner.run(GOAL, task_id=task['id'], task_store=store)
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'denied'
    assert set(runner.registry.schemas()) >= {'list_files', 'read_text', 'git_status', 'git_diff'}
    assert not list(bridge.ROOT.iterdir())
    with store.database.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0


@pytest.mark.parametrize('name,arguments', [
    ('approve_and_apply_candidate', {'candidate_id': '00000000-0000-0000-0000-000000000001'}),
    ('approve_and_deploy_commit', {'commit': 'a' * 40})])
def test_ordinary_production_turn_cannot_call_apply_or_deploy(tmp_path, monkeypatch, name, arguments):
    monkeypatch.setenv('JARVIS_MODE', 'production')
    store = TaskStore(tmp_path / 'chat.sqlite3')
    task = store.create_task(GOAL)
    model = ScriptedModel(call(name, arguments), final('Unavailable'))
    AgentRunner(model=model).run(GOAL, task_id=task['id'], task_store=store)
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'unknown_tool'
    assert name not in {t['name'] for t in model.requests[0]['tools']}
