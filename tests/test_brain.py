"""Brain routing is independent of model APIs and cannot expand tool authority."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

from core.agent import AgentRunner
from core.brain import BrainSettings, JarvisBrain
from core.orchestrator import Orchestrator
from core.task_context import TaskContext
from core.task_store import TaskStore
from models.provider import ProviderRegistry, ProviderRequest, ProviderTurn
from models.openai_provider import OpenAIProvider
from models import openai_client
from tools import RepositoryTools, ToolRegistry
from test_agent import ScriptedModel, call, final


class ScriptedProvider:
    name = 'local'

    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []

    def complete(self, request):
        self.requests.append(deepcopy(request))
        result = next(self.turns)
        if isinstance(result, Exception):
            raise result
        return result


def routes(store, task_id):
    return [event for event in store.get_task(task_id)['activity'] if event['type'] == 'brain_route']


def execute(tmp_path, provider, *, legacy=None, goal='Hello', settings=None, **limits):
    store = TaskStore(tmp_path / 'chat.db')
    task = store.create_task(goal)
    runner = AgentRunner(ToolRegistry(RepositoryTools(tmp_path)),
                         model=legacy or ScriptedModel(final('Legacy answer')), **limits)
    brain = JarvisBrain(runner, settings=settings or BrainSettings(True, 'local'),
                        providers=ProviderRegistry([provider]))
    outcome = brain.run(goal, task_id=task['id'], task_store=store)
    return outcome, store, task, brain


@pytest.mark.parametrize('value,enabled', [(None, False), ('0', False), ('1', True),
                                         ('true', False), ('false', False), ('invalid', False)])
def test_flag_is_explicit_and_default_off(monkeypatch, value, enabled):
    monkeypatch.delenv('JARVIS_BRAIN_ENABLED', raising=False)
    monkeypatch.delenv('JARVIS_BRAIN_PROVIDER', raising=False)
    if value is not None:
        monkeypatch.setenv('JARVIS_BRAIN_ENABLED', value)
    settings = BrainSettings.from_env()
    assert settings.enabled is enabled and settings.provider == 'openai'


@pytest.mark.parametrize('enabled', [False, True])
def test_orchestrator_selects_flagged_path(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv('JARVIS_BRAIN_ENABLED', '1' if enabled else '0')
    monkeypatch.setenv('JARVIS_BRAIN_PROVIDER', 'local')
    provider = ScriptedProvider(final('Brain answer'))
    legacy = ScriptedModel(final('Legacy answer'))
    store = TaskStore()
    task = store.create_task('Hello')
    runner = AgentRunner(model=legacy)
    orchestrator = Orchestrator(runner, providers=ProviderRegistry([provider]))
    outcome = orchestrator.run_goal(task['goal'], task_id=task['id'], task_store=store)
    assert outcome.response == ('Brain answer' if enabled else 'Legacy answer')
    assert bool(provider.requests) is enabled
    assert bool(legacy.requests) is not enabled
    assert routes(store, task['id'])
    assert routes(store, task['id'])[0]['reason'] == ('configured_provider' if enabled else 'brain_disabled')
    assert runner.model is legacy


def test_brain_receives_normalized_context_and_configured_route(tmp_path):
    provider = ScriptedProvider(final())
    _, store, task, brain = execute(tmp_path, provider)
    context = TaskContext.from_task(task['goal'], task)
    assert context.task_id == task['id'] and context.conversation_id == task['conversation_id']
    assert brain.route(context) is provider
    assert provider.requests[0].input == [{'role': 'user', 'content': 'Hello'}]
    assert [event['status'] for event in routes(store, task['id'])] == ['started', 'completed']
    with pytest.raises(ValueError):
        brain.run('Hello', task_id='missing', task_store=store)


@pytest.mark.parametrize('name', ['claude', 'codex', 'gemini', 'local', 'unknown-secret-value'])
def test_uninstalled_provider_uses_legacy_without_dynamic_import(tmp_path, name):
    result, store, task, _ = execute(tmp_path, OpenAIProvider(), settings=BrainSettings(True, name))
    assert result.response == 'Legacy answer'
    events = routes(store, task['id'])
    assert events[0]['provider'] == 'legacy' and events[0]['reason'] == 'provider_unavailable'
    assert 'unknown-secret-value' not in json.dumps(events)


@pytest.mark.parametrize('failure,reason', [
    (RuntimeError('SECRET_PROVIDER_EXCEPTION'), 'provider_error'),
    (ProviderTurn([], 'PRIVATE_INCOMPLETE_TEXT', 'incomplete'), 'provider_incomplete'),
    (object(), 'provider_error'),
])
def test_provider_failure_falls_back_without_leaking_details(tmp_path, failure, reason):
    provider = ScriptedProvider(failure)
    result, store, task, _ = execute(tmp_path, provider)
    assert result.status == 'completed' and result.response == 'Legacy answer'
    assert routes(store, task['id'])[2]['reason'] == reason
    assert 'SECRET' not in json.dumps(task) and 'PRIVATE_INCOMPLETE_TEXT' not in json.dumps(task)


def test_fallback_preserves_completed_tools_budgets_and_no_replay(tmp_path):
    (tmp_path / 'note.txt').write_text('Evidence')
    first = call('read_text', {'path': 'note.txt'})
    first.output.insert(0, {'type': 'reasoning', 'encrypted_content': 'PRIVATE_CONTINUATION', 'summary': []})
    provider = ScriptedProvider(first, RuntimeError('SECRET'))
    legacy = ScriptedModel(call('list_files', {}, 'call_2'), final('Done'))
    result, store, task, _ = execute(tmp_path, provider, legacy=legacy, max_tool_calls=2)
    assert result.status == 'completed' and result.tool_calls == 2 and result.iterations == 3
    assert len(provider.requests) == 2 and len(legacy.requests) == 2
    assert legacy.requests[0]['input'][-1]['call_id'] == 'call_1'
    assert 'Evidence' in legacy.requests[0]['input'][-1]['output']
    assert 'PRIVATE_CONTINUATION' not in json.dumps(legacy.requests)
    assert not legacy.requests[1]['allow_tools']
    assert [e['tool_name'] for e in task['activity'] if e['type'] == 'tool_started'] == ['read_text', 'list_files']
    assert routes(store, task['id'])[-1]['reason'] == 'legacy_continuation'
    assert 'Evidence' not in json.dumps(routes(store, task['id']))


def test_fallback_does_not_reset_call_identity_or_tool_limit(tmp_path):
    provider = ScriptedProvider(call(), RuntimeError('failure'))
    legacy = ScriptedModel(call(call_id='call_1'))
    result, _, _, _ = execute(tmp_path, provider, legacy=legacy)
    assert result.status == 'failed' and result.tool_calls == 1
    provider = ScriptedProvider(call(), RuntimeError('failure'))
    result, _, _, _ = execute(tmp_path, provider, legacy=ScriptedModel(call(call_id='call_2')), max_tool_calls=1)
    assert result.status == 'limit_reached' and result.tool_calls == 1


def test_fallback_preserves_new_legacy_continuations(tmp_path):
    first = call()
    first.output.insert(0, {'type': 'reasoning', 'encrypted_content': 'FOREIGN_OPAQUE', 'summary': []})
    second = call(call_id='second')
    second.output.insert(0, {'type': 'reasoning', 'encrypted_content': 'OPENAI_OPAQUE', 'summary': []})
    provider = ScriptedProvider(first, RuntimeError('failure'))
    legacy = ScriptedModel(second, final())
    outcome, _, _, _ = execute(tmp_path, provider, legacy=legacy)
    assert outcome.status == 'completed'
    assert 'FOREIGN_OPAQUE' not in json.dumps(legacy.requests)
    assert 'OPENAI_OPAQUE' in json.dumps(legacy.requests[1]['input'])


def test_openai_fallback_preserves_compatible_continuations(tmp_path):
    first = call()
    first.output.insert(0, {'type': 'reasoning', 'encrypted_content': 'OPENAI_OPAQUE', 'summary': []})
    provider = ScriptedProvider(first, RuntimeError('failure'))
    provider.name = 'openai'
    legacy = ScriptedModel(final())
    result, _, _, _ = execute(tmp_path, provider, legacy=legacy, settings=BrainSettings(True))
    assert result.status == 'completed'
    assert 'OPENAI_OPAQUE' in json.dumps(legacy.requests[0]['input'])


def test_failed_provider_cannot_mutate_legacy_request(tmp_path):
    class MutatingProvider:
        name = 'local'

        def complete(self, request):
            request.input.clear()
            request.tools.clear()
            raise RuntimeError('SECRET')

    legacy = ScriptedModel(final())
    execute(tmp_path, MutatingProvider(), legacy=legacy)
    assert legacy.requests[0]['input'] == [{'role': 'user', 'content': 'Hello'}]
    assert legacy.requests[0]['tools']


def test_both_providers_failing_is_bounded_and_redacted(tmp_path):
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('SECRET_FALLBACK')
    provider = ScriptedProvider(RuntimeError('SECRET_PRIMARY'))
    result, store, task, _ = execute(tmp_path, provider, legacy=fail)
    assert result.status == 'failed' and len(calls) == len(provider.requests) == 1
    assert routes(store, task['id'])[-1]['status'] == 'failed'
    assert 'SECRET' not in json.dumps(task)


@pytest.mark.parametrize('name,args,error', [
    ('read_text', {'path': '.env'}, 'denied'),
    ('read_text', {'path': '../outside'}, 'denied'),
    ('run_command', {'command': ['touch', 'escaped.txt']}, 'denied'),
    ('approve_and_apply_candidate', {'candidate_id': '0' * 36}, 'unknown_tool'),
    ('approve_and_deploy_commit', {'commit': 'a' * 40}, 'unknown_tool'),
])
def test_brain_provider_cannot_expand_authority(tmp_path, name, args, error):
    provider = ScriptedProvider(call(name, args), final())
    result, _, _, _ = execute(tmp_path, provider)
    assert result.status == 'completed'
    assert json.loads(provider.requests[1].input[-1]['output'])['error']['code'] == error
    assert not (tmp_path / 'escaped.txt').exists()


def test_production_candidate_and_approval_gates_are_preserved(tmp_path, monkeypatch):
    from core.candidate_bridge import CandidateClient
    monkeypatch.setenv('JARVIS_MODE', 'production')
    monkeypatch.setattr(CandidateClient, 'invoke', lambda *a: pytest.fail('Unauthorized bridge use'))
    provider = ScriptedProvider(call('create_self_edit_candidate', {
        'inspect_paths': ['main.py'], 'targeted_tests': ['tests']}), final())
    execute(tmp_path, provider)
    assert json.loads(provider.requests[1].input[-1]['output'])['error']['code'] == 'unknown_tool'
    names = {tool['name'] for tool in provider.requests[0].tools}
    assert 'create_self_edit_candidate' not in names
    assert not names & {'approve_and_apply_candidate', 'approve_and_deploy_commit'}


@pytest.mark.parametrize('bad', [ProviderTurn([{'type': 'shell_call'}], ''),
    ProviderTurn(call().output + call(call_id='second').output, '')])
def test_invalid_tool_outputs_fail_closed_without_fallback(tmp_path, bad):
    legacy = ScriptedModel(final())
    result, _, _, _ = execute(tmp_path, ScriptedProvider(bad), legacy=legacy)
    assert result.status == 'failed' and result.tool_calls == 0 and not legacy.requests


def test_route_metadata_survives_restart_without_reasoning(tmp_path):
    first = call()
    first.output.insert(0, {'type': 'reasoning', 'summary': [{'text': 'HIDDEN_REASONING'}],
                            'encrypted_content': 'OPAQUE_SECRET'})
    provider = ScriptedProvider(first, final('Done'))
    _, store, task, _ = execute(tmp_path, provider, goal='Hello PRIVATE_PROMPT')
    expected = routes(store, task['id'])
    store.close()
    reopened = TaskStore(tmp_path / 'chat.db')
    assert routes(reopened, task['id']) == expected
    assert not any(word in json.dumps(expected) for word in ('HIDDEN_REASONING', 'OPAQUE_SECRET', 'PRIVATE_PROMPT'))
    with reopened.database.transaction() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        persisted = '\n'.join(str(row[0]) for row in db.execute('SELECT content FROM messages'))
        persisted += '\n'.join(str(row[0]) for row in db.execute('SELECT event FROM activity'))
    assert 'HIDDEN_REASONING' not in persisted and 'OPAQUE_SECRET' not in persisted


@pytest.mark.parametrize('changes', [dict(provider='SECRET'), dict(reason='PRIVATE'),
    dict(iteration=65), dict(iteration=True), dict(status='SECRET')])
def test_routing_metadata_accepts_only_closed_fields(changes):
    store = TaskStore()
    task = store.create_task('Hello')
    args = dict(provider='local', reason='configured_provider', iteration=1, status='started') | changes
    with pytest.raises(ValueError):
        store.add_routing_activity(task['id'], **args)
    assert not routes(store, task['id'])


@pytest.mark.parametrize('enabled', [False, True])
def test_http_contract_and_conversation_continuity(tmp_path, monkeypatch, enabled):
    import main
    provider = ScriptedProvider(final('First'), final('Second'))
    legacy = ScriptedModel(final('First'), final('Second'))
    monkeypatch.setattr(main, 'orchestrator', Orchestrator(AgentRunner(model=legacy),
        brain_settings=BrainSettings(enabled, 'local'), providers=ProviderRegistry([provider])))
    with TestClient(main.create_app(tmp_path / 'api.db')) as client:
        first = client.post('/task', json={'goal': 'Hello', 'mode': 'agent'}).json()
        response = client.post('/task', json={'goal': 'Next', 'mode': 'agent',
                                              'conversation_id': first['conversation_id']})
        assert response.status_code == 200
        second = response.json()
        assert second['response'] == 'Second' and second['status'] == 'completed'
        assert second['agent'] == dict(iterations=1, tool_calls=0, stop_reason='final_response')
        assert client.get('/tasks/' + second['id']).json() == second
        requests = provider.requests if enabled else legacy.requests
        history = requests[1].input if enabled else requests[1]['input']
        assert history == [{'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'First'},
                           {'role': 'user', 'content': 'Next'}]


@pytest.mark.parametrize('goal', ['What did we decide about SQLite backups?', 'Run parser tests',
                                  'What did we talk about last time?', 'Hello'])
def test_memory_recall_and_experience_context_match_legacy(tmp_path, goal, monkeypatch):
    from datetime import datetime, timezone
    instant = datetime.now(timezone.utc)
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz else instant.replace(tzinfo=None)
    # Sequential routes must compare retrieval semantics at the same instant;
    # runtime inspection can make their wall-clock score decay differ.
    for module in ('core.recall', 'core.experience', 'core.experience_evaluation'):
        monkeypatch.setattr(module + '.datetime', FixedClock)
    from test_experience import capture
    store = TaskStore(tmp_path / 'context.db')
    earlier = store.create_task('Use SQLite backups daily')
    store.update_task(earlier['id'], response='Keep daily SQLite backups in the archive.')
    memory = store.memory.create_memory(scope='project', scope_id='jarvis', content='SQLite backups use daily rotation')
    capture(store)
    store.close()
    store = TaskStore(tmp_path / 'context.db')
    task = store.create_task(goal)
    legacy = ScriptedModel(final('Done'))
    runner = AgentRunner(model=legacy)
    old = runner.run(goal, task_id=task['id'], task_store=store)
    provider = ScriptedProvider(final('Done'))
    brain = JarvisBrain(runner, settings=BrainSettings(True, 'local'), providers=ProviderRegistry([provider]))
    new = brain.run(goal, task_id=task['id'], task_store=store)
    assert old == new
    assert len(legacy.requests) == len(provider.requests)
    if legacy.requests:
        # Brain exposes a semantic engineering action in addition to legacy tools.
        assert provider.requests[0].input == legacy.requests[0]['input']
        assert {t['name'] for t in provider.requests[0].tools} >= {t['name'] for t in legacy.requests[0]['tools']}
        if goal == 'Run parser tests':
            assert 'experience' in provider.requests[0].input[0]['content']
        if 'SQLite' in goal:
            assert 'durable_memory' in provider.requests[0].input[0]['content']
    assert store.memory.get_memory(memory['id']) == memory


def test_short_recall_returns_without_provider_call(tmp_path):
    provider = ScriptedProvider()
    result, store, task, _ = execute(tmp_path, provider, goal='What did we talk about last time?')
    assert result.stop_reason == 'recall_not_found' and not provider.requests
    assert not routes(store, task['id'])


def test_concurrent_tasks_have_independent_fallback_and_history(tmp_path):
    barrier = Barrier(2)
    class ConcurrentProvider:
        name = 'local'
        def complete(self, request):
            barrier.wait(timeout=10)
            if request.input[-1]['content'] == 'First':
                raise RuntimeError('failure')
            return final('Provider second')

    runner = AgentRunner(model=lambda **kw: final('Legacy ' + kw['input'][-1]['content']))
    brain = JarvisBrain(runner, settings=BrainSettings(True, 'local'),
                        providers=ProviderRegistry([ConcurrentProvider()]))
    store = TaskStore(tmp_path / 'concurrent.db')
    first, second = store.create_task('First'), store.create_task('Second')
    def run(task):
        return brain.run(task['goal'], task_id=task['id'], task_store=store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (first, second)))
    assert [r.response for r in results] == ['Legacy First', 'Provider second']
    assert not any(e['provider'] == 'legacy' for e in routes(store, second['id']))


def test_provider_registry_is_explicit_and_immutable():
    provider = ScriptedProvider(final())
    registry = ProviderRegistry([provider])
    assert registry.get('local') is provider and registry.get('claude') is None
    with pytest.raises(TypeError):
        registry.providers['claude'] = provider
    with pytest.raises(ValueError):
        ProviderRegistry([provider, provider])
    provider.name = 'arbitrary_module'
    with pytest.raises(ValueError):
        ProviderRegistry([provider])


@pytest.mark.parametrize('allow_tools', [False, True])
def test_openai_adapter_wraps_existing_transport(monkeypatch, allow_tools):
    calls = []
    turn = ProviderTurn([{'type': 'reasoning', 'encrypted_content': 'opaque', 'summary': []}], 'Answer')
    def transport(**kwargs):
        calls.append(kwargs)
        return turn
    monkeypatch.setattr(openai_client, 'request_agent_turn', transport)
    request = ProviderRequest([{'role': 'user', 'content': 'Hello'}], [], 'Instructions', allow_tools)
    assert OpenAIProvider().complete(request) is turn
    assert calls == [dict(input=request.input, tools=[], instructions='Instructions', allow_tools=allow_tools)]
    assert openai_client.AgentTurn is ProviderTurn


def test_default_enabled_route_uses_openai_adapter(tmp_path, monkeypatch):
    transport = ScriptedModel(final('OpenAI answer'))
    monkeypatch.setattr(openai_client, 'request_agent_turn', transport)
    store = TaskStore()
    task = store.create_task('Hello')
    brain = JarvisBrain(AgentRunner(model=lambda **kw: pytest.fail('Unexpected fallback')),
                        settings=BrainSettings(True))
    assert brain.run('Hello', task_id=task['id'], task_store=store).response == 'OpenAI answer'
    assert routes(store, task['id'])[0]['provider'] == 'openai'
