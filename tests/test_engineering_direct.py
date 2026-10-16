"""Engineering jobs on the direct-development path: routing, stages, outcomes, projections, budget."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core import direct_dev
from core.candidate_apply import CandidateApply
from core.candidate_bridge import project
from core.engineering_jobs import LEASE_SECONDS, EngineeringJobs, project_job
from core.engineering_portfolio import Portfolio
from core.engineering_worker import EngineeringWorker
from tools import CommandResult
from tools.policy import ToolDenied
from test_self_edit import setup, tree_bytes
from test_engineering import model, run_checks
from test_engineering_jobs import enqueue
from test_candidate_apply import integration_target

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = 'f95f459'


class Agent:
    """Subscription CLI stand-in: edits the real worktree, reviews the real diff."""
    provider_type = 'cli'

    def __init__(self, name, *, state='available', verdict='passed', timeouts=None):
        self.name, self.state, self.verdict, self.calls = name, state, verdict, []
        self.timeouts = timeouts if timeouts is not None else []
        self.failures = self.run_id = None

    def executable(self):
        return '/usr/bin/true'

    def probe(self, timeout):
        return self.state

    def complete(self, payload, instructions, timeout):
        self.calls.append(payload['action'])
        self.timeouts.append(timeout)
        if payload['action'] == 'implement':
            path = Path(payload['worktree']) / 'src/app.py'
            # Each revision is a real change, as an agent addressing feedback would make.
            path.write_text('value = 2\n' + '# revised\n' * (self.calls.count('implement') - 1))
            return dict(agent=self.name, summary=f'{self.name} set value to 2')
        assert '+value = 2' in payload['diff'] and payload['tests']
        return dict(agent=self.name, verdict=self.verdict, summary=f'{self.name} reviewed the diff')


class APIAgent(Agent):
    provider_type = 'api'

    def complete(self, payload, instructions, timeout):
        pytest.fail('An API-billed agent must never run without authorization')


def passed(repository, phase, paths):
    assert phase == 'targeted', 'D1: the direct path runs only the targeted tests'
    return CommandResult(('pytest',), 0, '1 passed')


def failing(repository, phase, paths):
    return CommandResult(('pytest',), 1, '1 failed')


@pytest.fixture
def agents(monkeypatch):
    team = {'codex_cli': Agent('codex_cli'), 'claude_cli': Agent('claude_cli'), 'codex_api': APIAgent('codex_api')}
    monkeypatch.setattr(direct_dev, 'ADAPTERS', {name: (lambda *a, agent=agent: agent) for name, agent in team.items()})
    return team


def run_worker(factory, source, tester=passed):
    return EngineeringWorker(factory(tester=tester), source=source, direct_dev=True).once()


def production_projection(job, candidate):
    """Run production's own projection code (f95f459) over a dev record, in a separate process."""
    if subprocess.run(['/usr/bin/git', '-C', str(ROOT), 'cat-file', '-e', PRODUCTION + '^{commit}'],
                      capture_output=True).returncode:
        pytest.skip('production commit not available in this checkout')
    return _run_production(job, candidate)


def _run_production(job, candidate, _cache={}):
    if 'tree' not in _cache:
        import tempfile
        tree = Path(tempfile.mkdtemp(prefix='jarvis-production-'))
        archive = subprocess.run(['/usr/bin/git', '-C', str(ROOT), 'archive', PRODUCTION],
                                 check=True, capture_output=True).stdout
        subprocess.run(['/usr/bin/tar', '-x', '-C', str(tree)], input=archive, check=True)
        _cache['tree'] = tree
    script = (f'import json,sys\nsys.path.insert(0, {str(_cache["tree"])!r})\n'
              'import core\n'
              f'assert core.__file__.startswith({str(_cache["tree"])!r}), core.__file__\n'
              'from core.engineering_jobs import project_job\n'
              'from core.candidate_bridge import project\n'
              'job, candidate = json.load(sys.stdin)\n'
              'print(json.dumps([project_job(job), project(candidate) if candidate else None]))\n')
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=_cache['tree'],
                            input=json.dumps([job, candidate]), capture_output=True, text=True,
                            env=dict(PATH='/usr/bin:/bin', HOME=os.environ['HOME'], PYTHONPATH=str(_cache['tree']),
                                     JARVIS_MODE='development'))
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def check_projections(factory, result):
    store = factory().store
    candidate = store.get(result['candidate_id']) if result['candidate_id'] else None
    dev = project_job(result), project(candidate) if candidate else None
    prod = production_projection(result, candidate)
    for job_view, candidate_view in (dev, prod):
        assert job_view['status'] == result['status'] and job_view['provider'] == result['provider']
        if candidate:
            assert candidate_view['state'] == candidate['state']
    return dev, prod


# --- path routing ---------------------------------------------------------------------

def test_routing_rule_keeps_every_other_job_on_the_legacy_path(setup):
    source, db, factory = setup
    service = factory()
    job = enqueue(db)
    portfolio = Portfolio(db)
    template = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    portfolio.register_project(template | dict(project_id='read-only-fixture', repository=str(source),
                                               read_only=True))
    portfolio.register_project(template | dict(project_id='no-repo-fixture', repository=None))
    cases = {
        'eligible': {},
        'operator_job': dict(operator=dict(mode='codex_builds')),
        'not_code_scope': dict(action=dict(job['action'], scope='documentation')),
        'provider_not_subscription_cli': dict(provider='codex_api'),
        'project_read_only': dict(project_id='read-only-fixture'),
        'project_without_repository': dict(project_id='no-repo-fixture'),
    }
    for reason, change in cases.items():
        assert direct_dev.direct_route(service, job | change)['reason'] == reason, reason
    assert direct_dev.direct_route(service, job | dict(provider='openai'))['path'] == 'legacy'
    for provider in ('codex', 'codex_cli', 'claude_code', 'claude_cli'):
        assert direct_dev.direct_route(service, job | dict(provider=provider))['path'] == 'direct'
    legacy = service.store.create('legacy', ['src/app.py'], ['tests/test_app.py'])
    assert direct_dev.direct_route(service, job | dict(candidate_id=legacy['id']))['reason'] == 'legacy_candidate_resume'
    direct = service.store.create('direct', ['src/app.py'], ['tests/test_app.py'],
                                  source_context=dict(direct_dev_run='run-1'))
    assert direct_dev.direct_route(service, job | dict(candidate_id=direct['id']))['path'] == 'direct'
    log = [json.loads(line) for line in (service.root.parent / 'direct-dev/path-routing.jsonl').read_text().splitlines()]
    assert log[0]['reason'] == 'eligible' and {entry['path'] for entry in log} == {'direct', 'legacy'}


def test_worker_without_opt_in_stays_on_the_legacy_path(setup, monkeypatch):
    source, db, factory = setup
    monkeypatch.setattr(direct_dev, 'run_direct', lambda *a, **k: pytest.fail('direct path must be opt-in'))
    enqueue(db)
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    assert result['status'] == 'completed' and result['provider'] == 'codex'


# --- outcomes --------------------------------------------------------------------------

def test_direct_job_completes_with_live_stages_and_both_projections(setup, agents):
    source, db, factory = setup
    before = tree_bytes(source)
    job = enqueue(db)
    result = run_worker(factory, source)
    assert result['id'] == job['id']
    assert result['status'] in {'completed', 'waiting_for_approval'}, result.get('failure')
    assert result['provider'] == 'codex_cli' and result['attempts'] == 1
    statuses = [h['status'] for h in result['history']]
    for earlier, later in (('planning', 'implementing'), ('implementing', 'testing'), ('testing', 'reviewing')):
        assert statuses.index(earlier) < statuses.index(later)
    assert agents['codex_cli'].calls == ['implement'] and agents['claude_cli'].calls == ['review']
    assert [t['phase'] for t in result['test_result']] == ['targeted']
    assert result['review_result']['verdict'] == 'passed'
    assert result['attribution']['outcome'] == 'multi_provider'
    assert result['provider_route']['provider_requested'] == 'codex'
    assert result['provider_route']['provider_used'] == 'codex_cli'
    candidate = factory().store.get(result['candidate_id'], internal=True)
    assert direct_dev.is_direct_candidate(candidate)
    assert candidate['source_context']['engineering_job_id'] == job['id']
    assert candidate['improvement']['baseline_comparison'] == {'result': 'not_run'}
    assert candidate['commit'] and candidate['reviewer_result']['commit'] == candidate['commit']
    assert tree_bytes(source) == before
    (_, dev_candidate), (prod_job, prod_candidate) = check_projections(factory, result)
    assert prod_candidate['improvement']['implementation_provider'] == 'codex_cli'
    assert prod_candidate['improvement']['review_provider'] == 'claude_cli'
    assert [r['provider'] for r in prod_job['provider_results']] == ['codex_cli', 'claude_cli']


def test_targeted_only_candidate_is_not_apply_eligible(setup, agents, tmp_path):
    """D1 trade-off: apply still demands full_python evidence, so it refuses (fails closed)."""
    source, db, factory = setup
    integration_target(setup, tmp_path)
    enqueue(db)
    result = run_worker(factory, source)
    service = factory(tester=passed)
    assert CandidateApply(service).preview(result['candidate_id'])['eligible'] is False
    apply = CandidateApply(service)
    apply._setup(result['candidate_id'])
    with pytest.raises(ToolDenied, match='Passing test evidence is incomplete'):
        apply._eligible()


@pytest.mark.parametrize('scenario,code,stage', [
    ('tests_failed', 'tests_failed', 'testing'),
    ('needs_revision', 'review_failed', 'reviewing'),
    ('rejected', 'review_failed', 'reviewing'),
    ('reviewer_unavailable', 'provider_authentication', 'reviewing'),
    ('source_not_clean', 'source_not_clean', 'planning'),
])
def test_unsuccessful_outcomes_are_blocked_with_closed_evidence(setup, agents, scenario, code, stage):
    source, db, factory = setup
    tester = failing if scenario == 'tests_failed' else passed
    if scenario in {'needs_revision', 'rejected'}:
        agents['claude_cli'].verdict = scenario
    if scenario == 'reviewer_unavailable':
        agents['claude_cli'].state = 'not_authenticated'
    if scenario == 'source_not_clean':
        (source / 'src/app.py').write_text('dirty\n')
    enqueue(db)
    result = run_worker(factory, source, tester)
    assert result['status'] == 'blocked'
    assert (result['failure']['code'], result['failure']['stage']) == (code, stage)
    assert agents['claude_cli'].calls.count('implement') == 0  # Codex stays the author
    if scenario == 'reviewer_unavailable':
        reasons = {r['provider_id']: r['current_usability'] for r in result['provider_blocker']['reasons']}
        assert reasons['claude_cli'] == 'not_authenticated'
        assert result['provider_blocker']['excluded_providers'] == ['codex_cli']
    if scenario in {'tests_failed', 'needs_revision'}:
        assert len(result['provider_results']) <= 6  # 3 attempts at most
    check_projections(factory, result)


def test_unavailable_default_author_falls_back_to_the_other_cli_never_api(setup, agents):
    source, db, factory = setup
    agents['codex_cli'].state = 'not_authenticated'
    enqueue(db)
    result = run_worker(factory, source)
    # Claude built it; Codex (unauthenticated) cannot review, and the API agent is refused.
    assert agents['claude_cli'].calls == ['implement'] and agents['codex_cli'].calls == []
    assert result['status'] == 'blocked' and result['provider'] == 'claude_cli'
    assert result['provider_route']['fallback_reason'] == 'provider_authentication'
    candidate = factory().store.get(result['candidate_id'], internal=True)
    assert candidate['improvement']['review_provider'] == 'codex_cli'
    check_projections(factory, result)


# --- budget (D1) -----------------------------------------------------------------------

def test_direct_budget_stays_inside_one_job_lease(setup, agents, monkeypatch):
    assert direct_dev.DIRECT_BUDGET_SECONDS + direct_dev.LEASE_MARGIN_SECONDS < LEASE_SECONDS
    now = time.time()
    assert direct_dev.direct_budget(dict(lease_until=now + LEASE_SECONDS), now) == direct_dev.DIRECT_BUDGET_SECONDS
    assert direct_dev.direct_budget(dict(lease_until=now + 100), now) == 100 - direct_dev.LEASE_MARGIN_SECONDS
    # The worker hands DirectDevelopment no more than the lease allows, and every agent
    # call receives no more than what is left of that budget.
    source, db, factory = setup
    budgets = []
    original = direct_dev.DirectDevelopment.run

    def run(self, *args, timeout, **kwargs):
        budgets.append(timeout)
        return original(self, *args, timeout=timeout, **kwargs)
    monkeypatch.setattr(direct_dev.DirectDevelopment, 'run', run)
    job = enqueue(db)
    started = time.time()
    result = run_worker(factory, source)
    assert result['status'] in {'completed', 'waiting_for_approval'}
    lease_end = started + LEASE_SECONDS
    assert budgets and budgets[0] <= direct_dev.DIRECT_BUDGET_SECONDS
    assert started + budgets[0] + direct_dev.LEASE_MARGIN_SECONDS <= lease_end + 1
    timeouts = agents['codex_cli'].timeouts + agents['claude_cli'].timeouts
    assert timeouts and max(timeouts) <= budgets[0]


def test_expired_budget_blocks_without_running_agents(setup, agents):
    source, db, factory = setup
    service = factory(tester=passed)
    jobs = EngineeringJobs(db)
    job = enqueue(db)
    claimed = jobs.claim('owner-1', now=time.time() - LEASE_SECONDS + 5)
    candidate, failure = direct_dev.run_direct(service, jobs, claimed, 'owner-1')
    assert candidate is None and (failure['code'], failure['stage']) == ('candidate_deadline', 'planning')
    assert agents['codex_cli'].calls == [] and agents['claude_cli'].calls == []


def test_advance_checks_the_lease_and_does_not_renew_it(setup):
    _, db, _ = setup
    jobs = EngineeringJobs(db)
    enqueue(db)
    job = jobs.claim('owner-1')
    jobs.advance(job['id'], 'owner-1', 'implementing')
    after = jobs.get(job['id'])
    assert after['current_stage'] == 'implementing' and after['lease_until'] == job['lease_until']
    with pytest.raises(ToolDenied):
        jobs.advance(job['id'], 'someone-else', 'testing')
    with pytest.raises(ValueError):
        jobs.advance(job['id'], 'owner-1', 'completed')
