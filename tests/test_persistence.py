"""SQLite durability, API compatibility, isolation and explicit memory boundaries."""

import json
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

import main
from core.agent import AgentRunner
from core.database import Database
from core.orchestrator import Orchestrator
from core.task_store import TaskStore
from models.openai_client import AgentTurn
from tools import RepositoryTools, ToolRegistry


def test_repeated_initialization_and_future_schema(tmp_path):
    path = tmp_path / 'state.db'
    db = Database(path)
    db.initialize()
    db.initialize()
    db.close()
    db.initialize()
    with db.transaction() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        connection.execute('PRAGMA user_version=4')
    db.close()
    with pytest.raises(ValueError, match='schema'):
        Database(path).initialize()


def test_store_restart_tasks_activity_messages_and_memory(tmp_path):
    path = tmp_path / 'state.db'
    store = TaskStore(path)
    task = store.create_task('Inspect')
    (tmp_path / 'note.txt').write_text('RAW_OUTPUT_NOT_IN_AUDIT')
    registry = ToolRegistry(RepositoryTools(tmp_path))
    registry.execute('read_text', '{"path":"note.txt"}', task_id=task['id'], task_store=store)
    store.update_task(task['id'], status='completed', steps=[{'id': 1}], response='Done',
                      agent={'iterations': 1})
    memory = store.memory.create_memory(scope='project', scope_id='jarvis', content='Use SQLite')
    expected = json.loads(json.dumps(task))
    store.close()
    restarted = TaskStore(path)
    assert restarted.get_task(task['id']) == expected
    assert 'RAW_OUTPUT' not in json.dumps(restarted.get_task(task['id'])['activity'])
    history = restarted.memory.conversation_history(task['conversation_id'])
    assert [message['role'] for message in history] == ['user', 'assistant']
    assert [message['content'] for message in history] == ['Inspect', 'Done']
    assert all(message['created_at'] and message['id'] for message in history)
    assert restarted.memory.get_memory(memory['id']) == memory
    assert restarted.memory.delete_memory(memory['id'])
    restarted.close()
    final = TaskStore(path)
    assert final.memory.get_memory(memory['id']) is None
    assert not final.memory.delete_memory(memory['id'])
    final.close()


def test_order_is_stable_with_equal_timestamps_and_conversations_are_isolated(monkeypatch):
    store = TaskStore()
    monkeypatch.setattr(store, 'timestamp', lambda: '2026-01-01T00:00:00')
    first = store.create_task('First')
    store.update_task(first['id'], response='Answer')
    second = store.create_task('Next', first['conversation_id'])
    other = store.create_task('Unrelated')
    assert [m['content'] for m in store.memory.conversation_history(first['conversation_id'])] == ['First', 'Answer', 'Next']
    assert [m['content'] for m in store.memory.conversation_history(other['conversation_id'])] == ['Unrelated']
    assert [t['id'] for t in store.memory.task_history(first['conversation_id'])] == [first['id'], second['id']]
    assert store.memory.list_memories(scope='user', scope_id='local') == []


def test_agent_uses_only_selected_conversation_after_restart(tmp_path):
    path = tmp_path / 'state.db'
    store = TaskStore(path)
    first = store.create_task('Remember this conversation')
    store.update_task(first['id'], response='Understood')
    store.create_task('OTHER_CONVERSATION')
    store.close()
    restarted = TaskStore(path)
    task = restarted.create_task('Continue', first['conversation_id'])
    requests = []
    def model(**request):
        requests.append(request)
        return AgentTurn([], 'Continued')
    AgentRunner(model=model).run(task['goal'], task_id=task['id'], task_store=restarted)
    assert requests[0]['input'] == [
        {'role': 'user', 'content': 'Remember this conversation'},
        {'role': 'assistant', 'content': 'Understood'},
        {'role': 'user', 'content': 'Continue'}]
    restarted.close()


def test_app_restart_and_history_api(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    monkeypatch.setattr(main, 'orchestrator', Orchestrator(AgentRunner(model=lambda **kw: AgentTurn([], 'Hello'))))
    with TestClient(main.create_app(path)) as client:
        task = client.post('/task', json={'goal': 'Hi'}).json()
        cid = task['conversation_id']
        memory = client.post('/memories', json={'scope': 'user', 'scope_id': 'local', 'content': 'Prefer concise replies'}).json()
    with TestClient(main.create_app(path)) as client:
        assert client.get('/tasks/' + task['id']).json() == task
        assert client.get('/activity').json()['activity']
        assert client.get('/conversations/' + cid).json()['id'] == cid
        assert client.get('/conversations').json()['conversations'][0]['id'] == cid
        history = client.get('/conversations/' + cid + '/messages').json()['messages']
        assert [m['role'] for m in history] == ['user', 'assistant']
        continued = client.post('/task', json={'goal': 'Again', 'conversation_id': cid}).json()
        assert continued['conversation_id'] == cid
        assert len(client.get('/conversations/' + cid + '/messages').json()['messages']) == 4
        assert client.get('/memories/' + memory['id']).json() == memory
        assert client.get('/memories?scope=user&scope_id=local').json()['memories'] == [memory]
        assert client.get('/memories?scope=project&scope_id=local').json()['memories'] == []
        assert client.delete('/memories/' + memory['id']).json() == {'deleted': True}
        assert client.get('/memories/' + memory['id']).status_code == 404


@pytest.mark.parametrize('bad_id', ['missing', "' OR 1=1 --", str(uuid4())])
def test_invalid_ids_do_not_create_or_expose_data(tmp_path, bad_id):
    with TestClient(main.create_app(tmp_path / 'app.db')) as client:
        assert client.get('/tasks/' + bad_id).status_code == 404
        assert client.get('/conversations/' + bad_id + '/messages').status_code == 404
        assert client.get('/memories/' + bad_id).status_code == 404
        assert client.delete('/memories/' + bad_id).status_code == 404
        assert client.post('/task', json={'goal': 'Hi', 'conversation_id': bad_id}).status_code == 404
        assert client.get('/tasks').json() == {'tasks': []}
        assert client.get('/conversations').json() == {'conversations': []}


@pytest.mark.parametrize('payload', [{}, {'scope': 'system', 'scope_id': 'x', 'content': 'a'},
    {'scope': 'user', 'scope_id': '', 'content': 'a'},
    {'scope': 'user', 'scope_id': 'x', 'content': '   '},
    {'scope': 'user', 'scope_id': 'x', 'content': 7},
    {'scope': 'user', 'scope_id': 'x', 'content': 'a', 'auto': True},
    {'scope': 'user', 'scope_id': 'x', 'content': 'a\x00b'}])
def test_malformed_memory(tmp_path, payload):
    with TestClient(main.create_app(tmp_path / 'app.db')) as client:
        assert client.post('/memories', json=payload).status_code == 422
        assert client.get('/memories?scope=user&scope_id=x').json() == {'memories': []}


@pytest.mark.parametrize('payload', [{}, {'goal': ''}, {'goal': '  '}, {'goal': []},
                                     {'goal': 'a\x00b'}, {'goal': 'Hi', 'conversation_id': 4}])
def test_malformed_task(tmp_path, payload):
    with TestClient(main.create_app(tmp_path / 'app.db')) as client:
        assert client.post('/task', json=payload).status_code == 422
        assert client.get('/tasks').json() == {'tasks': []}


def test_transactions_rollback_and_multiple_stores(tmp_path):
    path = tmp_path / 'state.db'
    one, two = TaskStore(path), TaskStore(path)
    task = one.create_task('Hi')
    two.set_status(task['id'], 'running')
    assert one.get_task(task['id'])['status'] == 'running'
    with pytest.raises(sqlite3.IntegrityError), one.database.transaction() as db:
        db.execute('INSERT INTO conversations VALUES (?,?)', ('rolled-back', 'now'))
        db.execute("INSERT INTO activity(task_id,event) VALUES ('missing','{}')")
    with one.database.transaction() as db:
        assert db.execute("SELECT * FROM conversations WHERE id='rolled-back'").fetchone() is None
    for change in ({'steps': 'bad'}, {'agent': []}, {'unknown': 1}, {'response': ''}):
        with pytest.raises(ValueError):
            one.update_task(task['id'], **change)
    one.close()
    two.close()


def test_database_cannot_be_placed_in_tool_visible_repository():
    from pathlib import Path
    with pytest.raises(ValueError, match='outside'):
        TaskStore(Path(__file__).resolve().parents[1] / 'private.db')


def test_failed_agent_is_durable_and_does_not_create_permanent_memory(tmp_path, monkeypatch):
    path = tmp_path / 'failed.db'
    def fail(**request):
        raise RuntimeError('SECRET_PROVIDER_ERROR')
    monkeypatch.setattr(main, 'orchestrator', Orchestrator(AgentRunner(model=fail)))
    with TestClient(main.create_app(path)) as client:
        response = client.post('/task', json={'goal': 'Inspect'})
        assert response.status_code == 500
        cid = response.headers['X-Conversation-ID']
    with TestClient(main.create_app(path)) as client:
        assert client.get('/tasks').json()['tasks'][0]['status'] == 'failed'
        history = client.get('/conversations/' + cid + '/messages')
        assert [m['role'] for m in history.json()['messages']] == ['user', 'assistant']
        assert 'SECRET' not in history.text + client.get('/activity').text
        assert client.get('/memories?scope=user&scope_id=local').json()['memories'] == []


def test_memory_interface_validation_and_scope_isolation():
    store = TaskStore()
    memory = store.memory
    saved = memory.create_memory(scope='user', scope_id='one', content='Explicit')
    assert memory.list_memories(scope='user', scope_id='two') == []
    assert memory.list_memories(scope='project', scope_id='one') == []
    assert memory.get_memory(saved['id']) == saved
    for kwargs in ({'scope': 'automatic', 'scope_id': 'x', 'content': 'a'},
                   {'scope': 'user', 'scope_id': 3, 'content': 'a'},
                   {'scope': 'user', 'scope_id': 'x', 'content': ''}):
        with pytest.raises(ValueError):
            memory.create_memory(**kwargs)
    with pytest.raises(ValueError):
        memory.conversation_history('missing')
    with pytest.raises(ValueError):
        memory.task_history('missing')


def test_concurrent_initialization_and_writes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'concurrent.db'
    def create(index):
        store = TaskStore(path)
        try:
            return store.create_task(f'Task {index}')['id']
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(create, range(8)))
    store = TaskStore(path)
    assert {task['id'] for task in store.list_tasks()} == set(ids)
    assert len(store.memory.list_conversations()) == 8
    store.close()


def test_plan_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / 'plan.db'
    steps = [{'id': 1, 'description': 'Inspect', 'agent': 'reviewer'}]
    monkeypatch.setattr(main.orchestrator, 'create_plan', lambda goal: {'steps': steps})
    with TestClient(main.create_app(path)) as client:
        task = client.post('/task', json={'goal': 'Plan', 'mode': 'plan'}).json()
    with TestClient(main.create_app(path)) as client:
        assert client.get('/tasks/' + task['id']).json() == task
        assert task['status'] == 'planned' and task['steps'] == steps
        history = client.get('/conversations/' + task['conversation_id'] + '/messages').json()['messages']
        assert json.loads(history[-1]['content']) == {'steps': steps}
