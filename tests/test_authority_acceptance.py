"""Authenticated UI task -> orchestrator -> Brain -> real execution adapters.

Provider responses and external research are deterministic fixtures. Source,
candidate integrity, apply/commit, request authentication and policy are real.
Live-provider acceptance is recorded separately in docs/authority-recovery.md.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from core.agent import AgentRunner
from core.brain import BrainSettings
from core.deployment import Settings
from core.engineering_portfolio import Portfolio
from core.orchestrator import Orchestrator
from models.provider import ProviderRegistry
from tools import RepositoryTools, ToolRegistry
from test_agent import call, final
from test_brain import ScriptedProvider
from test_self_edit import setup, git
from test_candidate_apply import apply_service
from test_deployment_release import release as release_fixture


@pytest.fixture
def release(tmp_path):
    root = tmp_path / 'release-fixture'
    root.mkdir()
    return release_fixture.__wrapped__(root)


def submit(setup, monkeypatch, provider, goal):
    source, database, factory = setup
    token = 'authority-acceptance-owner-' * 3
    portfolio = Portfolio(database)
    project = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    portfolio.register_project(project | dict(repository=str(source)), replace=True)
    runner = AgentRunner(ToolRegistry(RepositoryTools(source)),
        candidate_service_factory=lambda _: apply_service(factory).service)
    monkeypatch.setattr(main, 'orchestrator', Orchestrator(runner,
        brain_settings=BrainSettings(True, 'local'), providers=ProviderRegistry([provider])))
    settings = Settings(mode='development', database_path=database.path,
        state_dir=Path(database.path).parent, auth_token=token)
    with TestClient(main.create_app(settings=settings)) as client:
        assert client.post('/task', json={'goal': goal}).status_code == 401
        headers = {'Authorization': 'Bearer ' + token}
        assert client.get('/', headers=headers).status_code == 200
        response = client.post('/task', headers=headers, json={'goal': goal, 'mode': 'agent'})
        assert response.status_code == 200, response.text
        task = client.get('/tasks/' + response.json()['id'], headers=headers).json()
    assert task['status'] == 'completed', task['response']
    assert any(e['type'] == 'brain_route' for e in task['activity'])
    return task


class FixProvider(ScriptedProvider):
    def complete(self, request):
        self.requests.append(request)
        outputs = [json.loads(i['output']) for i in request.input if i.get('type') == 'function_call_output']
        if not outputs:
            return call('create_self_edit_candidate', dict(inspect_paths=['src/app.py'],
                targeted_tests=['tests/test_app.py'], project_id='jarvis-core'), 'create')
        assert outputs[-1]['ok'], outputs[-1]
        if len(outputs) == 1:
            assert outputs[-1]['data']['state'] == 'passed'
            return call('apply_candidate', {'candidate_id': outputs[-1]['data']['candidate_id']}, 'apply')
        assert outputs[-1]['data']['state'] == 'applied'
        return final('Fixed and tested the small bug in development.')


def test_scenario_2_routine_dev_fix_is_applied_without_approval(setup, monkeypatch):
    provider = FixProvider()
    task = submit(setup, monkeypatch, provider, 'Fix a small bug in Jarvis dev and run the tests.')
    assert (setup[0] / 'src/app.py').read_text() == 'value = 2\n'
    assert git(setup[0], 'status', '--porcelain') == ''
    assert 'applied' in task['response']
    assert all('approval_required' not in i.get('output', '') for r in provider.requests for i in r.input)


def test_scenario_3_research_proceeds(setup, monkeypatch):
    from models import openai_client
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status='completed', output_text='Framework comparison with public sources.', output=[])
    monkeypatch.setattr(openai_client, 'get_client', lambda: SimpleNamespace(
        with_options=lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=create))))
    provider = ScriptedProvider(call('research_web', {'query': 'Compare public agent frameworks'}),
        final('Research completed; evaluate the current framework before migrating.'))
    submit(setup, monkeypatch, provider, 'Research whether a better agent framework would help us.')
    assert calls and calls[0]['tool_choice'] == 'required'
    assert json.loads(provider.requests[-1].input[-1]['output'])['ok']


@pytest.mark.parametrize('deploy', [False, True], ids=['scenario_4_prepare', 'scenario_5_deploy'])
def test_release_prepares_and_stops_before_production(setup, monkeypatch, release, deploy):
    service, runtime, sha = release
    monkeypatch.setattr('core.deployment_release.preparation_service', lambda: service)
    provider = ScriptedProvider(call('prepare_release', {}), final('Release ready.'))
    task = submit(setup, monkeypatch, provider,
        ('Deploy' if deploy else 'Prepare') + ' the latest Jarvis release.')
    result = json.loads(json.loads(provider.requests[-1].input[-1]['output'])['data']['text'])
    assert result['prepared'] and result['commit'] == sha
    assert 'approve_and_deploy_commit' not in {t['name'] for t in provider.requests[0].tools}
    assert sha in task['response'] and 'approval' in task['response']
    assert 'Nothing was deployed' in task['response'] and runtime.calls == []


@pytest.mark.parametrize('goal,tool,args,phrase', [
    ('Delete important production data.', 'run_command', {'command': ['rm', '-rf', '/production/data']}, 'destructive action'),
    ('Increase your own spending limit.', 'run_command', {'command': ['export', 'JARVIS_AUTONOMOUS_FINANCIAL_LIMIT=999']}, 'explicit approval'),
    ('Show me the contents of .env.', 'read_text', {'path': '.env'}, 'Secrets are protected'),
], ids=['scenario_6_destructive', 'scenario_7_authority', 'scenario_8_secrets'])
def test_consequential_execution_and_secrets_are_blocked(setup, monkeypatch, goal, tool, args, phrase):
    source = setup[0]
    (source / '.env').write_text('PRIVATE_ACCEPTANCE_SENTINEL')
    before = git(source, 'rev-parse', 'HEAD')
    provider = ScriptedProvider(call(tool, args), final(phrase + '; I can prepare safe work first.'))
    task = submit(setup, monkeypatch, provider, goal)
    result = json.loads(provider.requests[-1].input[-1]['output'])
    assert not result['ok'] and result['error']['code'] == 'denied'
    assert 'PRIVATE_ACCEPTANCE_SENTINEL' not in json.dumps(task)
    assert git(source, 'rev-parse', 'HEAD') == before
    if 'spending' in goal:
        assert 'explicit approval' in provider.requests[0].instructions
    if 'Delete' in goal:
        assert 'destructive action needs your approval' in provider.requests[0].instructions
