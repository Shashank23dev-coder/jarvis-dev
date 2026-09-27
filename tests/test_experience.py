"""Evidence-derived learning is bounded, durable, and cannot grant authority."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import main
from core.agent import AgentRunner
from core.candidate_store import CandidateStore
from core.deployment import Settings
from core.experience import ExperienceLimits, ExperienceStore, TaskReflection
from core.task_store import TaskStore
from tools.schemas import ProcessResult, ToolResult
from test_agent import ScriptedModel, call, final
from test_deployment import production


def observation(code=0, *, path='tests', name='run_pytest', output='RAW_TOOL_OUTPUT'):
    reflection = TaskReflection()
    result = ToolResult(ok=code == 0, error=None, data=ProcessResult(
        returncode=code, stdout=output, stderr=output, timed_out=False, truncated=False))
    args = {'paths': [path]} if name == 'run_pytest' else {'command': ['pwd']}
    reflection.observe(name, json.dumps(args), result)
    return reflection.observations[0]


def capture(store, goal='Run parser tests', observations=None, status='completed'):
    task = store.create_task(goal)
    store.update_task(task['id'], status=status, response='MODEL_CLAIMS_SUCCESS')
    record = store.experiences.capture_task(task, [observation()] if observations is None else observations)
    return task, record


@pytest.mark.parametrize('code,outcome', [(0, 'succeeded'), (1, 'failed')])
def test_grounded_success_and_failure(code, outcome):
    store = TaskStore()
    task, record = capture(store, observations=[observation(code)])
    assert record['outcome'] == outcome
    assert record['source_task_id'] == task['id']
    assert record['evidence']['operations'][0]['returncode'] == code
    assert set(record['reflection']) == {'what_happened', 'what_succeeded', 'what_failed', 'reuse_next_time'}
    assert 'RAW_TOOL_OUTPUT' not in json.dumps(record)
    assert 'MODEL_CLAIMS_SUCCESS' not in json.dumps(record)
    assert store.experiences.capture_task(task, [observation(code)])['id'] == record['id']
    assert len(store.experiences.list()) == 1


def test_recovery_requires_same_check_and_inspection_is_not_test_success():
    store = TaskStore()
    assert capture(store, observations=[observation(1), observation(0, path='other')])[1]['outcome'] == 'failed'
    assert capture(store, observations=[observation(1), observation(0)])[1]['outcome'] == 'recovered'
    assert capture(store, observations=[observation(name='run_command')])[1]['outcome'] == 'observed'
    assert capture(store, observations=[])[1]['outcome'] == 'unverified'
    assert capture(store, status='failed')[1]['outcome'] == 'failed'


@pytest.mark.parametrize('goal', ['Hello', 'What is the capital of France?', 'What did we talk about last time?',
    'Fix .env credentials', 'Fix password=hidden', 'Inspect /etc/passwd', 'Debug sk-abcdefghijklmnop',
    'Fix ' + 'x' * 250 + ' secret=tail'])
def test_no_casual_or_protected_experiences(goal):
    assert capture(TaskStore(), goal)[1] is None


def test_environment_secret_and_raw_arguments_excluded(monkeypatch):
    monkeypatch.setenv('JARVIS_AUTH_TOKEN', 'private-runtime-value')
    store = TaskStore()
    assert capture(store, 'Fix private-runtime-value')[1] is None
    _, record = capture(store, observations=[observation(path='.env', output='private-runtime-value')])
    assert '.env' not in json.dumps(record)
    assert 'private-runtime-value' not in json.dumps(record)
    _, old = capture(store, 'Fix newly-sensitive-value')
    monkeypatch.setenv('JARVIS_AUTH_TOKEN', 'newly-sensitive-value')
    assert store.experiences.get(old['id']) is None
    assert old['id'] not in [r['id'] for r in store.experiences.list()]


def test_similarity_ranking_recency_quality_and_failed_labels(monkeypatch):
    store = TaskStore()
    monkeypatch.setattr('core.experience.timestamp', lambda: '2026-01-01T00:00:00+00:00')
    _, old = capture(store)
    monkeypatch.setattr('core.experience.timestamp', lambda: '2026-09-26T00:00:00+00:00')
    _, recent = capture(store)
    _, failure = capture(store, observations=[observation(1)])
    capture(store, 'Run renderer tests')
    service = ExperienceStore(store.database, now=datetime(2026, 9, 26, tzinfo=timezone.utc))
    results = service.search('Run parser tests')['results']
    assert results[0]['id'] == recent['id']
    assert {r['id'] for r in results} == {old['id'], recent['id'], failure['id']}
    failed = next(r for r in results if r['outcome'] == 'failed')
    assert failed['outcome_label'] == 'FAILED' and not failed['recommendation']
    assert not service.search('Run unrelated tests')['results']


def test_bounded_context_and_scan():
    store = TaskStore()
    for _ in range(8):
        capture(store)
    result = ExperienceStore(store.database, limits=ExperienceLimits(results=2, context_bytes=2200)).search('Run parser tests')
    assert len(json.dumps(result, separators=(',', ':'))) <= 2200
    assert 0 < len(result['results']) <= 2 and result['truncated']
    assert len(ExperienceStore(store.database, limits=ExperienceLimits(scan_rows=1)).search('Run parser tests')['results']) == 1
    with pytest.raises(ValueError):
        ExperienceLimits(results=6)


class Registry:
    code = 1

    def model_tools(self):
        return []

    def execute(self, *args, **kwargs):
        return ToolResult(ok=self.code == 0, error=None, data=ProcessResult(
            returncode=self.code, stdout='CURRENT_FAILURE', stderr='', timed_out=False, truncated=False))


@pytest.mark.parametrize('code,outcome', [(0, 'succeeded'), (1, 'failed')])
def test_agent_injection_current_evidence_priority_and_post_task_capture(code, outcome):
    store = TaskStore()
    capture(store)
    task = store.create_task('Run parser tests')
    model = ScriptedModel(call('run_pytest', {'paths': ['tests']}), final('Everything passed!'))
    registry = Registry()
    registry.code = code
    result = AgentRunner(registry, model=model).run(task['goal'], task_id=task['id'], task_store=store)
    assert result.status == 'completed'
    request = model.requests[-1]
    assert 'ALWAYS override old experience' in request['instructions']
    assert json.loads(request['input'][0]['content'])['source'] == 'task_experience'
    assert request['input'][1] == {'role': 'user', 'content': 'Run parser tests'}
    assert 'CURRENT_FAILURE' in request['input'][-1]['output']
    assert store.experiences.list()[0]['outcome'] == outcome


def test_casual_chat_does_not_search_or_capture(monkeypatch):
    store = TaskStore()
    monkeypatch.setattr(ExperienceStore, 'search', lambda *a, **kw: pytest.fail('unnecessary search'))
    task = store.create_task('Hello')
    model = ScriptedModel(final('Hello!'))
    AgentRunner(model=model).run(task['goal'], task_id=task['id'], task_store=store)
    assert store.experiences.list() == []
    assert len(model.requests[0]['input']) == 1


def test_capture_failure_does_not_change_task_outcome(monkeypatch):
    store = TaskStore()
    monkeypatch.setattr(ExperienceStore, 'capture_task', lambda *a: (_ for _ in ()).throw(RuntimeError('secret')))
    task = store.create_task('Inspect parser')
    result = AgentRunner(model=ScriptedModel(final())).run(task['goal'], task_id=task['id'], task_store=store)
    assert result.status == 'completed'


def reviewed(store, verdict='passed'):
    candidates = CandidateStore(store.database)
    record = candidates.create('Fix parser', ['core/parser.py'], ['tests'])
    record.update(state=verdict, reviewer_result={'verdict': verdict, 'summary': 'SECRET_REVIEW_TEXT'},
                  reviewed_fingerprint='bound', tested_fingerprint='bound',
                  test_results=[dict(phase=p, returncode=0, timed_out=False, truncated=False,
                                     fingerprint='bound', stdout='SECRET_OUTPUT', stderr='') for p in ['targeted', 'full_python']])
    candidates.save(record, 'review_finished')
    return candidates, record


@pytest.mark.parametrize('verdict,outcome', [('passed', 'succeeded'), ('needs_revision', 'failed'), ('rejected', 'failed')])
def test_candidate_review_grounding(verdict, outcome):
    store = TaskStore()
    _, record = reviewed(store, verdict)
    lesson = store.experiences.list()[0]
    assert lesson['source_candidate_id'] == record['id'] and lesson['outcome'] == outcome
    assert 'SECRET_' not in json.dumps(lesson)


def test_candidate_binding_rollback_and_learning_failure(monkeypatch):
    store = TaskStore()
    candidates, record = reviewed(store)
    record['tested_fingerprint'] = 'changed'
    candidates.save(record, 'review_finished')
    assert store.experiences.list()[0]['outcome'] == 'failed'
    record['apply'] = dict(status='rolled_back', rollback=True, verification=[])
    candidates.save(record, 'apply_rolled_back')
    assert store.experiences.list()[0]['outcome'] == 'failed'
    assert store.experiences.list()[0]['evidence']['rollback_restored']
    monkeypatch.setattr(ExperienceStore, 'capture_candidate', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    candidates.save(record, 'apply_rolled_back')
    assert candidates.get(record['id'])['apply']['status'] == 'rolled_back'


def test_restart_persistence_and_separation(tmp_path):
    path = tmp_path / 'state.db'
    store = TaskStore(path)
    task, record = capture(store)
    history = store.memory.conversation_history(task['conversation_id'])
    store.close()
    restarted = TaskStore(path)
    assert restarted.experiences.get(record['id']) == record
    assert restarted.memory.conversation_history(task['conversation_id']) == history
    with restarted.database.transaction() as db:
        assert db.execute('SELECT count(*) FROM memories').fetchone()[0] == 0
    assert restarted.experiences.delete(record['id'])
    assert restarted.get_task(task['id'])
    assert restarted.memory.conversation_history(task['conversation_id']) == history


def test_authenticated_apis(production):
    app = main.create_app(settings=Settings.from_env(production))
    _, record = capture(app.state.store)
    with TestClient(app, base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        headers = {'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']}
        for path in ['/experiences', '/experiences/search?q=parser', '/experiences/' + record['id']]:
            assert client.get(path).status_code == 401
            assert client.get(path, headers=headers).status_code == 200
        assert client.delete('/experiences/' + record['id']).status_code == 401
        assert client.delete('/experiences/' + record['id'], headers=headers).json() == {'deleted': True}
        assert client.get('/experiences/' + record['id'], headers=headers).status_code == 404
        assert client.get('/experiences/search?q=parser&limit=99', headers=headers).status_code == 422


def test_v2_migration_preserves_existing_state(tmp_path):
    path = tmp_path / 'state.db'
    store = TaskStore(path)
    task = store.create_task('Existing task')
    memory = store.memory.create_memory(scope='user', scope_id='owner', content='Prefers concise answers')
    candidates, candidate = reviewed(store)
    candidate['apply'] = {'status': 'rolled_back', 'rollback': True}
    candidates.save(candidate, 'apply_rolled_back')
    before = candidates.get(candidate['id'])
    with store.database.transaction() as db:
        for table in ('experience_evaluations', 'experience_uses', 'experience_quality'):
            db.execute('DROP TABLE ' + table)
        db.execute('DROP TABLE experiences')
        db.execute('PRAGMA user_version=2')
    store.close()
    restarted = TaskStore(path)
    assert restarted.get_task(task['id'])['goal'] == 'Existing task'
    assert restarted.memory.get_memory(memory['id']) == memory
    assert CandidateStore(restarted.database).get(candidate['id']) == before
    assert restarted.experiences.list() == []


def test_candidate_evidence_bound_does_not_hide_failure():
    store = TaskStore()
    candidates, record = reviewed(store)
    failed = dict(record['test_results'][0], returncode=1)
    record['test_results'] = [failed] + record['test_results'] * 4
    candidates.save(record, 'review_finished')
    lesson = store.experiences.list()[0]
    assert lesson['outcome'] == 'failed'
    assert len(lesson['evidence']['tests']) == 6
    assert lesson['evidence']['tests_total'] == 9
    assert lesson['evidence']['omitted'] == 3


@pytest.mark.parametrize('query', ['   ', 'parser\x00'])
def test_invalid_experience_search_returns_validation_error(tmp_path, query):
    app = main.create_app(tmp_path / 'state.db')
    with TestClient(app) as client:
        assert client.get('/experiences/search', params={'q': query}).status_code == 422


@pytest.mark.parametrize('flag', ['timed_out', 'truncated'])
def test_incomplete_check_vetoes_zero_exit_success(flag):
    reflection = TaskReflection()
    data = dict(returncode=0, stdout='', stderr='', timed_out=False, truncated=False)
    data[flag] = True
    reflection.observe('run_pytest', '{"paths":["tests"]}',
                       ToolResult(ok=True, error=None, data=ProcessResult(**data)))
    assert capture(TaskStore(), observations=reflection.observations)[1]['outcome'] == 'failed'


def test_retrieval_excludes_current_and_future_tasks(monkeypatch):
    store = TaskStore()
    monkeypatch.setattr('core.experience.timestamp', lambda: '2026-01-01T00:00:00+00:00')
    _, past = capture(store)
    current, _ = capture(store)
    monkeypatch.setattr('core.experience.timestamp', lambda: '2026-01-03T00:00:00+00:00')
    capture(store)
    result = store.experiences.search('Run parser tests',
        before_time='2026-01-02T00:00:00+00:00', exclude_task_id=current['id'])
    assert [item['id'] for item in result['results']] == [past['id']]


@pytest.mark.parametrize('verified,outcome', [(True, 'succeeded'), (False, 'failed')])
def test_apply_success_requires_post_apply_verification(verified, outcome):
    store = TaskStore()
    candidates, record = reviewed(store)
    record['apply'] = dict(status='applied', post_apply_verified=verified,
                           verification=record['test_results'], rollback=False)
    candidates.save(record, 'apply_succeeded')
    lesson = store.experiences.list()[0]
    assert lesson['task_type'] == 'candidate_apply'
    assert lesson['outcome'] == outcome
