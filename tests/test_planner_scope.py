"""Planner scope found during implementation; a rejected attempt never uses up the task's candidate."""
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core import direct_dev
from core.candidate_action import EngineeringAction, goal_digest
from core.candidate_tools import BrainEngineeringArguments, CandidateTools, CreateArguments, OperatorArguments
from core.direct_dev import NO_TESTS_NOTE, DirectDevelopment
from core.engineering_jobs import MAX_ATTEMPTS, EngineeringJobs
from core.engineering_worker import EngineeringWorker
from tools import CommandResult
from tools.policy import ToolDenied
from test_self_edit import setup
from test_engineering_direct import Agent, APIAgent
from test_direct_dev import Stub, fixture_project

GOAL = 'Fix the value in Jarvis'
PLAN = dict(problem_detected='The value is incorrect.', hypothesis='Setting the value to 2 fixes it.')
SCOPED = dict(PLAN, inspect_paths=['src/app.py'], targeted_tests=['tests/test_app.py'])


class DiscoveringAgent(Agent):
    """Finds its own scope: fixes src/app.py and adds the test that covers it."""
    write_test = True

    def complete(self, payload, instructions, timeout):
        if payload['action'] == 'implement' and self.write_test:
            (Path(payload['worktree']) / 'tests/test_value.py').write_text(
                'from src.app import value\n\n\ndef test_value_is_two():\n    assert value == 2\n')
        return super().complete(payload, instructions, timeout)


@pytest.fixture
def team(monkeypatch):
    agents = {'codex_cli': DiscoveringAgent('codex_cli'), 'claude_cli': Agent('claude_cli'),
              'codex_api': APIAgent('codex_api')}
    monkeypatch.setattr(direct_dev, 'ADAPTERS', {name: (lambda *a, agent=agent: agent) for name, agent in agents.items()})
    return agents


def submit(database, arguments, *, provider='codex', task=None):
    task = task or dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    action = EngineeringAction(provider=provider, scope='code', goal_digest=goal_digest(GOAL), **task)
    return EngineeringJobs(database).create(GOAL, action.model_dump(), arguments, task)


def recording_tester(calls):
    def tester(repository, phase, paths):
        calls.append((phase, list(paths)))
        return CommandResult(('pytest',), 0, '1 passed')
    return tester


def run_worker(factory, source, tester):
    return EngineeringWorker(factory(tester=tester), source=source, direct_dev=True).once()


# --- 1. scope is discovered during implementation ---------------------------------------

def test_plan_validation_accepts_a_plan_without_files_or_tests():
    args = BrainEngineeringArguments.model_validate(PLAN)
    assert args.inspect_paths == [] and args.targeted_tests == []
    # The operator baseline and the legacy proposal path still need explicit inputs.
    with pytest.raises(ValueError):
        OperatorArguments.model_validate(PLAN)
    with pytest.raises(ValueError):
        CreateArguments.model_validate({})


def test_unscoped_job_runs_the_tests_the_agent_added_and_is_reviewed(setup, team):
    source, database, factory = setup
    job = submit(database, PLAN)
    assert job['status'] == 'queued'
    assert (job['arguments']['inspect_paths'], job['arguments']['targeted_tests']) == ([], [])
    calls = []
    result = run_worker(factory, source, recording_tester(calls))
    assert result['status'] in {'completed', 'waiting_for_approval'}, result.get('failure')
    # The discovered test ran in the sandbox, and the reviewer saw the change and its tests.
    assert calls == [('targeted', ['tests/test_value.py'])]
    assert team['claude_cli'].calls == ['review']
    candidate = factory().store.get(result['candidate_id'], internal=True)
    assert sorted(candidate['files_changed']) == ['src/app.py', 'tests/test_value.py']
    assert candidate['targeted_tests'] == ['tests/test_value.py']
    assert candidate['reviewer_result']['verdict'] == 'passed'


def test_planned_tests_are_kept_and_discovered_tests_join_them(setup, team):
    source, database, factory = setup
    submit(database, SCOPED)
    calls = []
    result = run_worker(factory, source, recording_tester(calls))
    assert result['status'] in {'completed', 'waiting_for_approval'}, result.get('failure')
    assert calls == [('targeted', ['tests/test_app.py', 'tests/test_value.py'])]


def test_change_without_any_tests_is_sent_back_and_never_reviewed(tmp_path):
    _, database = fixture_project(tmp_path)
    codex, claude = Stub('codex_cli'), Stub('claude_cli')  # edits src/calc.py only
    calls = []
    report = DirectDevelopment(database, candidate_root=tmp_path / 'candidates', state_dir=tmp_path / 'state',
                               agents=[codex, claude], tester=recording_tester(calls)).run(
        'dd-fixture', 'Fix add()', [], [])
    assert report['status'] == 'tests_failed' and report['attempts_exhausted']
    assert [a['targeted_tests'] for a in report['attempts']] == [[]] * 3
    assert calls == [] and claude.calls == []
    assert NO_TESTS_NOTE in codex.payloads[1]['task'] and NO_TESTS_NOTE in codex.payloads[2]['task']


def test_routes_that_cannot_discover_scope_still_require_it(setup):
    _, database, _ = setup
    with pytest.raises(ToolDenied, match='explicit inspect_paths and targeted_tests'):
        submit(database, PLAN, provider='openai')
    assert submit(database, SCOPED, provider='openai')['status'] == 'queued'


# --- 2. a rejected attempt does not use up the task's candidate -------------------------

def test_rejected_attempt_lets_the_same_task_create_a_new_candidate(setup, team):
    source, database, factory = setup
    team['claude_cli'].verdict = 'rejected'
    first = submit(database, SCOPED)
    task = first['source']
    result = run_worker(factory, source, recording_tester([]))
    assert result['status'] == 'blocked' and result['attempts'] == 1
    rejected = result['candidate_id']
    # Before: 'Engineering job source mismatch'. Now the revised plan requeues the task's job.
    revised = dict(SCOPED, hypothesis='The value must be 2; keep the fix minimal.')
    again = submit(database, revised, task=task)
    assert again['id'] == first['id'] and again['status'] == 'queued'
    assert again['candidate_id'] is None and again['attempts'] == 1  # the budget is spent, not renewed
    assert again['arguments'] == revised and again['input_revisions'][0]['arguments'] == first['arguments']
    assert again['history'][-1]['status'] == 'queued'
    team['claude_cli'].verdict = 'passed'
    result = run_worker(factory, source, recording_tester([]))
    assert result['status'] in {'completed', 'waiting_for_approval'}, result.get('failure')
    assert result['attempts'] == 2 and result['candidate_id'] != rejected
    assert result['candidate_ids'] == [rejected, result['candidate_id']]


def test_resubmission_is_bounded_by_the_existing_retry_budget(setup, team):
    source, database, factory = setup
    team['claude_cli'].verdict = 'rejected'
    task = submit(database, SCOPED)['source']
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = run_worker(factory, source, recording_tester([]))
        assert result['attempts'] == attempt
        if attempt < MAX_ATTEMPTS:
            assert result['status'] == 'blocked'
            assert submit(database, SCOPED, task=task)['status'] == 'queued'
    assert result['status'] == 'failed' and len(result['candidate_ids']) == MAX_ATTEMPTS
    # Exhausted: an identical submission returns the failed job; a new plan is refused.
    assert submit(database, SCOPED, task=task)['status'] == 'failed'
    with pytest.raises(ToolDenied, match='retry budget exhausted'):
        submit(database, dict(SCOPED, hypothesis='Another idea.'), task=task)
    assert run_worker(factory, source, recording_tester([])) is None


def test_live_job_is_still_idempotent_and_paused_job_is_not_resubmitted(setup):
    _, database, _ = setup
    job = submit(database, SCOPED)
    assert submit(database, SCOPED, task=job['source'])['history'] == job['history']
    with pytest.raises(ToolDenied, match='source mismatch'):
        submit(database, PLAN, task=job['source'])
    EngineeringJobs(database).control(job['id'], 'pause')
    assert submit(database, SCOPED, task=job['source'])['status'] == 'blocked'


def test_in_turn_rejected_candidate_frees_the_slot_but_a_live_one_holds_it(setup):
    _, database, factory = setup
    tools = CandidateTools('Fix the value', database, lambda db: factory(),
                           source_context=dict(task_id=str(uuid4()), conversation_id=str(uuid4())))
    denied = tools.create(['.env'], ['tests/test_app.py'], project_id='jarvis-core')
    assert denied.state == 'rejected' and tools.created is False and tools.attempts == 1
    # Before: 'One candidate per explicit request'. The real legacy candidate now runs.
    passed = tools.create(['src/app.py'], ['tests/test_app.py'], project_id='jarvis-core')
    assert passed.state == 'passed' and tools.created is True
    with pytest.raises(ToolDenied, match='One candidate per explicit request'):
        tools.create(['src/app.py'], ['tests/test_app.py'], project_id='jarvis-core')


def test_in_turn_engineer_denial_creates_nothing_and_queued_job_holds_the_slot(setup):
    _, database, factory = setup
    tools = CandidateTools(GOAL, database, lambda db: factory(),
                           source_context=dict(task_id=str(uuid4()), conversation_id=str(uuid4())))
    with pytest.raises(ToolDenied):
        tools.engineer(['../outside.py'], [], PLAN['problem_detected'], PLAN['hypothesis'])
    assert tools.attempts == 0 and tools.created is False
    args = BrainEngineeringArguments.model_validate(PLAN).model_dump(exclude_none=True)
    result = tools.engineer(**args)
    assert result.jobs[0]['status'] == 'queued' and tools.created is True
    with pytest.raises(ToolDenied, match='One candidate per authorized task'):
        tools.engineer(**args)
