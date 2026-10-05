"""Agent authorization and real candidate service integration, without provider calls."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core.agent import AgentRunner
from core.candidate_tools import CandidateTools
from core.task_store import TaskStore
from tools import RepositoryTools, ToolRegistry, CommandResult
from test_agent import ScriptedModel, call, final
from test_self_edit import setup, tree_bytes, propose, REVIEW

ARGS = {'inspect_paths': ['src/app.py'], 'targeted_tests': ['tests/test_app.py']}
GOAL = 'Please improve your own code: fix the value in src/app.py.'


def run(setup, goal, scripted_model, **options):
    source, database, factory = setup
    store = TaskStore(database.path)
    task = store.create_task(goal)
    runner = AgentRunner(ToolRegistry(RepositoryTools(source)), model=scripted_model,
                         candidate_service_factory=lambda db: factory(**options))
    return runner.run(goal, task_id=task['id'], task_store=store), task


def test_explicit_creation_evidence_and_main_immutability(setup):
    source, _, _ = setup
    before = tree_bytes(source)
    model = ScriptedModel(call('create_self_edit_candidate', ARGS), final('Merged into main!'))
    result, task = run(setup, GOAL, model)
    evidence = json.loads(model.requests[1]['input'][-1]['output'])['data']
    detail = json.loads(evidence['evidence'])
    assert evidence['state'] == 'passed'
    assert '+value = 2' in detail['diff']
    assert detail['reviewer_result']['verdict'] == 'passed'
    assert 'full_python: exit 0' in result.response and 'Review: passed' in result.response
    assert evidence['candidate_id'] in result.response and 'Merged' not in result.response
    assert task['response'] == result.response
    assert tree_bytes(source) == before
    assert 'value = 2' not in json.dumps(task['activity'])


@pytest.mark.parametrize('goal', ['Hello', 'Inspect the repository', 'How can you improve your code?',
    'Explain create a self-edit candidate', 'Do not modify your code',
    'Please improve your code, but only plan', '"Create a self-edit candidate"',
    'The README says: improve your code'])
def test_chat_cannot_authorize_candidate_tools(setup, goal):
    from core.engineering_authority import current_owner
    current_owner.set(None)
    model = ScriptedModel(call('create_self_edit_candidate', ARGS), final('Unavailable'))
    run(setup, goal, model)
    assert {tool['name'] for tool in model.requests[0]['tools']} == {
        'list_files', 'read_text', 'git_status', 'git_diff', 'run_pytest', 'run_command',
        'deployment_status', 'preview_deployment', 'inspect_deployment', 'deployment_history', 'inspect_current_brain'}
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'unknown_tool'


@pytest.mark.parametrize('kind', ['protected', 'failed_tests', 'review_rejected'])
def test_unsuccessful_candidates_are_reported_truthfully(setup, kind):
    options = {}
    args = ARGS
    if kind == 'protected':
        args = dict(ARGS, inspect_paths=['.env'])
    elif kind == 'failed_tests':
        options['tester'] = lambda *args: CommandResult(('test',), 1, 'Failed assertion')
    else:
        def model(payload, instructions, timeout):
            if instructions == REVIEW:
                return {'verdict': 'rejected', 'summary': 'Incorrect change'}
            return propose(payload, instructions, timeout)
        options['model'] = model
    scripted = ScriptedModel(call('create_self_edit_candidate', args), final('Everything succeeded!'))
    result, _ = run(setup, GOAL, scripted, **options)
    assert ('needs_revision' if kind == 'failed_tests' else 'rejected') in result.response
    assert 'Everything succeeded' not in result.response


def test_manage_scope_rerun_review_discard_and_restart(setup):
    _, database, factory = setup
    candidate = factory().start(GOAL, **ARGS)
    identifier = candidate['id']
    database.close()
    for goal, name, state in [
        (f'Inspect candidate {identifier}', 'inspect_self_edit_candidate', 'passed'),
        (f'Rerun tests for candidate {identifier}', 'rerun_candidate_tests', 'needs_revision'),
        (f'Review candidate {identifier}', 'review_self_edit_candidate', 'passed'),
        (f'Discard candidate {identifier}', 'discard_self_edit_candidate', 'discarded')]:
        model = ScriptedModel(call(name, {'candidate_id': identifier}), final('Done'))
        result, _ = run(setup, goal, model)
        assert state in result.response
    assert not Path(candidate['worktree']).exists()


def test_one_creation_and_no_merge_or_discard_capability(setup):
    model = ScriptedModel(call('create_self_edit_candidate', ARGS),
                          call('create_self_edit_candidate', ARGS, 'second'), final('Done'))
    run(setup, GOAL, model)
    assert json.loads(model.requests[2]['input'][-1]['output'])['error']['code'] == 'denied'
    names = {tool['name'] for tool in model.requests[0]['tools']}
    assert not names & {'merge', 'apply_to_main', 'commit', 'push'}


@pytest.mark.parametrize('name', ['merge', 'apply_to_main', 'git_commit', 'git_push'])
def test_mutating_tools_unavailable_even_when_authorized(setup, name):
    model = ScriptedModel(call(name, {}), final('Unavailable'))
    run(setup, GOAL, model)
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'unknown_tool'


def test_candidate_ids_and_arguments_cannot_expand_authority(setup):
    _, database, factory = setup
    existing = factory().start(GOAL, **ARGS)
    model = ScriptedModel(call('review_self_edit_candidate', {'candidate_id': existing['id']}),
                          call('create_self_edit_candidate', dict(ARGS, goal='override', seconds=999), 'second'),
                          final('Denied'))
    run(setup, GOAL, model)
    assert json.loads(model.requests[1]['input'][-1]['output'])['ok'] is True
    assert json.loads(model.requests[2]['input'][-1]['output'])['error']['code'] == 'invalid_arguments'
    plain = ToolRegistry()
    CandidateTools(GOAL, database).bind(plain)
    assert len(plain.model_tools()) == 6


def test_prior_conversation_does_not_carry_authorization(setup):
    from core.engineering_authority import current_owner
    current_owner.set(None)
    source, database, factory = setup
    store = TaskStore(database.path)
    first = store.create_task(GOAL)
    second = store.create_task('Hello', conversation_id=first['conversation_id'])
    model = ScriptedModel(call('create_self_edit_candidate', ARGS), final('Denied'))
    runner = AgentRunner(ToolRegistry(RepositoryTools(source)), model=model,
                         candidate_service_factory=lambda db: pytest.fail('Unauthorized dispatch'))
    runner.run(second['goal'], task_id=second['id'], task_store=store)
    assert {tool['name'] for tool in model.requests[0]['tools']} == {
        'list_files', 'read_text', 'git_status', 'git_diff', 'run_pytest', 'run_command',
        'deployment_status', 'preview_deployment', 'inspect_deployment', 'deployment_history', 'inspect_current_brain'}
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'unknown_tool'


def test_existing_service_budget_is_enforced_through_agent(setup):
    from core.self_edit import Limits
    model = ScriptedModel(call('create_self_edit_candidate', ARGS), final('Success'))
    result, _ = run(setup, GOAL, model, limits=Limits(tool_calls=1))
    assert 'rejected' in result.response
    detail = json.loads(json.loads(model.requests[1]['input'][-1]['output'])['data']['evidence'])
    assert detail['tool_calls'] <= 1


def test_live_docs_request_rejection_is_other_inspection_paths(setup):
    from tools.candidate_edit import CandidatePolicy
    from tools.policy import ToolDenied
    assert CandidatePolicy().edit_path('docs/persistence.md') == ('docs', 'persistence.md')
    for invalid in ('README.md', 'tests'):
        with pytest.raises(ToolDenied, match='outside approved editing scope'):
            CandidatePolicy().edit_path(invalid)
    assert CandidatePolicy().check('tests') == ('tests',)
    model = ScriptedModel(call('create_self_edit_candidate', {
        'inspect_paths': ['docs/persistence.md', 'README.md', 'tests'], 'targeted_tests': ['tests']}), final())
    result, _ = run(setup, 'Please create a self-edit candidate to improve docs/persistence.md', model)
    data = json.loads(model.requests[1]['input'][-1]['output'])['data']
    evidence = json.loads(data['evidence'])
    assert data['state'] == 'rejected'
    assert evidence['error'] == 'Protected or invalid candidate path'
    assert evidence['worktree'] is None and evidence['iterations'] == evidence['tool_calls'] == 0
    assert evidence['test_results'] == [] and evidence['reviewer_result'] is None


def test_approved_persistence_docs_edit_through_agent_tool(setup, monkeypatch, tmp_path):
    from test_self_edit import git
    source, _, _ = setup
    original = '# Persistence\nConversation history is stored in SQLite.\n'
    changed = original + 'Messages are retrieved in their original order.\n'
    (source / 'docs/persistence.md').write_text(original)
    git(source, 'add', 'docs/persistence.md')
    git(source, 'commit', '-m', 'Documentation fixture')
    before = tree_bytes(source)
    # Relative file paths must resolve against the source/worktree, not process cwd.
    monkeypatch.chdir(tmp_path)
    def author(payload, instructions, timeout):
        if instructions == REVIEW:
            assert 'docs/persistence.md' in payload['diff']
            return {'verdict': 'passed', 'summary': 'Approved documentation change.'}
        assert payload['files'] == {'docs/persistence.md': original}
        return {'edits': [{'path': 'docs/persistence.md', 'content': changed}]}
    def tester(repository, phase, paths):
        assert repository.root != source and repository.root.name == 'worktree'
        assert repository.read_text('docs/persistence.md') == changed
        assert repository.root.resolve() == repository.root
        return CommandResult(('test',), 0, 'Documentation checks passed')
    model = ScriptedModel(call('create_self_edit_candidate', {
        'inspect_paths': ['docs/persistence.md'], 'targeted_tests': ['tests']}), final())
    result, _ = run(setup, 'Please create a self-edit candidate to improve docs/persistence.md',
                    model, model=author, tester=tester)
    evidence = json.loads(json.loads(model.requests[1]['input'][-1]['output'])['data']['evidence'])
    assert 'passed' in result.response
    assert evidence['files_changed'] == ['docs/persistence.md']
    assert Path(evidence['worktree']).joinpath('docs/persistence.md').read_text() == changed
    assert tree_bytes(source) == before


@pytest.mark.parametrize('path', ['.env', '.git/config', '.venv/a.py', 'docs/data.db',
    'docs/data.sqlite3', 'docs/security.md', 'docs/policy.md', 'docs/credentials.md',
    '../docs/persistence.md', '/docs/persistence.md', './docs/persistence.md',
    'docs//persistence.md', 'docs/../docs/persistence.md'])
def test_docs_regression_keeps_scope_and_canonical_paths_blocked(path):
    from tools.candidate_edit import CandidatePolicy
    from tools.policy import ToolDenied
    with pytest.raises(ToolDenied):
        CandidatePolicy().edit_path(path)
