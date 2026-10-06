"""Semantic Brain authorization and bounded broker capabilities."""
import json
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core.agent import AgentRunner
from core.brain import BrainSettings, JarvisBrain
from core.candidate_action import EngineeringAction, goal_digest
from core.candidate_broker import Broker
from core.candidate_bridge import Request
from core.task_store import TaskStore
from models.provider import ProviderRegistry
from tools import ToolDenied
from test_agent import call, final
from test_brain import ScriptedProvider
from test_candidate_bridge import request
from test_self_edit import setup, tree_bytes
from test_engineering import ARGS, model, run_checks

EXACT = ('Create a small documentation-only engineering improvement candidate for Jarvis using your engineering workflow. '
         'Use Codex for implementation, run tests and review, and stop before apply or deployment.')


def bound(raw, scope='code'):
    return EngineeringAction(provider='codex', scope=scope, task_id=raw['task_id'],
        conversation_id=raw['conversation_id'], goal_digest=goal_digest(raw['goal']))


@pytest.mark.parametrize('goal', [EXACT, 'Please prepare a Jarvis candidate that improves the docs.',
                                'I would like a clearer explanation in the Jarvis documentation. Implement it in a candidate.'])
def test_typed_action_does_not_reparse_wording(setup, goal):
    source, _, factory = setup
    before = tree_bytes(source)
    raw = request('engineer', goal, ARGS)
    raw['engineering_action'] = bound(raw).model_dump()
    broker = Broker(factory(model=model, tester=run_checks), source=source)
    record = broker.execute(raw)
    assert record['state'] == 'passed'
    assert record['improvement']['engineering_action'] == 'engineer'
    assert record['improvement']['implementation_provider'] == 'codex'
    assert broker.execute(raw) == record
    assert tree_bytes(source) == before
    assert not factory().store.get(record['id']).get('apply')


@pytest.mark.parametrize('change', ['goal', 'task_id', 'conversation_id', 'action', 'provider', 'workspace', 'stop_after'])
def test_action_binding_and_closed_authority(change):
    raw = request('engineer', EXACT, ARGS)
    raw['engineering_action'] = bound(raw).model_dump()
    if change in {'task_id', 'conversation_id'}:
        raw[change] = str(uuid4())
    elif change == 'goal':
        raw['goal'] = 'A different request'
    elif change == 'action':
        raw['action'] = 'deploy'
        raw['arguments'] = {'candidate_id': str(uuid4())}
    else:
        raw['engineering_action'][change] = {'provider': '../unknown', 'workspace': '/tmp', 'stop_after': 'deploy'}[change]
    with pytest.raises((ToolDenied, ValueError)):
        Request.model_validate(raw).checked()


def test_owner_preparation_needs_no_classifier(setup):
    from core.candidate_tools import CandidateTools
    from core.engineering_authority import OwnerDevAuthority
    source, db, factory = setup
    raw = request('workspace', 'Please investigate Jarvis and do what is needed.', {})
    context = {k: raw[k] for k in ('task_id', 'conversation_id')}
    tools = CandidateTools(raw['goal'], db, lambda db: factory(),
        authority=OwnerDevAuthority(), source_context=context)
    assert tools.prepare('codex').provider == 'codex'
    assert tools.engineering_action.workspace == 'configured_dev'
    unnamed = CandidateTools('Please investigate this and do what is needed.', db, lambda db: factory(),
        authority=OwnerDevAuthority(), source_context=context)
    with pytest.raises(ToolDenied) as denied:
        unnamed.prepare('codex')
    assert denied.value.reason == 'unknown_project'


def test_documentation_scope_rejects_code_from_codex(setup):
    source, _, factory = setup
    raw = request('engineer', EXACT, ARGS)
    raw['engineering_action'] = bound(raw, 'documentation').model_dump()
    before = tree_bytes(source)
    result = Broker(factory(model=model, tester=run_checks), source=source).execute(raw)
    assert result['state'] == 'rejected'
    assert result['test_results'] == []
    assert tree_bytes(source) == before


def test_workspace_catalog_is_read_only_scoped_and_requires_action(setup):
    source, database, factory = setup
    before = tree_bytes(source)
    broker = Broker(factory(), source=source)
    raw = request('workspace', EXACT, {})
    assert 'src/app.py' in broker.execute(raw)['editable_paths']
    raw['engineering_action'] = bound(raw, 'documentation').model_dump()
    result = broker.execute(raw)
    assert result['editable_paths'] == ['docs/readme.md']
    assert result['test_paths'] == ['tests/test_app.py']
    assert tree_bytes(source) == before
    with database.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0


def test_bad_path_does_not_consume_prepared_candidate_authority(setup):
    from core.candidate_tools import CandidateTools
    source, database, factory = setup
    raw = request('engineer', 'Improve Jarvis documentation', {})
    action = bound(raw, 'documentation')
    tools = CandidateTools(raw['goal'], database, lambda db: factory(),
        engineering_authorizer=lambda provider: action,
        source_context={'task_id': raw['task_id'], 'conversation_id': raw['conversation_id']})
    workspace = tools.prepare('codex')
    assert workspace.editable_paths == ['docs/readme.md']
    with pytest.raises(ToolDenied):
        tools.engineer(**dict(ARGS, inspect_paths=['README.md']))
    assert tools.created is False


def test_preparation_alone_cannot_finish_engineering_at_budget_limit(setup):
    _, database, factory = setup
    store = TaskStore(database.path)
    task = store.create_task(EXACT)
    provider = ScriptedProvider(call('prepare_engineering', {'provider': 'codex'}),
        final('Finished'), final('Finished'))
    brain = JarvisBrain(AgentRunner(max_iterations=3, candidate_service_factory=lambda db: factory()),
        settings=BrainSettings(True, 'local'), providers=ProviderRegistry([provider]))
    outcome = brain.run(task['goal'], task_id=task['id'], task_store=store)
    assert outcome.status == 'limit_reached'
    assert outcome.iterations == 3
    with database.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0
