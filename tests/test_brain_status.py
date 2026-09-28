"""Task-scoped runtime introspection is evidence-based, bounded and read-only."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from core.agent import AgentRunner
from core.brain import BrainSettings, JarvisBrain
from core.brain_status import BrainStatusTools, read_current_brain
from core.orchestrator import Orchestrator
from core.task_store import TaskStore
from models.provider import ProviderRegistry, ProviderTurn
from tools import ToolRegistry
from test_agent import ScriptedModel, call, final
from test_brain import ScriptedProvider, execute


GOAL = 'Which brain and provider handled this turn?'


def inspect_call(call_id='inspect'):
    return call('inspect_current_brain', {}, call_id)


def result_from(request):
    history = request.input if hasattr(request, 'input') else request['input']
    return json.loads(history[-1]['output'])['data']


def test_enabled_chat_confirms_brain_and_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('JARVIS_MODE', 'production')
    provider = ScriptedProvider(inspect_call(), final('I cannot access routing metadata.'))
    provider.name = 'openai'
    outcome, store, task, _ = execute(tmp_path, provider, goal=GOAL, settings=BrainSettings(True))
    status = result_from(provider.requests[1])
    assert status['available'] and status['recording_status'] == 'recorded'
    assert status['jarvisbrain_enabled'] is True and status['feature_flag_state'] == '1'
    assert status['brain_path'] == 'jarvisbrain'
    assert status['provider_selected'] == status['provider_used'] == 'openai'
    assert status['fallback_used'] is False and status['fallback_reason'] is None
    assert status['routing_decision_category'] == 'configured_provider'
    assert status['task_id'] == task['id'] and status['model_iteration'] == 1
    assert status['recorded_at'] <= status['latest_route_at'] <= status['observed_at']
    assert 'JarvisBrain is enabled' in outcome.response and 'Provider used: openai' in outcome.response
    assert 'cannot access' not in outcome.response
    assert 'Model iteration: 2' in outcome.response  # Final answer refreshes the snapshot.
    assert read_current_brain(store, task['id']).model_iteration == 2
    names = {tool['name'] for tool in provider.requests[0].tools}
    assert not names & {'approve_and_apply_candidate', 'approve_and_deploy_commit'}


@pytest.mark.parametrize('enabled', [False, True])
def test_production_http_chat_exposes_runtime_status(tmp_path, monkeypatch, enabled):
    import main
    from core.deployment import Settings
    monkeypatch.setenv('JARVIS_MODE', 'production')
    settings = Settings.from_env(dict(JARVIS_MODE='production', OPENAI_API_KEY='test-provider-key',
        JARVIS_AUTH_TOKEN='test-auth-' + 'x' * 32, JARVIS_PUBLIC_ORIGIN='https://jarvis.example.com',
        JARVIS_STATE_DIR=str(tmp_path / 'state'), JARVIS_DB_PATH=str(tmp_path / 'state/chat.sqlite3')))
    provider = ScriptedProvider(inspect_call(), final('Unknown'))
    legacy = ScriptedModel(inspect_call(), final('Unknown'))
    monkeypatch.setattr(main, 'orchestrator', Orchestrator(AgentRunner(model=legacy),
        brain_settings=BrainSettings(enabled, 'local'), providers=ProviderRegistry([provider])))
    with TestClient(main.create_app(settings=settings), base_url=settings.public_origin) as client:
        assert client.post('/task', json={'goal': GOAL}).status_code == 401
        response = client.post('/task', json={'goal': GOAL}, headers={'Authorization': 'Bearer ' + settings.auth_token})
    assert response.status_code == 200
    assert f'JarvisBrain is {"enabled" if enabled else "disabled"}' in response.json()['response']
    request = provider.requests[1] if enabled else legacy.requests[1]
    status = result_from(request)
    assert status['feature_flag_state'] == ('1' if enabled else '0')
    assert status['brain_path'] == ('jarvisbrain' if enabled else 'legacy')
    assert status['fallback_used'] is False
    assert status['routing_decision_category'] == ('configured_provider' if enabled else 'brain_disabled')


@pytest.mark.parametrize('failure,reason', [
    (RuntimeError('PRIVATE_PROVIDER_FAILURE'), 'provider_error'),
    (ProviderTurn([], 'PRIVATE_INCOMPLETE_PAYLOAD', 'incomplete'), 'provider_incomplete'),
])
def test_fallback_reports_selected_and_used_provider(tmp_path, failure, reason):
    legacy = ScriptedModel(inspect_call(), final('No fallback happened.'))
    provider = ScriptedProvider(failure)
    outcome, store, task, _ = execute(tmp_path, provider, legacy=legacy, goal=GOAL)
    status = result_from(legacy.requests[1])
    assert status['provider_selected'] == 'local' and status['provider_used'] == 'legacy'
    assert status['brain_path'] == 'legacy_fallback' and status['fallback_used'] is True
    assert status['fallback_reason'] == status['routing_decision_category'] == reason
    current = read_current_brain(store, task['id'])
    assert current.routing_decision_category == 'legacy_continuation'
    assert current.fallback_reason == reason
    assert 'Legacy fallback used: yes' in outcome.response
    assert f'Fallback reason: {reason}' in outcome.response
    assert 'PRIVATE_' not in json.dumps(status) + outcome.response


def test_unavailable_provider_never_exposes_configured_value(tmp_path):
    legacy = ScriptedModel(inspect_call(), final())
    outcome, _, _, _ = execute(tmp_path, ScriptedProvider(), legacy=legacy, goal=GOAL,
                              settings=BrainSettings(True, 'PRIVATE_CONFIG_VALUE'))
    status = result_from(legacy.requests[1])
    assert status['provider_selected'] is None and status['provider_used'] == 'legacy'
    assert status['fallback_reason'] == 'provider_unavailable'
    assert 'PRIVATE_CONFIG_VALUE' not in outcome.response + json.dumps(status)


def test_fallback_after_tool_snapshot_is_reported_in_final_answer(tmp_path):
    provider = ScriptedProvider(inspect_call(), RuntimeError('PRIVATE_EXCEPTION'))
    outcome, _, _, _ = execute(tmp_path, provider, goal=GOAL, legacy=ScriptedModel(final('Provider local, no fallback.')))
    assert result_from(provider.requests[1])['fallback_used'] is False
    assert 'Provider used: legacy' in outcome.response and 'Legacy fallback used: yes' in outcome.response
    assert 'Fallback reason: provider_error' in outcome.response


def test_missing_metadata_is_unknown_even_with_enabled_environment(monkeypatch):
    monkeypatch.setenv('JARVIS_BRAIN_ENABLED', '1')
    store = TaskStore()
    task = store.create_task(GOAL)
    # Old releases recorded routes without an effective-settings marker.
    store.add_routing_activity(task['id'], provider='openai', reason='configured_provider', iteration=1, status='completed')
    status = read_current_brain(store, task['id'])
    assert not status.available and status.recording_status == 'not_recorded'
    assert status.jarvisbrain_enabled is status.provider_used is status.fallback_used is None
    model = ScriptedModel(inspect_call(), final('The Brain is definitely disabled.'))
    outcome = AgentRunner(model=model).run(GOAL, task_id=task['id'], task_store=store)
    assert 'unavailable' in outcome.response and 'definitely disabled' not in outcome.response


def test_context_only_does_not_claim_provider_execution(tmp_path):
    outcome, store, task, _ = execute(tmp_path, ScriptedProvider(), goal='What did we talk about last time?')
    assert outcome.stop_reason == 'recall_not_found'
    status = read_current_brain(store, task['id'])
    assert status.available and status.jarvisbrain_enabled
    assert status.brain_path == 'not_recorded' and status.provider_used is None


def test_direct_runner_reuse_cannot_report_a_previous_brain_execution(tmp_path):
    _, store, task, _ = execute(tmp_path, ScriptedProvider(final('First execution')))
    assert read_current_brain(store, task['id']).provider_used == 'local'
    outcome = AgentRunner(model=ScriptedModel(inspect_call(), final('Wrong prior route'))).run(
        GOAL, task_id=task['id'], task_store=store)
    assert 'unavailable' in outcome.response
    assert read_current_brain(store, task['id']).recording_status == 'not_recorded'


def test_effective_cached_settings_win_over_later_environment_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('JARVIS_BRAIN_ENABLED', '1')
    store = TaskStore(tmp_path / 'state.db')
    task = store.create_task(GOAL)
    provider = ScriptedProvider(inspect_call(), final())
    provider.name = 'openai'
    brain = JarvisBrain(AgentRunner(), providers=ProviderRegistry([provider]))
    monkeypatch.setenv('JARVIS_BRAIN_ENABLED', '0')
    brain.run(GOAL, task_id=task['id'], task_store=store)
    assert read_current_brain(store, task['id']).feature_flag_state == '1'


def test_no_hidden_reasoning_prompts_payloads_or_private_trace_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv('JARVIS_AUTH_TOKEN', 'PRIVATE_AUTH_SENTINEL')
    monkeypatch.setenv('OPENAI_API_KEY', 'PRIVATE_KEY_SENTINEL')
    first = inspect_call()
    first.output.insert(0, dict(type='reasoning', encrypted_content='PRIVATE_ENCRYPTED_REASONING',
                              summary=[{'text': 'PRIVATE_CHAIN_OF_THOUGHT'}]))
    provider = ScriptedProvider(first, final('PRIVATE_MODEL_ANSWER'))
    outcome, store, task, _ = execute(tmp_path, provider, goal=GOAL)
    with store.database.transaction() as db:
        rows = db.execute("SELECT sequence,event FROM activity WHERE task_id=?", (task['id'],)).fetchall()
        for row in rows:
            event = json.loads(row['event'])
            event.update(system_prompt='PRIVATE_SYSTEM_PROMPT', payload='PRIVATE_RAW_PAYLOAD',
                         message='PRIVATE_TOOL_TRACE', exception='PRIVATE_EXCEPTION')
            db.execute('UPDATE activity SET event=? WHERE sequence=?', (json.dumps(event), row['sequence']))
    status = read_current_brain(store, task['id'])
    assert status.available
    assert 'PRIVATE_' not in status.model_dump_json() + outcome.response + json.dumps(result_from(provider.requests[1]))
    assert len(status.model_dump_json()) < 2000


@pytest.mark.parametrize('field,value', [('provider', 'PRIVATE_PROVIDER'), ('reason', 'PRIVATE_REASON'),
    ('timestamp', 'PRIVATE_TIMESTAMP'), ('iteration', True), ('status', 'PRIVATE_STATUS'),
    ('task_id', 'PRIVATE_TASK')])
def test_invalid_route_metadata_is_sanitized(field, value):
    store = TaskStore()
    task = store.create_task(GOAL)
    store.add_brain_runtime(task['id'], enabled=True, selected_provider='openai')
    store.add_routing_activity(task['id'], provider='openai', reason='configured_provider', iteration=1, status='completed')
    with store.database.transaction() as db:
        row = db.execute('SELECT sequence,event FROM activity ORDER BY sequence DESC LIMIT 1').fetchone()
        event = json.loads(row['event'])
        event[field] = value
        db.execute('UPDATE activity SET event=? WHERE sequence=?', (json.dumps(event), row['sequence']))
    result = read_current_brain(store, task['id'])
    assert not result.available and result.recording_status == 'invalid_metadata'
    assert 'PRIVATE_' not in result.model_dump_json()


def test_unavailable_database_returns_no_exception_text():
    class Broken:
        @property
        def database(self):
            raise RuntimeError('PRIVATE_DB_EXCEPTION')
    result = read_current_brain(Broken(), '00000000-0000-0000-0000-000000000001')
    assert result.recording_status == 'unavailable' and 'PRIVATE_' not in result.model_dump_json()


@pytest.mark.parametrize('kind', ['invalid_runtime', 'oversized_event', 'too_many_routes'])
def test_corrupt_or_excessive_records_are_bounded_and_never_exposed(kind):
    store = TaskStore()
    task = store.create_task(GOAL)
    store.add_brain_runtime(task['id'], enabled=True, selected_provider='openai')
    if kind == 'too_many_routes':
        for _ in range(257):
            store.add_routing_activity(task['id'], provider='openai', reason='configured_provider', iteration=1, status='completed')
    else:
        with store.database.transaction() as db:
            row = db.execute('SELECT sequence,event FROM activity ORDER BY sequence DESC LIMIT 1').fetchone()
            event = json.loads(row['event'])
            event.update({'enabled': 'PRIVATE_INVALID_FLAG'} if kind == 'invalid_runtime' else {'payload': 'PRIVATE_' * 1000})
            db.execute('UPDATE activity SET event=? WHERE sequence=?', (json.dumps(event), row['sequence']))
    result = read_current_brain(store, task['id'])
    assert not result.available and result.recording_status == 'invalid_metadata'
    assert len(result.model_dump_json()) < 2000 and 'PRIVATE_' not in result.model_dump_json()


def test_reader_is_read_only_and_tool_has_no_target_or_mutation_arguments(tmp_path):
    store = TaskStore(tmp_path / 'state.db')
    task = store.create_task(GOAL)
    store.add_brain_runtime(task['id'], enabled=True, selected_provider='openai')
    store.add_routing_activity(task['id'], provider='openai', reason='configured_provider', iteration=1, status='completed')
    tools = BrainStatusTools(store, task['id'])
    registry = ToolRegistry()
    scoped = tools.bind(registry)
    assert set(scoped.schemas()) - set(registry.schemas()) == {'inspect_current_brain'}
    schema = scoped.schemas()['inspect_current_brain']['arguments']
    assert schema['properties'] == {} and schema['additionalProperties'] is False
    before = deepcopy(store.get_task(task['id']))
    assert tools.inspect().available
    assert store.get_task(task['id']) == before
    for args in ({'task_id': 'other'}, {'enabled': False}, {'action': 'deploy'}, {'include_reasoning': True}):
        response = scoped.execute('inspect_current_brain', json.dumps(args), task_id=task['id'], task_store=store)
        assert response.error.code == 'invalid_arguments'
    assert read_current_brain(store, task['id']).feature_flag_state == '1'
    assert not set(scoped.schemas()) & {'approve_and_apply_candidate', 'approve_and_deploy_commit'}


def test_metadata_is_task_local_survives_restart_and_resets_on_rerun(tmp_path):
    store = TaskStore(tmp_path / 'state.db')
    first, second = store.create_task('First'), store.create_task('Second')
    for task, provider in ((first, 'openai'), (second, 'local')):
        store.add_brain_runtime(task['id'], enabled=True, selected_provider=provider)
        store.add_routing_activity(task['id'], provider=provider, reason='configured_provider', iteration=1, status='completed')
    first_id, second_id = first['id'], second['id']
    store.close()
    reopened = TaskStore(tmp_path / 'state.db')
    assert read_current_brain(reopened, first_id).provider_used == 'openai'
    assert read_current_brain(reopened, second_id).provider_used == 'local'
    reopened.add_brain_runtime(first_id, enabled=False, selected_provider='legacy')
    status = read_current_brain(reopened, first_id)
    assert status.jarvisbrain_enabled is False and status.provider_used is None
    assert status.brain_path == 'not_recorded'


@pytest.mark.parametrize('changes', [dict(enabled='PRIVATE'), dict(selected_provider='PRIVATE'),
                                  dict(enabled=True, selected_provider='legacy'), dict(enabled=False, selected_provider='openai')])
def test_runtime_writer_accepts_only_safe_configuration(changes):
    store = TaskStore()
    task = store.create_task(GOAL)
    with pytest.raises(ValueError):
        store.add_brain_runtime(task['id'], **(dict(enabled=True, selected_provider='openai') | changes))
    assert not read_current_brain(store, task['id']).available
