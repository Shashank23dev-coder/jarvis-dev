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
GOAL = 'Build Phase 1 of JarvisBrain'


def request(action='create', goal=GOAL, arguments=None, **extra):
    return dict(request_id=str(uuid4()), task_id=str(uuid4()), conversation_id=str(uuid4()),
                goal=goal, action=action, arguments=ARGS if arguments is None else arguments,
                created=time.time(), **extra)


def test_ordinary_production_chat_routes_to_dev_and_audits(setup, tmp_path, monkeypatch):
    source, db, factory = setup
    broker = Broker(factory())
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
    assert 'create_self_edit_candidate' in names
    assert not names & {'approve_and_apply_candidate', 'approve_and_deploy_commit'}
    data = json.loads(model.requests[1]['input'][-1]['output'])['data']
    record = factory().store.get(data['candidate_id'], internal=True)
    assert record['source_context']['task_id'] == task['id']
    assert record['source_context']['conversation_id'] == task['conversation_id']
    assert record['source_context']['source'] == 'production_chat'
    assert Path(record['worktree']).is_relative_to(tmp_path / 'candidates')
    assert (Path(record['worktree']) / 'src/app.py').read_text() == 'value = 2\n'
    assert tree_bytes(source) == before and tree_bytes(production) == untouched
    assert broker.execute(requests[0])['id'] == record['id']
    assert 'PRIVATE SENTINEL' not in json.dumps(data)
    assert 'worktree' not in project(record)


@pytest.mark.parametrize('action', ['apply', 'deploy', 'command', 'start'])
def test_bridge_has_no_privileged_operations(setup, action):
    with pytest.raises(ToolDenied):
        Broker(setup[2]()).execute(request(action))


@pytest.mark.parametrize('goal', ['Only plan to build JarvisBrain', 'Do not modify your code', 'Hello', '"Build JarvisBrain"'])
def test_host_rechecks_authority(setup, goal):
    with pytest.raises(ToolDenied):
        Broker(setup[2]()).execute(request(goal=goal))


def test_protected_paths_and_sanitized_projection(setup):
    record = Broker(setup[2]()).execute(request(arguments=dict(ARGS, inspect_paths=['.env'])))
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
    broker = Broker(factory(tester=None))
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
