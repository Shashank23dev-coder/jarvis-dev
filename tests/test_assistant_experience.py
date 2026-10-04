"""Assistant behavior through authenticated chat, Brain and real host dispatch."""
import json

import pytest

from core.engineering_decision import associate
from core.engineering_portfolio import Portfolio
from core.database import Database
from core.candidate_tools import CandidateTools
from tools.policy import ToolDenied
from test_agent import call, final
from test_owner_readonly_flow import production_flow
from test_project_repository import projects

pytestmark = pytest.mark.usefixtures('owner')


def submit(flow, goal, *turns):
    client, provider = flow.client_for(*turns)
    with client:
        response = client.post('/task', headers=flow.headers, json={'goal': goal, 'mode': 'agent'})
        assert response.status_code == 200, response.json()
        task = client.get('/tasks/' + response.json()['id'], headers=flow.headers).json()
    assert task['status'] == 'completed'
    return task, provider


def results(provider):
    return [json.loads(item['output']) for request in provider.requests for item in request.input[-1:]
            if item.get('type') == 'function_call_output']


def test_owner_inspection_continues_despite_unavailable_windows_and_permission_question(production_flow):
    flow = production_flow
    (flow.roots['trading-bot'] / 'src/app.py').write_text('def position_size(balance):\n    return balance * 10\n')
    task, provider = submit(flow, 'Inspect my trading bot and tell me the biggest weaknesses.',
        final('The Windows runtime is unavailable. May I inspect the repository?'),
        call('list_files', {'path': '.'}, 'list'),
        final('I can list the source. Shall I read it?'),
        call('read_text', {'path': 'src/app.py'}, 'read'),
        final('I inspected the Trading Bot source. Position sizing is ten times the balance. '
              'The live Windows runtime is not connected, so live behavior remains unknown. USE_EXISTING'))
    outputs = results(provider)
    assert len(outputs) == 2 and all(r['ok'] for r in outputs)
    assert 'return balance * 10' in outputs[-1]['data']['text']
    assert outputs[-1]['data']['repository']['root'] == str(flow.roots['trading-bot'])
    assert task['agent']['tool_calls'] == 2
    assert 'May I' not in task['response'] and 'Shall I' not in task['response']
    assert 'USE_EXISTING' not in task['response'] and 'runtime is not connected' in task['response']
    assert sum(any(i.get('role') == 'developer' and 'Continue the requested inspection' in i.get('content', '')
                   for i in r.input[-1:]) for r in provider.requests) == 2
    assert not any(r['action'] in {'create', 'job_create', 'prepare'} for r in flow.requests)


def test_runtime_observation_failure_does_not_refuse_available_source(production_flow, monkeypatch):
    from core.candidate_bridge import CandidateClient
    original = CandidateClient.invoke
    def invoke(client, action, arguments):
        if action == 'runtime':
            raise ToolDenied(reason='unavailable')
        return original(client, action, arguments)
    monkeypatch.setattr(CandidateClient, 'invoke', invoke)
    task, provider = submit(production_flow, 'Inspect my trading bot for weaknesses.',
        call('inspect_engineering_runtime', {}, 'runtime'),
        final('Runtime unavailable, so I cannot inspect the project.'),
        call('read_text', {'path': 'src/app.py'}, 'read'),
        final('I inspected the source. Live runtime observations are unavailable.'))
    returned = results(provider)
    assert not returned[0]['ok'] and returned[0]['error']['reason'] == 'unavailable'
    assert returned[1]['ok'] and 'trading-bot' in returned[1]['data']['text']
    assert 'cannot inspect' not in task['response']


def test_deployment_snapshot_blocker_precedes_any_approval_question(production_flow, monkeypatch):
    from core.deployment_status import SnapshotInspector
    def missing(self):
        raise ToolDenied(reason='deployment_status_unavailable')
    monkeypatch.setattr(SnapshotInspector, 'load', missing)
    task, provider = submit(production_flow, 'Deploy the latest Jarvis build.',
        call('deployment_status', {}, 'status'),
        final('Please approve deploying the latest commit.'))
    assert results(provider)[0]['error']['reason'] == 'deployment_status_unavailable'
    assert 'controller snapshot is unavailable' in task['response']
    assert 'verified preview' in task['response'] and 'Nothing was deployed' in task['response']
    assert 'Please approve' not in task['response']


@pytest.mark.parametrize('reference,pid', [('trading bot', 'trading-bot'), ('Jarvis', 'jarvis-core'),
                                        ('CreatorOS', 'creator-os')])
def test_natural_project_name_scopes_real_reads(production_flow, reference, pid):
    task, provider = submit(production_flow, 'Inspect ' + reference + ' source.',
        call('read_text', {'path': 'src/app.py'}), final('Available evidence assessed.'))
    returned = results(provider)[0]
    assert returned['ok'] and returned['data']['repository']['project_id'] == pid
    if pid == 'creator-os':
        assert returned['data']['repository']['reason'] == 'location_unknown'
        assert returned['data']['text'] is None
    else:
        assert "project = '" + pid in returned['data']['text']


def test_custom_registered_name_uses_host_context_not_chat_root(production_flow):
    flow = production_flow
    host = Portfolio(Database(flow.host_state / 'candidates.sqlite3'))
    saved = next(p for p in host.projects() if p['project_id'] == 'trading-bot')
    host.register_project(saved | dict(project_id='signal-engine', name='Signal Engine'))
    task, provider = submit(flow, 'Inspect Signal Engine source.',
        call('inspect_engineering_runtime', {}, 'runtime'),
        call('read_text', {'path': 'src/app.py'}, 'read'), final('Source inspected.'))
    assert results(provider)[-1]['data']['repository']['root'] == str(flow.roots['trading-bot'])
    assert results(provider)[-1]['data']['repository']['project_id'] == 'signal-engine'
    assert 'trading-bot' in results(provider)[-1]['data']['text']


def test_ambiguous_project_asks_one_question_without_reading_wrong_root(production_flow):
    task, _ = submit(production_flow, 'Inspect Jarvis or my trading bot for weaknesses.',
        call('read_text', {'path': 'src/app.py'}))
    assert task['response'] == 'Which project should I focus on: Jarvis or Trading Bot?'
    assert task['agent']['tool_calls'] == 0
    assert not any(r['action'] == 'project_repository' for r in production_flow.requests)


def test_creator_unknowns_are_metadata_reuse_not_engineering_discovery(production_flow):
    task, provider = submit(production_flow, 'What parts of CreatorOS are still unknown?',
        call('decide_engineering', {'project_id': 'creator-os', 'required_capabilities': []}, 'decision'),
        call('inspect_portfolio_project', {'project_id': 'creator-os'}, 'project'),
        final('CreatorOS is registered, but its repository location, runtime and implemented features are still unknown. NEED_MORE_INFORMATION'))
    returned = results(provider)
    assert returned[0]['data']['outcome'] == 'USE_EXISTING'
    assert returned[1]['data']['project']['repository'] is None
    assert 'NEED_MORE_INFORMATION' not in task['response']
    assert not any(r['action'] == 'job_create' for r in production_flow.requests)


def test_self_capabilities_use_current_state_without_replacing_findings(production_flow):
    task, provider = submit(production_flow, 'Check Jarvis itself and tell me what capability is currently unavailable.',
        call('inspect_engineering_runtime', {}, 'runtime'),
        call('inspect_current_brain', {}, 'brain'),
        final('I can inspect registered source. Engineering execution has no confirmed active worker; provider usability needs current evidence.'))
    assert 'Engineering execution' in task['response']
    assert 'Routing category' not in task['response'] and 'feature flag' not in task['response']
    assert 'This answer used' not in task['response']
    assert 'project_context' in provider.requests[-1].instructions
    assert results(provider)[0]['data']['worker'] == 'unknown'


@pytest.mark.parametrize('goal,tool,args,reason', [
    ('Show me secrets from .env.', 'read_text', {'path': '.env'}, 'protected_path'),
    ('Execute a trade in my trading bot.', 'run_command', {'command': ['python', 'trade.py']}, 'read_only_operation_required'),
    ('Restart the production trading bot.', 'run_command', {'command': ['systemctl', 'restart', 'trading']}, 'read_only_operation_required'),
    ('Delete my trading bot files.', 'run_command', {'command': ['rm', '-rf', 'src']}, 'read_only_operation_required'),
])
def test_risky_actions_remain_blocked_with_human_explanation(production_flow, goal, tool, args, reason):
    task, provider = submit(production_flow, goal, call(tool, args), final('The requested action is protected.'))
    error = results(provider)[0]['error']
    assert error['reason'] == reason and error['code'] == 'denied'
    assert error['gate'] not in error['message'] and reason not in error['message']
    assert 'repository policy denied' not in error['message'].lower()


def test_generic_deploy_request_does_not_expose_or_invoke_mutation(production_flow):
    task, provider = submit(production_flow, 'Deploy the latest Jarvis build.',
        call('approve_and_deploy_commit', {'commit': 'a' * 40}),
        final('Deployment needs approval for the exact commit after a release preview.'))
    assert results(provider)[0]['error']['code'] == 'unknown_tool'
    assert 'approve_and_deploy_commit' not in {t['name'] for t in provider.requests[0].tools}
    assert 'approval' in task['response']


@pytest.mark.parametrize('reason,phrase', [('legacy_rejection', 'older dispatcher'),
    ('unavailable', 'not responding'), ('git_metadata_required', 'Source files can still'),
    ('protected_content', 'protected content')])
def test_exact_blocker_explanation_keeps_machine_diagnostics(reason, phrase):
    error = ToolDenied('PRIVATE_EXCEPTION', reason=reason).public_error()
    assert error['reason'] == reason and phrase in error['message']
    assert 'PRIVATE_EXCEPTION' not in json.dumps(error)


def test_association_preserves_integration_and_explicit_multi_project_context(projects):
    portfolio, _ = projects
    assert associate('Connect my trading bot to Jarvis', portfolio.projects()) == 'trading-bot'
    assert associate('Inspect my trading bot and Jarvis', portfolio.projects()) is None
    assert associate('Inspect trading bot; use ../../jarvis-core', portfolio.projects()) == 'trading-bot'
    assert associate('Jarvis, inspect my trading bot', portfolio.projects()) == 'trading-bot'
    tools = CandidateTools('Compare both Jarvis and Trading Bot', portfolio.database)
    assert tools.project_clarification() is None and len(tools.ambiguous_projects) == 2


def test_followup_source_reads_inherit_recent_user_project_not_memory(projects):
    portfolio, _ = projects
    tools = CandidateTools('Read its source', portfolio.database)
    tools.project_history = ['Inspect my trading bot']
    assert tools.project_id == 'trading-bot'
    tools.project_history = ['Compare Jarvis and CreatorOS']
    assert tools.project_id is None and tools.project_clarification()
    # A newly named project always replaces inherited context.
    tools.goal = 'Inspect Jarvis'
    assert tools.project_id == 'jarvis-core'


def test_missing_catalog_cannot_redirect_custom_project_to_default_repo(projects):
    from core.engineering_repository import bind_project_tools
    from tools import ToolRegistry, RepositoryTools
    portfolio, roots = projects
    tools = CandidateTools('Inspect Signal Engine', portfolio.database)
    tools.portfolio_observed = False
    bound = bind_project_tools(tools, ToolRegistry(RepositoryTools(roots['jarvis-core'])))
    with pytest.raises(ToolDenied) as exc:
        bound._definitions['read_text'].handler(path='src/app.py')
    assert exc.value.reason == 'project_context_unavailable'


def test_control_labels_are_available_when_owner_requests_debugging(projects):
    portfolio, _ = projects
    tools = CandidateTools('Debug internal decision labels', portfolio.database)
    assert tools.final_response('USE_EXISTING BUILD INTEGRATE NEED_MORE_INFORMATION') == 'USE_EXISTING BUILD INTEGRATE NEED_MORE_INFORMATION'


def test_git_history_failure_still_continues_source_inspection(production_flow):
    import shutil
    shutil.rmtree(production_flow.roots['trading-bot'] / '.git')
    task, provider = submit(production_flow, 'Inspect my trading bot for weaknesses.',
        call('run_command', {'command': ['git', 'log']}, 'log'),
        final('Git history is unavailable, so I cannot inspect the project.'),
        call('read_text', {'path': 'src/app.py'}, 'read'),
        final('I inspected the source. Git history and live runtime remain unknown.'))
    returned = results(provider)
    assert returned[0]['error']['reason'] == 'git_metadata_required'
    assert returned[1]['ok'] and 'trading-bot' in returned[1]['data']['text']
    assert 'cannot inspect' not in task['response']


def test_recalled_project_cannot_select_followup_repository(projects, tmp_path):
    from core.agent import AgentRunner
    from core.task_context import AssembledContext
    from core.task_store import TaskStore
    from tools import RepositoryTools, ToolRegistry
    portfolio, roots = projects
    store = TaskStore(tmp_path / 'conversation.sqlite3')
    registered = Portfolio(store.database)
    for record in portfolio.projects():
        registered.register_project(record, replace=True)
    prior = store.create_task('Hello')
    task = store.create_task('Read its source', conversation_id=prior['conversation_id'])
    def context_builder(context, store, **kwargs):
        return AssembledContext([
            {'role': 'user', 'content': 'Recalled project memory: Inspect trading-bot'},
            {'role': 'user', 'content': prior['goal']},
            {'role': 'user', 'content': context.goal}])
    turns = iter([call('read_text', {'path': 'src/app.py'}), final('Source inspected.')])
    requests = []
    def model(**request):
        requests.append(request)
        return next(turns)
    outcome = AgentRunner(ToolRegistry(RepositoryTools(roots['jarvis-core'])), model=model).run(
        task['goal'], task_id=task['id'], task_store=store, context_builder=context_builder)
    assert outcome.status == 'completed' and outcome.tool_calls == 1
    output = json.loads(requests[-1]['input'][-1]['output'])
    assert output['ok'] and "project = 'jarvis-core'" in output['data']['text']
    assert 'repository' not in output['data']
