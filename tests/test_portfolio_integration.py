"""Project audit metadata never becomes filesystem or execution authority."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.candidate_broker import Broker
from core.candidate_tools import CandidateTools
from core.engineering_decision import Decisions
from core.engineering_jobs import EngineeringJobs, project_job
from core.engineering_portfolio import Portfolio
from core.engineering_tools import project_runtime
from core.engineering_worker import EngineeringWorker
from core.self_state import SelfState
from core.task_store import TaskStore
from tools import ToolRegistry, RepositoryTools
from tools.policy import ToolDenied
from test_engineering import ARGS, model, run_checks
from test_engineering_jobs import enqueue
from test_self_edit import setup, tree_bytes
from test_candidate_bridge import request

pytestmark = pytest.mark.usefixtures('owner')


def test_job_project_association_projection_and_reopen(setup):
    _, db, _ = setup
    job = enqueue(db, project_id='jarvis-core')
    assert job['project_id'] == 'jarvis-core'
    assert job['decision']['outcome'] == 'BUILD' and job['decision']['rationale']
    assert project_job(project_job(job)) == project_job(job)
    db.close()
    assert EngineeringJobs(db).get(job['id'])['project_id'] == 'jarvis-core'


def test_unknown_project_default_and_explicit_unknown_are_rejected(setup):
    _, db, _ = setup
    with pytest.raises(ToolDenied):
        enqueue(db, goal='Fix an unknown project')
    with pytest.raises(ToolDenied):
        enqueue(db, project_id='unregistered')
    assert EngineeringJobs(db).recent() == []


def test_legacy_missing_fields_idempotent_and_worker_compatible(setup, monkeypatch):
    source, db, factory = setup
    job = enqueue(db)
    with db.transaction() as connection:
        legacy = EngineeringJobs(db)._get(connection, job['id'])
        legacy.pop('project_id')
        legacy.pop('decision')
        connection.execute('UPDATE engineering_jobs SET record=? WHERE id=?', (json.dumps(legacy), job['id']))
    jobs = EngineeringJobs(db)
    old = jobs.get(job['id'])
    assert jobs.create(old['goal'], old['action'], old['arguments'], old['source']) == old
    assert project_job(old)['project_id'] is None and project_job(old)['decision'] is None
    monkeypatch.setenv('JARVIS_MODE', 'development')
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    assert result['status'] == 'blocked'
    assert result['candidate_id'] is None


def test_project_reference_is_candidate_context_never_root(setup, monkeypatch):
    source, db, factory = setup
    before = tree_bytes(source)
    job = enqueue(db, project_id='jarvis-core')
    monkeypatch.setenv('JARVIS_MODE', 'development')
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    candidate = factory().store.get(result['candidate_id'])
    assert candidate['source_context']['project_id'] == 'jarvis-core'
    assert candidate['source_context']['decision_id'] == job['decision']['id']
    assert candidate['source_root'] == str(source) and tree_bytes(source) == before


@pytest.mark.parametrize('goal,outcome', [('Add Claude support', 'USE_EXISTING'),
    ('Connect my trading bot to Jarvis', 'INTEGRATE'), ('Build CreatorOS', 'NEED_MORE_INFORMATION'),
    ('Fix my trading bot', 'BUILD')])
def test_no_speculative_job_and_refused_rationale_is_persistent(setup, goal, outcome):
    source, db, _ = setup
    before = tree_bytes(source)
    if goal in {'Add Claude support', 'Build CreatorOS'}:
        with pytest.raises(ToolDenied):
            enqueue(db, goal=goal)
    else:
        job = enqueue(db, goal=goal)
        assert job['project_id'] != 'jarvis-core'
        assert Decisions(db).recent()[0]['outcome'] == outcome
    assert tree_bytes(source) == before



def test_runtime_includes_closed_portfolio_and_decisions(setup):
    _, db, _ = setup
    job = enqueue(db, project_id='jarvis-core')
    value = EngineeringJobs(db).runtime()
    assert len(value['portfolio']['projects']) == 3
    assert len(value['portfolio']['capabilities']) == 14
    assert value['decisions'][0]['id'] == job['decision']['id']
    assert project_runtime(project_runtime(value)) == project_runtime(value)
    assert len(json.dumps(project_runtime(value))) < 48000


def test_owner_decision_and_project_tools_no_authority_expansion(setup):
    _, db, _ = setup
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    tools = CandidateTools('Add Claude support', db, source_context=context)
    bound = tools.bind(ToolRegistry())
    assert {'decide_engineering', 'inspect_portfolio_project'} <= bound.schemas().keys()
    assert bound._definitions['decide_engineering'].handler().outcome == 'USE_EXISTING'
    creator = bound._definitions['inspect_portfolio_project'].handler(project_id='creator-os')
    assert creator.project.repository is None and creator.project.status == 'needs_discovery'
    tools.authority = None
    for handler, args in [(tools.decide, {}), (tools.inspect_project, dict(project_id='creator-os'))]:
        with pytest.raises(ToolDenied): handler(**args)


def test_broker_project_and_decision_closed_arguments(setup, monkeypatch):
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    broker = Broker(factory(), source=source)
    value = broker.execute(request('decision', 'Add Claude support', {}))
    assert value['outcome'] == 'USE_EXISTING'
    assert broker.execute(request('project_inspect', 'Where is CreatorOS?', dict(project_id='creator-os')))['project']['repository'] is None
    with pytest.raises(ValueError):
        broker.execute(request('project_inspect', 'Inspect project', dict(project_id='trading-bot', command='start')))
    assert EngineeringJobs(db).recent() == []


def test_prior_missing_information_cannot_be_bypassed_by_queue_call(setup):
    _, db, _ = setup
    job_goal = 'Inspect the repository'
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    tools = CandidateTools(job_goal, db, source_context=context)
    decision = tools.decide(project_id='jarvis-core', required_capabilities=['testing'])
    assert decision.outcome == 'NEED_MORE_INFORMATION'
    from core.candidate_action import EngineeringAction, goal_digest
    action = EngineeringAction(provider='codex', scope='code', goal_digest=goal_digest(job_goal), **context)
    job = EngineeringJobs(db).create(job_goal, action.model_dump(), ARGS | {'project_id': 'jarvis-core'}, context)
    assert job['status'] == 'queued'
    assert job['decision']['outcome'] == 'NEED_MORE_INFORMATION'


def test_explicit_build_decision_keeps_relevant_and_required_distinct(setup):
    _, db, _ = setup
    goal = 'Fix the value'
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    tools = CandidateTools(goal, db, source_context=context)
    decision = tools.decide(project_id='jarvis-core')
    assert decision.outcome == 'BUILD' and decision.capability_ids and not decision.required_capabilities
    from core.candidate_action import EngineeringAction, goal_digest
    action = EngineeringAction(provider='codex', scope='code', goal_digest=goal_digest(goal), **context)
    job = EngineeringJobs(db).create(goal, action.model_dump(), ARGS | {'project_id': 'jarvis-core'}, context)
    assert job['project_id'] == 'jarvis-core' and job['decision']['outcome'] == 'BUILD'


def test_current_successful_tool_evidence_is_used_in_preflight(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'app.py').write_text('value = 1\n')
    store = TaskStore(tmp_path / 'state.sqlite3')
    task = store.create_task('Inspect the repository')
    registry = ToolRegistry(RepositoryTools(root))
    assert registry.execute('read_text', json.dumps(dict(path='app.py')), task_id=task['id'], task_store=store).ok
    tools = CandidateTools(task['goal'], store.database, source_context=dict(task_id=task['id'], conversation_id=task['conversation_id']))
    result = tools.decide(project_id='jarvis-core', required_capabilities=['repo-files'])
    assert result.outcome == 'USE_EXISTING' and result.capability_states['repo-files'] == 'available'


def test_self_state_projects_jobs_decisions_and_privacy(setup, monkeypatch, tmp_path):
    _, db, _ = setup
    job = enqueue(db, project_id='jarvis-core')
    live = EngineeringJobs(db).runtime()
    store = TaskStore(tmp_path / 'chat.sqlite3')
    task = store.create_task('What projects do you manage?')
    tools = CandidateTools(task['goal'], store.database, source_context=dict(task_id=task['id'], conversation_id=task['conversation_id']))
    state = SelfState(store, task['id'], tools)
    monkeypatch.setattr(state, 'engineering_runtime', lambda: live | dict(candidate_records={}))
    result = state.snapshot([], [])
    assert len(result['portfolio']['projects']) == 3
    assert result['engineering']['jobs'][0]['project_id'] == 'jarvis-core'
    assert result['pre_engineering_decisions'][0]['rationale']
    assert result['facts']['capability.self-state.status'] == 'available'
    assert result['facts']['capability.docker-sandbox.status'] in {'unproven', 'unknown'}
    tools.authority = None
    assert state.snapshot([], [])['portfolio'] is None


def test_job_input_recovery_cannot_change_project(setup):
    _, db, _ = setup
    job = enqueue(db, project_id='jarvis-core')
    jobs = EngineeringJobs(db)
    jobs.control(job['id'], 'pause')
    from core.engineering_authority import current_owner
    with pytest.raises(ToolDenied):
        jobs.revise_inputs(job['id'], ARGS | dict(project_id='trading-bot'), authority=current_owner.get())
    assert jobs.get(job['id'])['project_id'] == 'jarvis-core'


def test_legacy_candidate_tool_fails_closed_without_a_resolved_project(setup, monkeypatch):
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    arguments = dict(inspect_paths=ARGS['inspect_paths'], targeted_tests=ARGS['targeted_tests'])
    # No project named: a clarification, never a Jarvis default.
    with pytest.raises(ToolDenied) as caught:
        CandidateTools('Add Claude support', db, lambda _: factory()).create(**arguments)
    assert caught.value.reason == 'unknown_project'
    with pytest.raises(ToolDenied):
        Broker(factory(), source=source).execute(request('create', 'Add Claude support', arguments))
    with db.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM candidates').fetchone()[0] == 0
    # Advisory capability evidence still does not block a uniquely resolved project.
    assert CandidateTools('Add Claude support to Jarvis', db, lambda _: factory()).create(**arguments).state == 'passed'
    result = Broker(factory(), source=source).execute(request('create', 'Add Claude support to Jarvis', arguments))
    assert result['state'] == 'passed'
    with db.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM candidates').fetchone()[0] == 2


@pytest.mark.parametrize('path', ['core/engineering_portfolio.py', 'core/engineering_decision.py'])
def test_portfolio_policy_code_is_protected_from_candidates(setup, path):
    assert enqueue(setup[1], inspect_paths=[path])['status'] == 'queued'
