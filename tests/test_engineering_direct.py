"""Engineering jobs on the direct-development path: routing, stages, outcomes, projections, budget."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
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
from test_self_edit import git, setup, tree_bytes
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


# --- prepare for apply -----------------------------------------------------------------

def full(phases, returncodes=None, limits=None):
    """Tester for the preparation step: records phases; optional per-phase return codes."""
    def run(repository, phase, paths):
        phases.append((phase, paths))
        if limits is not None:
            limits.append(repository.pytest_timeout)
        return CommandResult(('pytest',), (returncodes or {}).get(phase, 0), f'{phase} done\n1 passed')
    return run


def waiting_candidate(setup, agents):
    source, db, factory = setup
    # Mentioning permissions makes apply require approval (authority_expansion).
    enqueue(db, goal='Fix the value shown on the Jarvis permissions page')
    result = run_worker(factory, source)
    assert result['status'] == 'waiting_for_approval', result.get('failure')
    return result


def prepare(factory, source, tester):
    return EngineeringWorker(factory(tester=tester), source=source, direct_dev=True).once()


def test_preparation_runs_full_suite_and_second_review_then_apply_accepts(setup, agents, tmp_path):
    source, db, factory = setup
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    service = factory(tester=passed)
    assert CandidateApply(service).preview(job['candidate_id'])['eligible'] is False  # targeted only
    phases, limits = [], []
    result = prepare(factory, source, full(phases, limits=limits))
    # Targeted reused; the fixture's one test file is a single full_python shard with the shard limit.
    assert phases == [('full_python', ['tests/test_app.py'])] and limits == [600]
    assert result['status'] == 'waiting_for_approval'
    candidate = factory().store.get(job['candidate_id'], internal=True)
    preparation = candidate['apply_preparation']
    assert preparation['status'] == 'prepared' and preparation['commit'] == candidate['commit']
    assert (preparation['budget_seconds'], preparation['shard_seconds']) == (3000, 600)
    assert [t['phase'] for t in preparation['tests']] == ['targeted', 'full_python']
    assert [(s['shard'], s['paths'], s['returncode'], s['summary']) for s in preparation['full_python_shards']] == [
        ('python-1', ['tests/test_app.py'], 0, ['1 passed'])]
    stored = [t for t in candidate['test_results'] if t['phase'] == 'full_python'][-1]
    assert stored['paths'] == ['tests'] and stored['sandbox']['shard_seconds'] == 600
    assert candidate['reviewer_result']['purpose'] == 'apply_preparation'
    assert candidate['reviewer_result']['provider'] == 'claude_cli' != candidate['improvement']['implementation_provider']
    assert agents['claude_cli'].calls == ['review', 'review'] and agents['codex_cli'].calls == ['implement']
    assert [t['phase'] for t in result['test_result']][-2:] == ['targeted', 'full_python']
    # Apply's unchanged checks now accept the candidate.
    preview = CandidateApply(factory(tester=passed)).preview(job['candidate_id'])
    assert preview['eligible'] is True, preview
    check_projections(factory, result)
    assert prepare(factory, source, full([])) is None  # prepared once per commit


def suite(phases, candidate, baseline=None):
    """Tester for preparation: `candidate`/`baseline` map phase -> CommandResult (default: pass)."""
    def run(repository, phase, paths):
        phases.append((phase, repository.root.name))
        results = baseline if phase.startswith('baseline_') else candidate
        result = (results or {}).get(phase.removeprefix('baseline_'))
        if isinstance(result, Exception):
            raise result
        return result or CommandResult(('pytest',), 0, '2 passed')
    return run


def pytest_failed(*tests, **kwargs):
    lines = ''.join(f'FAILED {test} - AssertionError\n' for test in tests)
    return CommandResult(('pytest',), 1, f'... \n=== short test summary info ===\n{lines}', **kwargs)


SEAL = ('state', 'reviewer_result', 'tested_fingerprint', 'reviewed_at', 'reviewed_fingerprint',
        'reviewed_diff_hash', 'failure', 'failures')


def test_new_full_suite_failure_is_the_candidates_fault(setup, agents, tmp_path):
    source, db, factory = setup
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    phases = []
    result = prepare(factory, source, suite(phases, {'full_python': pytest_failed(
        'tests/test_app.py::test_value', 'tests/test_old.py::test_broken')},
        {'full_python': pytest_failed('tests/test_old.py::test_broken')}))
    assert [p for p, _ in phases] == ['full_python', 'baseline_full_python']
    assert phases[1][1] == 'baseline'  # the base commit, not the candidate worktree
    assert result['status'] == 'blocked'
    assert (result['failure']['code'], result['failure']['stage']) == ('tests_failed', 'testing')
    candidate = factory().store.get(job['candidate_id'], internal=True)
    preparation = candidate['apply_preparation']
    assert candidate['state'] == 'needs_revision' and preparation['status'] == 'tests_failed'
    assert preparation['failing_tests'] == [
        dict(phase='full_python', test='tests/test_app.py::test_value', pre_existing=False),
        dict(phase='full_python', test='tests/test_old.py::test_broken', pre_existing=True)]
    assert agents['claude_cli'].calls == ['review']  # no second review of a failing commit
    assert not (Path(candidate['worktree']).parent / 'baseline').exists()
    assert CandidateApply(factory(tester=passed)).preview(job['candidate_id'])['eligible'] is False
    check_projections(factory, result)


@pytest.mark.parametrize('scenario,reason', [
    ('pre_existing_only', 'pre_existing_failures_only'),
    ('timeout', 'sandbox_time_limit'),
    ('sandbox_limit', 'sandbox_time_limit'),
    ('baseline_timeout', 'sandbox_time_limit'),
    ('unidentified', 'failures_unidentified'),
])
def test_environment_blocked_preparation_leaves_candidate_and_job_unchanged(
        setup, agents, tmp_path, scenario, reason):
    source, db, factory = setup
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    before = factory().store.get(job['candidate_id'], internal=True)
    old = pytest_failed('tests/test_old.py::test_broken')
    candidate_results, baseline_results = {
        'pre_existing_only': ({'full_python': old}, {'full_python': old}),
        'timeout': ({'full_python': CommandResult(('pytest',), -9, '', timed_out=True)}, None),
        'sandbox_limit': ({'full_python': ToolDenied('Candidate deadline exceeded')}, None),
        'baseline_timeout': ({'full_python': old},
                             {'full_python': CommandResult(('pytest',), -9, '', timed_out=True)}),
        'unidentified': ({'full_python': CommandResult(('pytest',), 1, 'no summary')}, None),
    }[scenario]
    phases = []
    result = prepare(factory, source, suite(phases, candidate_results, baseline_results))
    candidate = factory().store.get(job['candidate_id'], internal=True)
    preparation = candidate['apply_preparation']
    assert (preparation['status'], preparation['reason']) == ('environment_blocked', reason)
    assert preparation['fingerprint'] == candidate['fingerprint']
    if scenario == 'pre_existing_only':
        assert preparation['failing_tests'] == [
            dict(phase='full_python', test='tests/test_old.py::test_broken', pre_existing=True)]
    # Seal, state and job are exactly as before; no second review ran.
    assert {k: candidate.get(k) for k in SEAL} == {k: before.get(k) for k in SEAL}
    assert candidate['state'] == 'passed'
    assert result['status'] == 'waiting_for_approval'
    assert len(result['history']) == len(job['history'])
    assert agents['claude_cli'].calls == ['review']
    # Apply still refuses: the failing full-suite evidence stays on this fingerprint.
    assert CandidateApply(factory(tester=passed)).preview(job['candidate_id'])['eligible'] is False
    apply = CandidateApply(factory(tester=passed))
    apply._setup(job['candidate_id'])
    with pytest.raises(ToolDenied):
        apply._eligible()
    check_projections(factory, result)
    # Never re-run for the same fingerprint.
    rerun = []
    assert prepare(factory, source, suite(rerun, {})) is None and rerun == []


def test_preparation_never_lets_the_author_review(setup, agents):
    source, db, factory = setup
    job = waiting_candidate(setup, agents)
    agents['claude_cli'].state = 'not_authenticated'
    result = prepare(factory, source, full([]))
    assert result['status'] == 'blocked' and result['failure']['stage'] == 'reviewing'
    assert result['failure']['code'] == 'provider_authentication'
    assert agents['codex_cli'].calls == ['implement']  # Codex authored; it is never the reviewer
    candidate = factory().store.get(job['candidate_id'], internal=True)
    assert candidate['apply_preparation']['failure']['exception'] == 'core.direct_dev.NoAgentAvailable'
    check_projections(factory, result)


def test_preparation_refuses_a_commit_changed_after_review(setup, agents):
    source, db, factory = setup
    job = waiting_candidate(setup, agents)
    candidate = factory().store.get(job['candidate_id'], internal=True)
    (Path(candidate['worktree']) / 'src/app.py').write_text('value = 3\n')
    phases = []
    result = prepare(factory, source, full(phases))
    assert phases == [] and result['status'] == 'blocked'
    candidate = factory().store.get(job['candidate_id'], internal=True)
    assert candidate['state'] == 'needs_revision'
    assert candidate['apply_preparation']['failure']['exception'] == 'tools.policy.ToolDenied'


def test_preparation_is_opt_in_with_the_direct_path(setup, agents):
    source, db, factory = setup
    waiting_candidate(setup, agents)
    assert EngineeringWorker(factory(tester=full([])), source=source).once() is None


# --- full_python as release shards -----------------------------------------------------

SHARDED = {'tests/test_self_edit.py': 'def test_a(): pass\n',          # release shard 1
           'tests/test_candidate_apply.py': 'def test_b(): pass\n'}    # release shard 2
# tests/test_app.py is in no release shard, so it runs in a final extra shard.
SHARD_PATHS = [['tests/test_self_edit.py'], ['tests/test_candidate_apply.py'], ['tests/test_app.py']]


def sharded_source(source):
    for path, text in SHARDED.items():
        (source / path).write_text(text)
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Tests in two release shards')


def test_full_python_shards_follow_the_release_shards(tmp_path):
    from core.deployment_release import PYTHON_SHARDS
    shards = direct_dev.full_python_shards(ROOT)
    # Release shards first, as listed; any file they miss runs once in a final extra shard.
    assert shards[:3] == [[p for p in shard if (ROOT / p).is_file()] for shard in PYTHON_SHARDS]
    files = [path for shard in shards for path in shard]
    assert len(files) == len(set(files)) and set(files) == {
        p.relative_to(ROOT).as_posix() for p in (ROOT / 'tests').rglob('test_*.py')}
    (tmp_path / 'tests/sub').mkdir(parents=True)
    for path in ('tests/test_candidate_apply.py', 'tests/test_self_edit.py', 'tests/sub/test_new.py',
                 'tests/helper.py', 'tests/conftest.py'):
        (tmp_path / path).write_text('')
    # Removed release files drop out, added ones run once in an extra shard, helpers never run alone.
    assert direct_dev.full_python_shards(tmp_path) == [
        ['tests/test_self_edit.py'], ['tests/test_candidate_apply.py'], ['tests/sub/test_new.py']]
    assert direct_dev.full_python_shards(tmp_path / 'empty') == [['tests']]


def test_prepare_limits_are_configurable_and_bounded():
    assert direct_dev.prepare_limits({}) == (600, 3000)
    assert direct_dev.prepare_limits(dict(JARVIS_PREPARE_SHARD_SECONDS='300',
                                          JARVIS_PREPARE_BUDGET_SECONDS='1200')) == (300, 1200)
    assert direct_dev.prepare_limits(dict(JARVIS_PREPARE_SHARD_SECONDS='5',
                                          JARVIS_PREPARE_BUDGET_SECONDS='99999')) == (60, 3600)
    assert direct_dev.prepare_limits(dict(JARVIS_PREPARE_SHARD_SECONDS='inf',
                                          JARVIS_PREPARE_BUDGET_SECONDS='-1')) == (900, 300)
    for invalid in ('nan', 'ten', ''):
        assert direct_dev.prepare_limits(dict(JARVIS_PREPARE_SHARD_SECONDS=invalid,
                                              JARVIS_PREPARE_BUDGET_SECONDS=invalid)) == (600, 3000)


def test_sharded_result_keeps_every_shard_and_stays_within_the_evidence_limit():
    class Repository:
        pytest_timeout = 120
    seen = []

    def run(paths):
        seen.append((paths, Repository.pytest_timeout))
        failed = paths == ['b']
        tail = f'\nFAILED {paths[0]}::test_x - AssertionError\n1 failed' if failed else '\n9 passed'
        return CommandResult(('pytest', *paths), int(failed), 'x' * 12000 + tail, sandbox=dict(backend='docker'))
    complete, stored = direct_dev.run_sharded(Repository, run, [['a'], ['b'], ['c']], 600)
    assert seen == [(['a'], 600), (['b'], 600), (['c'], 600)] and Repository.pytest_timeout == 120
    assert complete.returncode == stored.returncode == 1 and not stored.truncated and not stored.timed_out
    assert direct_dev.failing_tests(complete) == {'b::test_x'}
    assert len(stored.stdout) < 16000 and stored.stdout.count('=== python-') == 3
    assert [r['shard'] for r in stored.runners] == ['python-1', 'python-2', 'python-3']
    assert [r['returncode'] for r in stored.runners] == [0, 1, 0]
    for runner in stored.runners:  # each shard's span holds its own summary
        assert stored.stdout[slice(*runner['stdout_span'])].endswith('1 failed' if runner['returncode'] else '9 passed')
    shards = stored.sandbox['shards']
    assert [(s['shard'], s['paths'], s['summary']) for s in shards] == [
        ('python-1', ['a'], ['9 passed']), ('python-2', ['b'], ['1 failed']), ('python-3', ['c'], ['9 passed'])]
    assert stored.sandbox['backend'] == 'docker' and stored.sandbox['planned_shards'] == 3


def test_preparation_runs_release_shards_in_order_and_apply_accepts(setup, agents, tmp_path, monkeypatch):
    source, db, factory = setup
    sharded_source(source)
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    monkeypatch.setenv('JARVIS_PREPARE_SHARD_SECONDS', '240')
    phases, limits = [], []
    result = prepare(factory, source, full(phases, limits=limits))
    assert phases == [('full_python', paths) for paths in SHARD_PATHS] and limits == [240, 240, 240]
    assert result['status'] == 'waiting_for_approval'
    candidate = factory().store.get(job['candidate_id'], internal=True)
    preparation = candidate['apply_preparation']
    assert preparation['status'] == 'prepared' and preparation['shard_seconds'] == 240
    assert [s['paths'] for s in preparation['full_python_shards']] == SHARD_PATHS
    assert all(s['returncode'] == 0 and not s['timed_out'] for s in preparation['full_python_shards'])
    preview = CandidateApply(factory(tester=passed)).preview(job['candidate_id'])
    assert preview['eligible'] is True, preview


def test_new_failure_in_one_shard_reruns_only_that_shard_on_the_base_commit(setup, agents, tmp_path):
    source, db, factory = setup
    sharded_source(source)
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    calls = []

    def tester(repository, phase, paths):
        calls.append((phase, repository.root.name, paths))
        if phase == 'full_python' and paths == SHARD_PATHS[1]:
            return pytest_failed('tests/test_candidate_apply.py::test_b')
        return CommandResult(('pytest',), 0, '1 passed')
    result = prepare(factory, source, tester)
    assert [c[0] for c in calls] == ['full_python'] * 3 + ['baseline_full_python']
    assert calls[-1][1:] == ('baseline', SHARD_PATHS[1])  # only the failing shard, on the base commit
    assert result['status'] == 'blocked' and result['failure']['code'] == 'tests_failed'
    preparation = factory().store.get(job['candidate_id'], internal=True)['apply_preparation']
    assert preparation['failing_tests'] == [
        dict(phase='full_python', test='tests/test_candidate_apply.py::test_b', pre_existing=False)]
    assert [s['returncode'] for s in preparation['full_python_shards']] == [0, 1, 0]
    assert CandidateApply(factory(tester=passed)).preview(job['candidate_id'])['eligible'] is False


def test_a_timed_out_shard_stops_the_run_as_an_environment_limit(setup, agents, tmp_path):
    source, db, factory = setup
    sharded_source(source)
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    phases = []
    timed_out = CommandResult(('pytest',), -9, '', timed_out=True)
    result = prepare(factory, source, suite(phases, {'full_python': timed_out}))
    assert [p for p, _ in phases] == ['full_python']  # later shards and the baseline never run
    assert result['status'] == 'waiting_for_approval'
    preparation = factory().store.get(job['candidate_id'], internal=True)['apply_preparation']
    assert (preparation['status'], preparation['reason']) == ('environment_blocked', 'sandbox_time_limit')
    assert [(s['shard'], s['timed_out']) for s in preparation['full_python_shards']] == [('python-1', True)]
    assert CandidateApply(factory(tester=passed)).preview(job['candidate_id'])['eligible'] is False


# --- preparation yields to queued jobs -------------------------------------------------

def yielding_tester(calls, db, *, fail=None):
    """Targeted passes; full_python shards are recorded; one chat job is queued during the first shard."""
    queued = []

    def run(repository, phase, paths):
        calls.append((phase, paths))
        if phase == 'full_python' and not queued:
            queued.append(enqueue(db, goal='Chat: fix the value in Jarvis'))
        if phase == 'full_python' and paths == fail:
            return pytest_failed(f'{paths[0]}::test_new')
        return CommandResult(('pytest',), 0, f'{phase} {paths}\n1 passed')
    return run


def paused_candidate(setup, agents, tmp_path, calls, **kwargs):
    source, db, factory = setup
    sharded_source(source)
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    before = factory().store.get(job['candidate_id'], internal=True)
    worker = EngineeringWorker(factory(tester=yielding_tester(calls, db, **kwargs)), source=source, direct_dev=True)
    result = worker.once()
    return job, before, worker, result


def test_preparation_yields_to_a_queued_chat_job_and_resumes_from_the_next_shard(setup, agents, tmp_path):
    source, db, factory = setup
    calls = []
    job, before, worker, result = paused_candidate(setup, agents, tmp_path, calls)
    jobs = EngineeringJobs(db)
    # Shard 1 ran, a chat job was queued during it, so shard 2 never started.
    assert calls == [('full_python', SHARD_PATHS[0])]
    assert result['id'] == job['id'] and result['status'] == 'waiting_for_approval'
    candidate = factory().store.get(job['candidate_id'], internal=True)
    preparation = candidate['apply_preparation']
    assert preparation['status'] == 'paused' and not candidate['busy']
    assert preparation['checkpoint']['shards_completed'] == 1 and preparation['checkpoint']['shards_planned'] == 3
    assert [p['after_shard'] for p in preparation['pauses']] == [1]
    started_at = preparation['started_at']
    # Seal and state are exactly as before; apply still refuses (no full_python evidence yet).
    assert {k: candidate.get(k) for k in SEAL} == {k: before.get(k) for k in SEAL}
    assert not [t for t in candidate['test_results'] if t['phase'] == 'full_python']
    assert CandidateApply(factory(tester=passed)).preview(job['candidate_id'])['eligible'] is False
    # The next worker pass runs the chat job (not preparation): it claims and finishes it.
    chat = [j for j in jobs.recent() if j['id'] != job['id']][0]
    assert chat['status'] == 'queued'
    calls.clear()
    ran = worker.once()
    assert ran['id'] == chat['id'] and ran['status'] != 'queued' and 'claimed' in [h['event'] for h in ran['history']]
    assert ('full_python', SHARD_PATHS[1]) not in calls  # the chat job ran no preparation shard
    assert not jobs.queued()
    # Idle again: preparation resumes from shard 2; shard 1 is not rerun, and its evidence is kept.
    calls.clear()
    resumed = worker.once()
    assert resumed['id'] == job['id'] and resumed['status'] == 'waiting_for_approval'
    assert calls == [('full_python', paths) for paths in SHARD_PATHS[1:]]
    candidate = factory().store.get(job['candidate_id'], internal=True)
    preparation = candidate['apply_preparation']
    assert preparation['status'] == 'prepared' and preparation['started_at'] == started_at
    assert [(s['shard'], s['paths']) for s in preparation['full_python_shards']] == [
        (f'python-{n}', paths) for n, paths in enumerate(SHARD_PATHS, 1)]
    assert preparation['full_python_shards'][0]['summary'] == ['1 passed']
    assert preparation['pauses'][0]['checkpoint'] == 'reused' and preparation['pauses'][0]['resumed_run_id'] == preparation['run_id']
    stored = [t for t in candidate['test_results'] if t['phase'] == 'full_python'][-1]
    assert stored['paths'] == ['tests'] and stored['sandbox']['planned_shards'] == 3
    assert f"full_python {SHARD_PATHS[0]}" in stored['stdout']  # shard 1's real output, from the first run
    assert CandidateApply(factory(tester=passed)).preview(job['candidate_id'])['eligible'] is True
    assert not direct_dev._checkpoint_path(direct_dev._state_dir(factory()), job['candidate_id']).exists()
    check_projections(factory, resumed)


def test_a_failure_before_the_pause_is_still_attributed_after_resuming(setup, agents, tmp_path):
    source, db, factory = setup
    calls = []
    job, _, worker, result = paused_candidate(setup, agents, tmp_path, calls, fail=SHARD_PATHS[0])
    assert factory().store.get(job['candidate_id'], internal=True)['apply_preparation']['status'] == 'paused'
    jobs = EngineeringJobs(db)
    chat = [j for j in jobs.recent() if j['id'] != job['id']][0]
    jobs.control(chat['id'], 'pause')  # the owner parks the chat job: preparation may resume
    calls.clear()
    result = worker.once()
    # Shards 2-3 run, then only the failing shard 1 on the base commit; the failure is new.
    assert [c[0] for c in calls] == ['full_python', 'full_python', 'baseline_full_python']
    assert calls[-1][1] == SHARD_PATHS[0]
    assert result['status'] == 'blocked' and result['failure']['code'] == 'tests_failed'
    preparation = factory().store.get(job['candidate_id'], internal=True)['apply_preparation']
    assert preparation['failing_tests'] == [
        dict(phase='full_python', test=f'{SHARD_PATHS[0][0]}::test_new', pre_existing=False)]
    assert [s['returncode'] for s in preparation['full_python_shards']] == [1, 0, 0]


@pytest.mark.parametrize('tamper', ['edit', 'delete'])
def test_a_changed_or_missing_checkpoint_reruns_every_shard(setup, agents, tmp_path, tamper):
    source, db, factory = setup
    calls = []
    job, _, worker, _ = paused_candidate(setup, agents, tmp_path, calls, fail=SHARD_PATHS[0])
    path = direct_dev._checkpoint_path(direct_dev._state_dir(factory()), job['candidate_id'])
    if tamper == 'edit':  # forge a passing shard 1
        path.write_text(path.read_text().replace('"returncode": 1', '"returncode": 0'))
    else:
        path.unlink()
    jobs = EngineeringJobs(db)
    jobs.control([j for j in jobs.recent() if j['id'] != job['id']][0]['id'], 'pause')
    calls.clear()
    result = worker.once()
    assert [c for c in calls if c[0] == 'full_python'] == [('full_python', paths) for paths in SHARD_PATHS]
    preparation = factory().store.get(job['candidate_id'], internal=True)['apply_preparation']
    assert preparation['pauses'][0]['checkpoint'] == 'discarded'
    # The rerun shard 1 really fails again; the forged pass was never used.
    assert result['status'] == 'blocked' and [s['returncode'] for s in preparation['full_python_shards']] == [1, 0, 0]


def test_every_preparation_run_finishes_at_least_one_shard(setup, agents, tmp_path):
    """Even with a job always waiting, preparation progresses one shard per run."""
    source, db, factory = setup
    sharded_source(source)
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    calls, jobs = [], EngineeringJobs(db)
    service = factory(tester=full(calls))
    states = []
    for _ in range(3):
        pending, _ = direct_dev.pending_preparation(service)
        candidate = direct_dev.prepare_for_apply(direct_dev.development_service(service, job.get('project_id')),
                                                 jobs, pending, pause=lambda: True)
        states.append(candidate['apply_preparation']['status'])
    assert calls == [('full_python', paths) for paths in SHARD_PATHS]
    assert states == ['paused', 'paused', 'prepared']
    preparation = candidate['apply_preparation']
    assert [p['after_shard'] for p in preparation['pauses']] == [1, 2]
    assert all(p['checkpoint'] == 'reused' for p in preparation['pauses'])
    assert preparation['active_seconds'] >= 0 and direct_dev.pending_preparation(service) is None


def test_preparation_keeps_the_worker_heartbeat_fresh(setup, agents, monkeypatch):
    from core import engineering_worker
    source, db, factory = setup
    job = waiting_candidate(setup, agents)
    monkeypatch.setattr(engineering_worker, 'HEARTBEAT_SECONDS', 0.05)

    def observed():
        with db.transaction() as connection:
            return connection.execute('SELECT observed FROM engineering_worker_status WHERE id=1').fetchone()[0]
    seen = []

    def slow(repository, phase, paths):
        start = observed()
        time.sleep(0.5)  # a long shard, no job lease held
        seen.append((start, observed()))
        return CommandResult(('pytest',), 0, '1 passed')
    result = EngineeringWorker(factory(tester=slow), source=source, direct_dev=True).once()
    assert result['id'] == job['id'] and seen
    assert all(after > start for start, after in seen)  # refreshed while the shard ran
    assert time.time() - observed() < 1
    # The heartbeat thread stops with the step.
    assert not [t for t in threading.enumerate() if t.name == 'engineering-heartbeat']


def test_queued_counts_only_jobs_claim_would_run(setup):
    _, db, _ = setup
    jobs = EngineeringJobs(db)
    assert not jobs.queued()
    job = enqueue(db)
    assert jobs.queued()
    jobs.control(job['id'], 'pause')
    assert not jobs.queued()


@pytest.mark.skipif(os.environ.get('JARVIS_DOCKER_TESTS') != '1', reason='Opt-in Docker integration')
def test_preparation_runs_real_shards_in_the_sandbox(setup, agents, tmp_path):
    source, db, factory = setup
    sharded_source(source)
    integration_target(setup, tmp_path)
    job = waiting_candidate(setup, agents)
    result = prepare(factory, source, None)
    candidate = factory().store.get(job['candidate_id'], internal=True)
    preparation = candidate['apply_preparation']
    assert preparation['status'] == 'prepared', preparation
    assert [(s['paths'], s['returncode'], s['summary'][-1:]) for s in preparation['full_python_shards']] == [
        (paths, 0, ['1 passed']) for paths in SHARD_PATHS]
    stored = [t for t in candidate['test_results'] if t['phase'] == 'full_python'][-1]
    assert [s['sandbox']['backend'] for s in stored['sandbox']['shards']] == ['docker'] * 3
    assert all(s['sandbox']['limits']['wall_seconds'] <= 600 for s in stored['sandbox']['shards'])
    assert result['status'] == 'waiting_for_approval'
    assert CandidateApply(factory(tester=passed)).preview(job['candidate_id'])['eligible'] is True
