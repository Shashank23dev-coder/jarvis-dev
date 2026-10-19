"""Planner-scope edges: unscoped jobs never reach the legacy path, and requeueing is fenced and budgeted."""
import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core.engineering_authority import current_owner
from core.engineering_jobs import SCOPE_REQUIRED, EngineeringJobs, management_lock
from core.engineering_portfolio import Portfolio
from core.engineering_worker import EngineeringWorker
from core.self_edit import SelfEditService
from tools.policy import ToolDenied
from test_self_edit import setup
from test_planner_scope import PLAN, SCOPED, recording_tester, run_worker, submit, team


def register(database, source):
    portfolio = Portfolio(database)
    template = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    portfolio.register_project(template | dict(project_id='read-only-fixture', repository=str(source), read_only=True))
    portfolio.register_project(template | dict(project_id='no-repo-fixture', repository=None))
    return portfolio, template


def no_legacy_proposal(monkeypatch):
    monkeypatch.setattr(SelfEditService, 'engineer',
                        lambda *a, **k: pytest.fail('an unscoped job must not reach the legacy proposal path'))


def blocked_job(database, factory, source, team, arguments=SCOPED):
    team['claude_cli'].verdict = 'rejected'
    job = submit(database, arguments)
    result = run_worker(factory, source, recording_tester([]))
    assert result['status'] == 'blocked' and result['attempts'] == 1
    team['claude_cli'].verdict = 'passed'
    return job


# --- 1. unscoped jobs avoid the legacy path ----------------------------------------------

@pytest.mark.parametrize('project_id', ['read-only-fixture', 'no-repo-fixture'])
def test_unscoped_job_for_a_project_without_the_direct_path_is_refused_at_submission(setup, project_id):
    source, database, _ = setup
    register(database, source)
    with pytest.raises(ToolDenied, match=SCOPE_REQUIRED):
        submit(database, dict(PLAN, project_id=project_id))
    assert EngineeringJobs(database).recent() == []


def test_scoped_job_for_a_read_only_project_is_still_accepted(setup):
    source, database, _ = setup
    register(database, source)
    assert submit(database, dict(SCOPED, project_id='read-only-fixture'))['status'] == 'queued'


def test_worker_without_the_direct_path_blocks_an_unscoped_job_clearly(setup, monkeypatch):
    source, database, factory = setup
    no_legacy_proposal(monkeypatch)
    submit(database, PLAN)
    result = EngineeringWorker(factory(tester=recording_tester([])), source=source).once()
    # Before: the legacy proposal raised a pydantic ValueError, projected as worker_failed.
    assert result['status'] == 'blocked' and result['failure']['code'] == 'candidate_policy_denied'
    assert result['candidate_id'] is None


def test_project_made_read_only_after_admission_blocks_an_unscoped_job_clearly(setup, team, monkeypatch):
    source, database, factory = setup
    no_legacy_proposal(monkeypatch)
    portfolio, template = register(database, source)
    submit(database, dict(PLAN, project_id='jarvis-core'))  # the fixtures copy jarvis-core's name
    portfolio.register_project(template | dict(read_only=True), replace=True)
    result = run_worker(factory, source, recording_tester([]))
    assert result['status'] == 'blocked' and result['failure']['code'] == 'candidate_policy_denied'
    assert team['codex_cli'].calls == []


def test_owner_input_revision_cannot_drop_scope_on_a_read_only_project(setup):
    source, database, _ = setup
    register(database, source)
    job = submit(database, dict(SCOPED, project_id='read-only-fixture'))
    jobs = EngineeringJobs(database)
    jobs.control(job['id'], 'pause')
    with pytest.raises(ToolDenied, match=SCOPE_REQUIRED):
        jobs.revise_inputs(job['id'], dict(PLAN, project_id='read-only-fixture'), authority=current_owner.get())
    assert jobs.get(job['id'])['arguments'] == job['arguments']


# --- 2. requeueing takes the worker's lock -----------------------------------------------

def test_requeue_waits_for_the_worker_lock_but_a_live_job_stays_idempotent(setup, team):
    source, database, factory = setup
    job = blocked_job(database, factory, source, team)
    revised = dict(SCOPED, hypothesis='Keep the fix minimal.')
    with management_lock(database):  # the same flock EngineeringWorker.once() holds
        with pytest.raises(ToolDenied, match='Engineering worker or candidate management is active'):
            submit(database, revised, task=job['source'])
        assert EngineeringJobs(database).get(job['id'])['status'] == 'blocked'
        assert run_worker(factory, source, recording_tester([])) is None
    again = submit(database, revised, task=job['source'])
    assert again['status'] == 'queued' and again['arguments'] == revised
    queued = submit(database, revised, task=job['source'])
    with management_lock(database):
        # Idempotent submission of a job that is not being requeued needs no lock.
        assert submit(database, revised, task=job['source'])['history'] == queued['history']


# --- 3. an identical resubmission spends an attempt --------------------------------------

def test_identical_resubmission_requeues_and_spends_one_attempt(setup, team):
    source, database, factory = setup
    job = blocked_job(database, factory, source, team)
    again = submit(database, SCOPED, task=job['source'])
    assert again['status'] == 'queued' and again['attempts'] == 1
    assert 'input_revisions' not in again and again['history'][-1]['event'] == 'resubmitted'
    result = run_worker(factory, source, recording_tester([]))
    assert result['status'] in {'completed', 'waiting_for_approval'}, result.get('failure')
    assert result['attempts'] == 2
