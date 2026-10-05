"""Default-act authority, with consequences enforced by real execution adapters."""
import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.engineering_authority import assess_action, authorize, EscalationRequired, current_owner
from core.candidate_tools import CandidateTools
from core.engineering_jobs import EngineeringJobs
from core.candidate_broker import Broker
from tools import ToolDenied, ToolRegistry
from tools.candidate_edit import CandidatePolicy
from test_self_edit import setup, start, git
from test_candidate_apply import apply_service, confirmation
from test_candidate_bridge import request
from test_project_repository import projects

pytestmark = pytest.mark.usefixtures('owner')


@pytest.mark.parametrize('action', ['inspect', 'research', 'develop', 'test', 'benchmark', 'review', 'dev_service', 'metadata', 'release_prepare'])
def test_normal_actions_need_only_owner(action):
    assert authorize(action).allowed
    token = current_owner.set(None)
    try:
        with pytest.raises(ToolDenied):
            authorize(action)
    finally:
        current_owner.reset(token)


@pytest.mark.parametrize('action,reason', [('production_deploy', 'major_change'), ('architecture_promote', 'major_change'),
    ('security_change', 'major_change'), ('delete_data', 'destructive'), ('destroy_infrastructure', 'destructive'),
    ('authority_change', 'authority_expansion')])
def test_consequences_only_gate_execution(action, reason):
    assert authorize(action, phase='prepare').allowed
    with pytest.raises(EscalationRequired) as caught:
        authorize(action)
    assert reason in caught.value.decision.escalation
    assert authorize(action, approved=True).approval_required


@pytest.mark.parametrize('limit,amount,currency,allowed', [
    (None, '1', 'USD', False), ('10', '9.99', 'USD', True), ('10', '10', 'USD', True),
    ('10', '10.01', 'USD', False), ('10', '1', 'EUR', False), ('NaN', '1', 'USD', False),
    ('-1', '0', 'USD', False), ('10', None, 'USD', False), ('10', '-1', 'USD', False),
    ('10', 'Infinity', 'USD', False), ('0', '0', 'USD', True)])
def test_financial_limit_never_invented(limit, amount, currency, allowed):
    config = {'JARVIS_AUTONOMOUS_FINANCIAL_CURRENCY': 'USD'}
    if limit is not None:
        config['JARVIS_AUTONOMOUS_FINANCIAL_LIMIT'] = limit
    decision = assess_action('spend', amount=amount, currency=currency, financial_config=config)
    assert decision.allowed is allowed
    assert assess_action('spend', phase='prepare', financial_config=config).allowed


@pytest.mark.parametrize('path', ['core/engineering_authority.py', 'tools/policy.py', 'core/candidate_broker.py',
    'core/candidate_apply.py', 'tools/sandbox.py', 'core/agent.py', 'core/brain.py', 'models/__init__.py',
    'core/new_policy.py', 'tools/new_permissions.py', 'main.py'])
def test_authority_source_can_be_prepared_but_not_promoted(path):
    CandidatePolicy().edit_path(path)
    decision = assess_action('candidate_apply', changed_paths=[path])
    assert decision.approval_required and 'authority_expansion' in decision.escalation


@pytest.mark.parametrize('path', ['.env', 'docs/credentials.txt', 'docs/passwords.txt', 'docs/token.json', '../src/app.py', '/etc/passwd', '.git/config'])
def test_hard_boundaries_remain_denials(path):
    with pytest.raises(ToolDenied):
        CandidatePolicy().edit_path(path)


def test_routine_candidate_applies_without_phrase_with_real_integrity_checks(setup):
    source, _, factory = setup
    candidate = start(factory())
    preview = apply_service(factory).preview(candidate['id'])
    assert preview['eligible'] and preview['required_confirmation'] is None
    result = apply_service(factory).apply(candidate['id'])
    assert result['apply']['status'] == 'applied'
    assert (source / 'src/app.py').read_text() == 'value = 2\n'
    assert git(source, 'status', '--porcelain') == ''


def test_authority_candidate_requires_exact_owner_approval(setup):
    source, _, factory = setup
    service = factory()
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    record['goal'] = 'Increase your own authority'
    service.store.save(record)
    for text in ('', 'looks good', confirmation(str(uuid4()))):
        with pytest.raises(EscalationRequired):
            apply_service(factory).apply(candidate['id'], text)
    assert (source / 'src/app.py').read_text() == 'value = 1\n'
    assert apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))['apply']['status'] == 'applied'


def test_direct_job_creation_without_prepare_or_capability_approval(setup):
    _, database, factory = setup
    tools = CandidateTools('Fix a small bug in Jarvis dev and run the tests.', database,
        lambda db: factory(), source_context=dict(task_id=str(uuid4()), conversation_id=str(uuid4())))
    result = tools.engineer(['src/app.py'], ['tests/test_app.py'], 'Wrong value', 'Correct the value')
    job = EngineeringJobs(database).get(result.jobs[0]['id'])
    assert job['status'] == 'queued' and job['project_id'] == 'jarvis-core'
    assert tools.workspace is None
    assert 'create_engineering_job' in {t['name'] for t in tools.bind(ToolRegistry()).model_tools()}


def test_broker_workspace_is_discovery_not_permission(setup):
    source, _, factory = setup
    raw = request('workspace', 'Fix a small bug', {})
    result = Broker(factory(), source=source).execute(raw)
    assert 'src/app.py' in result['editable_paths']


def test_unknown_project_cannot_be_redirected_to_jarvis(setup):
    _, database, factory = setup
    tools = CandidateTools('Fix missing project', database, lambda db: factory())
    with pytest.raises(ToolDenied):
        tools.create(['src/app.py'], ['tests/test_app.py'], project_id='unregistered')
    with pytest.raises(ToolDenied):
        tools._service('creator-os')


def test_external_candidate_keeps_broker_source_and_projects_separate(setup, tmp_path):
    from core.engineering_portfolio import Portfolio
    source, database, factory = setup
    external = tmp_path / 'external'
    # A clean distinct registered Git fixture; never select a root from a request.
    import subprocess
    subprocess.run(['git', 'clone', '--local', str(source), str(external)], check=True,
        capture_output=True)
    portfolio = Portfolio(database)
    project = next(p for p in portfolio.projects() if p['project_id'] == 'trading-bot')
    portfolio.register_project(project | dict(repository=str(external)), replace=True)
    broker = Broker(factory(), source=source)
    created = broker.execute(request('create', 'Fix my trading bot', dict(
        inspect_paths=['src/app.py'], targeted_tests=['tests/test_app.py'], project_id='trading-bot')))
    assert created['state'] == 'passed'
    assert broker.service.source == source
    again = broker.execute(request('inspect', 'Inspect candidate', dict(candidate_id=created['id'])))
    assert again['id'] == created['id'] and again['state'] == 'passed'
    assert (source / 'src/app.py').read_text() == 'value = 1\n'
    assert (external / 'src/app.py').read_text() == 'value = 1\n'


def test_legacy_portfolio_upgrades_only_obsolete_authority_wording(setup):
    from core.engineering_portfolio import Portfolio
    portfolio = Portfolio(setup[1])
    record = next(p for p in portfolio.projects() if p['project_id'] == 'trading-bot')
    original = record | dict(repository=str(setup[0]), limitations=[
        'Bounded read-only repository inspection; no file changes, credentials, runtime or service operations.',
        'Operator custom limitation'])
    portfolio.register_project(original, replace=True)
    upgraded = next(p for p in Portfolio(setup[1]).projects() if p['project_id'] == 'trading-bot')
    assert upgraded['repository'] == original['repository'] and upgraded['read_only'] == original['read_only']
    assert upgraded['limitations'][-1] == 'Operator custom limitation'
    assert 'isolated development' in upgraded['limitations'][0]


def test_registered_project_tests_use_its_sandbox(projects, monkeypatch):
    from tools import RepositoryTools, CommandResult
    from core.engineering_repository import inspect_repository
    portfolio, roots = projects
    observed = []
    def run(repository, paths):
        observed.append((repository.root, paths))
        return CommandResult(('pytest',), 0, '1 passed')
    monkeypatch.setattr(RepositoryTools, 'run_pytest', run)
    result = inspect_repository(portfolio.database, project_id='trading-bot',
        operation='run_pytest', paths=['tests'])
    assert result['process']['returncode'] == 0
    assert observed == [(roots['trading-bot'], ['tests'])]


def test_research_adapter_uses_bounded_provider_search(monkeypatch):
    from types import SimpleNamespace
    from core.engineering_tools import research_web
    from models import openai_client
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status='completed', output_text='Framework comparison', output=[])
    client = SimpleNamespace(with_options=lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai_client, 'get_client', lambda: client)
    assert 'Framework comparison' in research_web('Compare public agent frameworks')
    assert calls[0]['tools'] == [{'type': 'web_search'}] and calls[0]['tool_choice'] == 'required'
    assert calls[0]['store'] is False
