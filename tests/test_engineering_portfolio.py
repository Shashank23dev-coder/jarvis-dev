"""Portfolio resources, conservative preflight and evidence precedence."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.database import Database
from core.engineering_decision import Decisions, associate
from core.engineering_portfolio import Portfolio
from tools.policy import ToolDenied

NOW = '2026-10-04T10:00:00'


@pytest.fixture
def portfolio(tmp_path):
    return Portfolio(Database(tmp_path / 'state.sqlite3'), clock=lambda: NOW)


def test_seed_projects_and_capabilities_persist(portfolio):
    value = portfolio.snapshot()
    assert {p['project_id'] for p in value['projects']} == {'jarvis-core', 'trading-bot', 'creator-os'}
    assert len(value['capabilities']) == 14
    assert all(p['executive'] == 'Jarvis' for p in value['projects'])
    portfolio.database.close()
    assert Portfolio(Database(portfolio.database.path), clock=lambda: NOW).snapshot() == value


def test_registration_is_host_owned_and_non_destructive(portfolio):
    value = portfolio.projects()[0] | dict(project_id='other', name='Other')
    portfolio.register_project(value)
    portfolio.register_project(value | dict(name='Changed'))
    assert next(p for p in portfolio.projects() if p['project_id'] == 'other')['name'] == 'Other'
    portfolio.register_project(value | dict(name='Changed'), replace=True)
    assert next(p for p in portfolio.projects() if p['project_id'] == 'other')['name'] == 'Changed'
    with pytest.raises(ValueError):
        portfolio.register_project(value | dict(executive='Claude'))
    cap = portfolio.snapshot()['capabilities'][0] | dict(capability_id='other-capability')
    portfolio.register_capability(cap)
    assert any(c['capability_id'] == 'other-capability' for c in portfolio.snapshot()['capabilities'])


def test_unknown_creator_and_read_only_trading_baseline(portfolio):
    projects = {p['project_id']: p for p in portfolio.projects()}
    creator = projects['creator-os']
    assert creator['repository'] is None and creator['status'] == 'needs_discovery'
    trading = projects['trading-bot']
    assert trading['read_only'] and trading['current_commit'] is None
    assert trading['baseline_commit'] == 'e03c307c64fdaaf267ecb7be3898325af555f966'
    assert trading['repository'] == '/srv/jarvis/projects/trading-bot'
    assert portfolio.inspect_project('creator-os') is None


def test_unproven_and_expired_capability(portfolio):
    cap = portfolio.snapshot()['capabilities'][0]
    assert cap['current_usability'] == 'unproven' and cap['configured'] is None
    portfolio.register_capability(cap | dict(current_usability='available', configured=True), replace=True)
    portfolio.clock = lambda: '2026-10-04T10:10:00'
    assert next(c for c in portfolio.snapshot()['capabilities'] if c['capability_id'] == cap['capability_id'])['current_usability'] == 'unknown'


def test_runtime_wins_and_surfaces_contradiction(portfolio):
    cap = next(c for c in portfolio.snapshot()['capabilities'] if c['capability_id'] == 'claude-code-engineering')
    portfolio.register_capability(cap | dict(current_usability='available', configured=True), replace=True)
    from core.engineering_providers import ProviderState, Observation
    provider = ProviderState(provider_id='claude_code', provider_type='cli', capabilities=['implementing', 'reviewing'],
        task_types=['code'], enabled=True, configured=True, state='not_authenticated', current_usability='not_authenticated',
        observation=Observation(state='not_authenticated', observed_at=NOW, source='claude_auth_status', scope='local_authentication'))
    snapshot = portfolio.snapshot(provider_registry={'providers': [provider.model_dump()]})
    live = next(c for c in snapshot['capabilities'] if c['capability_id'] == cap['capability_id'])
    assert live['current_usability'] == 'not_authenticated'
    assert snapshot['contradictions'] == [dict(resource=cap['capability_id'], field='current_usability', registry='available', runtime='not_authenticated')]
    assert 'not_authenticated' in json.dumps(snapshot)


def test_project_git_probe_only_identity_no_runtime_or_mutation(portfolio, monkeypatch, tmp_path):
    root = tmp_path / 'bot'
    root.mkdir()
    from core.engineering_portfolio import PROJECT_ROOTS
    monkeypatch.setattr('core.engineering_portfolio.PROJECT_ROOTS', dict(PROJECT_ROOTS) | {'trading-bot': root})
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=str(root).encode() + b'\n' + b'1' * 40 + b'\n')
    monkeypatch.setattr('core.engineering_portfolio.subprocess.run', run)
    value = portfolio.inspect_project('trading-bot')
    assert value['repository_state'] == 'present' and value['read_only']
    assert calls[0][0] == ['/usr/bin/git', '--no-optional-locks', '-C', str(root), 'rev-parse', '--show-toplevel', 'HEAD']
    assert calls[0][1]['env'] == {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    assert any(c['resource'] == 'trading-bot' for c in portfolio.snapshot()['contradictions'])
    assert 'Linux compatibility and runtime are unproven' in value['runtime_notes'][0]


def test_probe_failure_no_raw_errors(portfolio, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('PRIVATE_AUTH_MATERIAL')
    monkeypatch.setattr('core.engineering_portfolio.subprocess.run', fail)
    assert portfolio.inspect_project('trading-bot')['repository_state'] == 'unavailable'
    assert 'PRIVATE_AUTH_MATERIAL' not in json.dumps(portfolio.snapshot())


@pytest.mark.parametrize('goal,project,outcome', [
    ('Add Claude support', None, 'USE_EXISTING'),
    ('Add Codex support', 'jarvis-core', 'USE_EXISTING'),
    ('Connect my trading bot to Jarvis', None, 'INTEGRATE'),
    ('Implement a new Jarvis status endpoint', None, 'BUILD'),
    ('Help me with something', None, 'NEED_MORE_INFORMATION'),
    ('Connect this to something', None, 'NEED_MORE_INFORMATION'),
    ('Build CreatorOS integration', None, 'NEED_MORE_INFORMATION'),
    ('Fix my trading bot', None, 'NEED_MORE_INFORMATION'),
])
def test_preflight_outcomes_and_durable_rationale(portfolio, goal, project, outcome):
    decisions = Decisions(portfolio.database)
    value = decisions.evaluate(goal, portfolio.snapshot(), project_id=project)
    assert value['outcome'] == outcome and value['rationale']
    assert decisions.recent()[0] == value
    assert goal not in json.dumps(value)
    if 'trading bot' in goal:
        assert value['project_id'] == 'trading-bot'
    if goal == 'Add Claude support':
        assert value['capability_states']['claude-code-engineering'] == 'unproven'


def test_existing_capability_needs_fresh_usable_scoped_evidence(portfolio):
    decisions = Decisions(portfolio.database)
    kwargs = dict(project_id='jarvis-core', required_capabilities=['repo-files'])
    assert decisions.evaluate('Inspect repository', portfolio.snapshot(), **kwargs)['outcome'] == 'NEED_MORE_INFORMATION'
    snapshot = portfolio.snapshot(observations={'repo-files': dict(current_usability='available', configured=True,
        evidence=portfolio.evidence('bound_tools', 'current task configured repository').model_dump())})
    assert decisions.evaluate('Inspect repository', snapshot, **kwargs)['outcome'] == 'USE_EXISTING'
    assert decisions.evaluate('Inspect repository', snapshot, project_id='trading-bot', required_capabilities=['repo-files'])['outcome'] == 'NEED_MORE_INFORMATION'


def test_missing_capability_build_requires_specific_request(portfolio):
    decisions = Decisions(portfolio.database)
    args = dict(project_id='jarvis-core', required_capabilities=['missing-adapter'])
    assert decisions.evaluate('Build a new adapter', portfolio.snapshot(), **args)['outcome'] == 'BUILD'
    assert decisions.evaluate('Use that adapter', portfolio.snapshot(), **args)['outcome'] == 'NEED_MORE_INFORMATION'
    assert decisions.evaluate('Connect my trading bot', portfolio.snapshot(), **args)['outcome'] == 'NEED_MORE_INFORMATION'


def test_unknown_project_and_no_authority_from_association(portfolio):
    value = Decisions(portfolio.database).evaluate('Fix a bug', portfolio.snapshot(), project_id='made-up')
    assert value['outcome'] == 'NEED_MORE_INFORMATION' and value['project_id'] is None
    assert associate('Jarvis and CreatorOS and trading bot', portfolio.projects()) is None
    assert associate('Unrelated request', portfolio.projects()) is None


def test_decisions_and_snapshots_never_touch_trading_files(portfolio, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('Normal portfolio/preflight must not invoke subprocesses or trading runtime')
    monkeypatch.setattr('core.engineering_portfolio.subprocess.run', forbidden)
    bot = tmp_path / 'trading-bot'
    bot.mkdir()
    (bot / 'baseline.py').write_text('untouched\n')
    before = {p.name: p.read_bytes() for p in bot.iterdir()}
    value = next(p for p in portfolio.projects() if p['project_id'] == 'trading-bot')
    portfolio.register_project(value | dict(repository=str(bot)), replace=True)
    for goal in ['Connect my trading bot to Jarvis', 'Fix my trading bot', 'What projects exist?']:
        Decisions(portfolio.database).evaluate(goal, portfolio.snapshot())
    assert {p.name: p.read_bytes() for p in bot.iterdir()} == before


def test_credential_request_rejected_before_persistence(portfolio):
    decisions = Decisions(portfolio.database)
    with pytest.raises(ToolDenied):
        decisions.evaluate("Use api_key='synthetic-provider-fixture'", portfolio.snapshot())
    assert decisions.recent() == []
