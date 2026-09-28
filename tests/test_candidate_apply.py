"""Human UUID approval, exact patch application, verification and recovery."""
import json
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

import main
from core.candidate_apply import CandidateApply, approved_id
from core.candidate_tools import CandidateTools
from core.database import Database
from core.self_edit import SelfEditService
from core.task_store import TaskStore
from tools import ToolRegistry, CommandResult, ToolDenied
from test_self_edit import setup, start, git, tree_bytes, require_live_sandbox


def apply_service(factory, tester=None):
    # Existing candidate fixture tester asserts candidate-only root. This adapter
    # separately asserts post-apply verification sees the approved main bytes.
    def verify(repository, phase, paths):
        assert (repository.root / 'src/app.py').read_text() == 'value = 2\n'
        return CommandResult(('test',), 0, '1 passed')
    return CandidateApply(factory(tester=tester or verify))


def confirmation(identifier):
    return f'Approve and apply candidate {identifier} to main'


def test_success_and_idempotency_restart(setup):
    source, database, factory = setup
    candidate = start(factory())
    service = apply_service(factory)
    assert service.preview(candidate['id'])['eligible']
    result = service.apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'applied', result
    assert result['files_changed'] == ['src/app.py']
    assert result['post_apply_verified'] and result['completed_at']
    assert [t['phase'] for t in result['verification']] == ['targeted', 'full_python']
    assert git(source, 'rev-parse', 'HEAD').strip() == result['commit']
    assert git(source, 'status', '--porcelain') == ''
    assert candidate['id'] in git(source, 'log', '-1', '--format=%s')
    assert git(source, 'rev-parse', 'HEAD^').strip() == candidate['base_commit']
    database.close()
    assert apply_service(factory).inspect(candidate['id'])['apply'] == result
    assert apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))['apply'] == result


@pytest.mark.parametrize('mask', [0o002, 0o022, 0o077])
@pytest.mark.parametrize('executable', [False, True])
def test_apply_compares_git_modes_independently_of_umask(setup, mask, executable):
    source, _, factory = setup
    if executable:
        (source / 'src/app.py').chmod(0o775)
        git(source, 'add', 'src/app.py')
        git(source, 'commit', '-m', 'Executable fixture')
    previous = os.umask(mask)
    try:
        candidate = start(factory())
        result = apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))['apply']
    finally:
        os.umask(previous)
    assert result['status'] == 'applied', result
    assert git(source, 'ls-files', '--stage', 'src/app.py').split()[0] == ('100755' if executable else '100644')


def test_staged_executable_change_still_rolls_back(setup):
    source, _, factory = setup
    candidate = start(factory())
    apply = apply_service(factory)
    verify = apply._verify_patch_contents
    def change_index_mode():
        git(source, 'update-index', '--chmod=+x', 'src/app.py')
        with pytest.raises(ToolDenied, match='Staged mode differs'):
            verify()
        # Restore the injected concurrent index change so rollback can safely
        # reverse the reviewed patch, then propagate the validation failure.
        git(source, 'update-index', '--chmod=-x', 'src/app.py')
        raise ToolDenied('Staged mode differs from reviewed candidate')
    apply._verify_patch_contents = change_index_mode
    result = apply.apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'rolled_back'
    assert result['verification'] == []
    assert git(source, 'status', '--porcelain') == ''
    assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']


@pytest.mark.parametrize('approval_text', ['continue', 'looks good', 'approve', 'Apply it',
    'Do not approve and apply candidate {id} to main', 'Approve and apply candidate ' + str(uuid4()) + ' to main'])
def test_explicit_current_uuid_required(setup, approval_text):
    source, _, factory = setup
    candidate = start(factory())
    before = tree_bytes(source)
    with pytest.raises(ToolDenied):
        apply_service(factory).apply(candidate['id'], approval_text.format(id=candidate['id']))
    assert tree_bytes(source) == before
    assert apply_service(factory).inspect(candidate['id'])['apply'] is None


@pytest.mark.parametrize('state', ['rejected', 'needs_revision', 'discarded', 'failed', 'expired'])
def test_nonpassed_never_applies(setup, state):
    source, _, factory = setup
    service = factory()
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    record['state'] = state
    service.store.save(record)
    result = apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))
    assert result['apply']['status'] == 'denied'
    assert (source / 'src/app.py').read_text() == 'value = 1\n'


@pytest.mark.parametrize('kind', ['dirty', 'drift', 'protected', 'tampered', 'expired', 'missing_tests', 'diff'])
def test_preconditions_fail_closed(setup, kind):
    source, _, factory = setup
    service = factory()
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    if kind == 'dirty':
        (source / 'docs/readme.md').write_text('User changes\n')
    elif kind == 'drift':
        git(source, 'commit', '--allow-empty', '-m', 'Another commit')
    elif kind == 'protected':
        (Path(candidate['worktree']) / '.env').write_text('blocked')
    elif kind == 'tampered':
        (Path(candidate['worktree']) / 'src/app.py').write_text('value = 3\n')
    elif kind == 'expired':
        record['reviewed_at'] = '2000-01-01T00:00:00'
    elif kind == 'missing_tests':
        record['test_results'] = []
    else:
        record['diff'] += '\nforged patch\n'
    service.store.save(record)
    before = {p: v for p, v in tree_bytes(source).items() if not p.startswith('.git/')}
    result = apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))
    assert result['apply']['status'] == 'denied', result
    assert {p: v for p, v in tree_bytes(source).items() if not p.startswith('.git/')} == before


@pytest.mark.parametrize('phase', ['targeted', 'full_python', 'full_ui'])
def test_failed_verification_rolls_back(setup, phase):
    source, database, factory = setup
    (source / 'tests/ui.test.cjs').write_text("require('node:test')('ok', () => {});\n")
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'UI fixture')
    candidate = start(factory())
    def tester(repository, current, paths):
        return CommandResult(('test',), int(current == phase), 'check')
    result = apply_service(factory, tester).apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'rolled_back', result
    assert result['rollback'] and not result['post_apply_verified']
    assert (source / 'src/app.py').read_text() == 'value = 1\n'
    assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']
    assert git(source, 'status', '--porcelain') == ''
    database.close()
    assert apply_service(factory).inspect(candidate['id'])['apply'] == result


def test_http_approval_preview_and_audit(setup):
    _, database, factory = setup
    candidate = start(factory())
    application = main.create_app(database.path)
    application.dependency_overrides[main.get_self_edit] = lambda: apply_service(factory).service
    url = '/candidates/' + candidate['id'] + '/apply'
    with TestClient(application) as client:
        assert client.get(url + '/preview').json()['eligible']
        assert client.post(url, json={'confirmation': 'looks good'}).status_code == 409
        assert client.post(url, json={'confirmation': confirmation(candidate['id']), 'force': True}).status_code == 422
        result = client.post(url, json={'confirmation': confirmation(candidate['id'])}).json()
        assert result['apply']['status'] == 'applied', result
        assert client.get(url).json() == result
        assert client.get('/candidates/invalid/apply').status_code == 404


def test_agent_approval_not_inferred_and_wrong_id_blocked(setup):
    _, database, factory = setup
    candidate = start(factory())
    store = TaskStore(database.path)
    for goal in ('looks good', 'continue', 'Please improve your code'):
        tools = CandidateTools(goal, database).bind(ToolRegistry()).model_tools()
        assert 'approve_and_apply_candidate' not in {t['name'] for t in tools}
    goal = confirmation(candidate['id'])
    task = store.create_task(goal)
    adapter = CandidateTools(goal, database, lambda db: apply_service(factory).service)
    registry = adapter.bind(ToolRegistry())
    bad = registry.execute('approve_and_apply_candidate', json.dumps({'candidate_id': str(uuid4())}),
                           task_id=task['id'], task_store=store)
    assert bad.error.code == 'denied'
    good = registry.execute('approve_and_apply_candidate', json.dumps({'candidate_id': candidate['id']}),
                            task_id=task['id'], task_store=store)
    assert good.data.state == 'applied', good
    assert 'apply applied' in adapter.final_response('Never applied')


def test_interrupted_apply_recovers_on_audit_read(setup):
    source, database, factory = setup
    candidate = start(factory())
    def crash(*args):
        raise SystemExit('Simulated process interruption')
    with pytest.raises(SystemExit):
        apply_service(factory, crash).apply(candidate['id'], confirmation(candidate['id']))
    assert (source / 'src/app.py').read_text() == 'value = 2\n'
    database.close()
    recovered = apply_service(factory).inspect(candidate['id'])['apply']
    assert recovered['status'] == 'rolled_back' and recovered['rollback']
    assert git(source, 'status', '--porcelain') == ''
    assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']


def test_unrelated_staged_changes_cannot_be_committed(setup):
    source, _, factory = setup
    candidate = start(factory())
    service = apply_service(factory)
    original = service._verify_patch_contents
    def race():
        (source / 'docs/readme.md').write_text('Concurrent user edit\n')
        git(source, 'add', 'docs/readme.md')
        original()
    service._verify_patch_contents = race
    result = service.apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'rollback_failed'  # Preserve unrelated human edits.
    assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']
    assert (source / 'docs/readme.md').read_text() == 'Concurrent user edit\n'
    assert (source / 'src/app.py').read_text() == 'value = 1\n'


@pytest.mark.parametrize('kind', ['symlink', 'budget', 'review'])
def test_additional_apply_gates(setup, tmp_path, kind):
    source, _, factory = setup
    candidate = start(factory())
    service = factory()
    record = service.store.get(candidate['id'], internal=True)
    if kind == 'symlink':
        target = Path(candidate['worktree']) / 'src/app.py'
        target.unlink()
        target.symlink_to(source / 'src/app.py')
    elif kind == 'budget':
        record['elapsed_seconds'] = 300
    else:
        record['reviewed_diff_hash'] = 'missing'
    service.store.save(record)
    result = apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'denied'
    assert (source / 'src/app.py').read_text() == 'value = 1\n'


def test_real_post_apply_sandbox_python_and_ui(setup):
    source, _, factory = setup
    require_live_sandbox(source)
    (source / 'tests/ui.test.cjs').write_text("require('node:test')('ok', () => {});\n")
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'UI fixture')
    candidate = start(factory())
    result = CandidateApply(factory(tester=None)).apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'applied', result
    assert [t['phase'] for t in result['verification']] == ['targeted', 'full_python', 'full_ui']
    assert all(t['returncode'] == 0 for t in result['verification'])


def test_previous_turn_approval_does_not_authorize_current_chat(setup):
    from core.agent import AgentRunner
    from test_agent import ScriptedModel, call, final
    _, database, factory = setup
    candidate = start(factory())
    store = TaskStore(database.path)
    first = store.create_task(confirmation(candidate['id']))
    current = store.create_task('looks good', conversation_id=first['conversation_id'])
    model = ScriptedModel(call('approve_and_apply_candidate', {'candidate_id': candidate['id']}), final('Denied'))
    runner = AgentRunner(model=model, candidate_service_factory=lambda db: pytest.fail('No approval'))
    runner.run(current['goal'], task_id=current['id'], task_store=store)
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'unknown_tool'


@pytest.mark.parametrize('content', ['New docs\n', ''])
def test_reviewed_new_file_patch_applies_exactly(setup, content):
    from test_self_edit import REVIEW
    source, _, factory = setup
    def model(payload, instructions, timeout):
        if instructions == REVIEW:
            return {'verdict': 'passed', 'summary': 'Docs are correct.'}
        return {'edits': [{'path': 'docs/new.md', 'content': content}]}
    service = factory(model=model, tester=lambda *args: CommandResult(('test',), 0, 'passed'))
    candidate = start(service)
    assert candidate['state'] == 'passed'
    result = CandidateApply(service).apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'applied', result
    assert (source / 'docs/new.md').read_text() == content
    assert git(source, 'status', '--porcelain') == ''


def test_inspecting_applied_candidate_reports_applied_audit(setup):
    _, database, factory = setup
    candidate = start(factory())
    result = apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))
    adapter = CandidateTools(f"Inspect candidate {candidate['id']}", database, lambda db: factory())
    adapter.action('inspect', candidate['id'])
    response = adapter.final_response('Nothing changed')
    assert 'apply applied' in response and result['apply']['commit'] in response
    assert 'Main was not modified' not in response


def test_budget_exhaustion_after_apply_rolls_back(setup):
    source, _, factory = setup
    candidate = start(factory())
    service = apply_service(factory)
    def exhausted(*args):
        service.record['tool_calls'] = service.service.limits.tool_calls
        return CommandResult(('test',), 0, 'passed')
    service.service.tester = exhausted
    result = service.apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'rolled_back' and result['rollback']
    assert git(source, 'status', '--porcelain') == ''
    assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']


def test_apply_serializes_main_and_recovery_source_is_bound(setup, tmp_path):
    source, database, factory = setup
    candidate = start(factory())
    first, second = apply_service(factory), apply_service(factory)
    with first._lock():
        with pytest.raises(ToolDenied, match='Another apply'):
            second.apply(candidate['id'], confirmation(candidate['id']))
    other = tmp_path / 'other'
    other.mkdir()
    git(other, 'init')
    foreign = CandidateApply(SelfEditService(database, source_root=other))
    with pytest.raises(ToolDenied, match='original source'):
        foreign.inspect(candidate['id'])
    assert (source / 'src/app.py').read_text() == 'value = 1\n'


def agent_apply_request(setup, goal, name, identifier, *, tester=None):
    from test_agent import ScriptedModel, call, final
    from test_candidate_tools import run
    model = ScriptedModel(call(name, {'candidate_id': identifier}), final('Applied successfully!'))
    result, _ = run(setup, goal, model, tester=tester or (lambda *args: CommandResult(('test',), 0, 'passed')))
    output = json.loads(model.requests[1]['input'][-1]['output'])
    names = {t['name'] for t in model.requests[0]['tools']}
    return result, output, names


def test_agent_named_preview_available_without_approval_and_main_unchanged(setup):
    source, _, factory = setup
    candidate = start(factory())
    identifier = candidate['id']
    before = tree_bytes(source)
    result, output, names = agent_apply_request(setup,
        f'Show the apply eligibility preview for candidate {identifier}. Do not apply it.',
        'preview_candidate_apply', identifier)
    assert output['ok'] and output['data']['state'] == 'eligible', output
    assert 'preview_candidate_apply' in names
    assert 'approve_and_apply_candidate' not in names
    assert 'apply eligible' in result.response and 'Preview only' in result.response
    # Preview retains the service's shared lock bookkeeping; all source, index,
    # refs and object bytes remain untouched.
    after = tree_bytes(source)
    assert after.pop('.git/jarvis-apply.lock') == hashlib.sha256(b'').hexdigest()
    assert after == before
    assert factory().store.get(identifier).get('apply') is None


@pytest.mark.parametrize('status', ['absent', 'applied', 'interrupted'])
def test_agent_apply_audit_is_read_only_even_with_expired_lease(setup, status):
    source, database, factory = setup
    candidate = start(factory())
    identifier = candidate['id']
    if status == 'applied':
        apply_service(factory).apply(identifier, confirmation(identifier))
    elif status == 'interrupted':
        def crash(*args):
            raise SystemExit('Simulated interruption')
        with pytest.raises(SystemExit):
            apply_service(factory, crash).apply(identifier, confirmation(identifier))
        service = factory()
        service.store.claim(identifier)
        record = service.store.get(identifier, internal=True)
        record['lease_until'] = 0
        service.store.save(record)
    before = tree_bytes(source)
    def saved():
        with database.transaction() as db:
            return tuple(db.execute('SELECT record,busy FROM candidates WHERE id=?', (identifier,)).fetchone())
    stored = saved()
    result, output, names = agent_apply_request(setup,
        f'Inspect apply audit candidate {identifier}. Do not apply it.',
        'inspect_candidate_apply', identifier)
    assert output['ok'], output
    assert 'inspect_candidate_apply' in names and 'approve_and_apply_candidate' not in names
    assert output['data']['state'] == {'absent': 'not_applied', 'applied': 'applied', 'interrupted': 'verifying'}[status]
    assert identifier in result.response
    assert tree_bytes(source) == before
    assert saved() == stored  # No lease cleanup, budget/event writes or recovery.


def test_preview_does_not_recover_interrupted_apply(setup):
    source, _, factory = setup
    candidate = start(factory())
    def crash(*args):
        raise SystemExit('Simulated interruption')
    with pytest.raises(SystemExit):
        apply_service(factory, crash).apply(candidate['id'], confirmation(candidate['id']))
    before = tree_bytes(source)
    result = apply_service(factory).preview(candidate['id'])
    assert not result['eligible']
    assert tree_bytes(source) == before
    # The existing explicit backend recovery path remains functional.
    assert apply_service(factory).inspect(candidate['id'])['apply']['status'] == 'rolled_back'


def test_agent_exact_approval_applies_and_duplicate_is_idempotent(setup):
    source, _, factory = setup
    candidate = start(factory())
    identifier = candidate['id']
    result, output, names = agent_apply_request(setup, confirmation(identifier),
        'approve_and_apply_candidate', identifier)
    assert 'approve_and_apply_candidate' in names
    assert output['ok'] and output['data']['state'] == 'applied', output
    assert 'apply applied' in result.response
    before = tree_bytes(source)
    _, duplicate, _ = agent_apply_request(setup, confirmation(identifier),
        'approve_and_apply_candidate', identifier)
    assert duplicate['data'] == output['data']
    assert tree_bytes(source) == before


@pytest.mark.parametrize('goal', ['continue', 'looks good', 'go ahead',
    'Approve and apply candidate to main',
    'Approve and apply candidate invalid to main',
    'Show the apply eligibility preview for candidate {id}. Do not apply it.'])
def test_agent_unapproved_apply_not_exposed_or_invoked(setup, goal):
    source, _, factory = setup
    candidate = start(factory())
    before = tree_bytes(source)
    _, output, names = agent_apply_request(setup, goal.format(id=candidate['id']),
        'approve_and_apply_candidate', candidate['id'])
    assert 'approve_and_apply_candidate' not in names
    assert output['error']['code'] == 'unknown_tool'
    assert tree_bytes(source) == before


@pytest.mark.parametrize('name,goal', [
    ('preview_candidate_apply', 'Show the apply eligibility preview for candidate {id}'),
    ('inspect_candidate_apply', 'Inspect apply audit candidate {id}'),
    ('approve_and_apply_candidate', 'Approve and apply candidate {id} to main')])
def test_agent_apply_tools_wrong_uuid_fail_closed(setup, name, goal):
    source, _, factory = setup
    candidate = start(factory())
    before = tree_bytes(source)
    _, output, _ = agent_apply_request(setup, goal.format(id=str(uuid4())), name, candidate['id'])
    assert output['error']['code'] == 'denied'
    assert tree_bytes(source) == before


def test_agent_apply_failure_preserves_rollback(setup):
    source, _, factory = setup
    candidate = start(factory())
    _, output, _ = agent_apply_request(setup, confirmation(candidate['id']),
        'approve_and_apply_candidate', candidate['id'],
        tester=lambda *args: CommandResult(('test',), 1, 'failed'))
    assert output['data']['state'] == 'rolled_back', output
    assert (source / 'src/app.py').read_text() == 'value = 1\n'
    assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']
    assert git(source, 'status', '--porcelain') == ''


@pytest.mark.parametrize('arguments', [{}, {'candidate_id': 'invalid'},
    {'candidate_id': None}, {'candidate_id': '00000000-0000-0000-0000-000000000000', 'confirmation': 'approved'}])
@pytest.mark.parametrize('name', ['preview_candidate_apply', 'inspect_candidate_apply', 'approve_and_apply_candidate'])
def test_agent_apply_tools_missing_or_extra_identity_arguments_fail_closed(setup, arguments, name):
    _, database, factory = setup
    candidate = start(factory())
    store = TaskStore(database.path)
    goal = confirmation(candidate['id'])
    task = store.create_task(goal)
    adapter = CandidateTools(goal, database, lambda db: pytest.fail('Invalid arguments must not dispatch'))
    result = adapter.bind(ToolRegistry()).execute(name, json.dumps(arguments),
        task_id=task['id'], task_store=store)
    assert result.error.code == 'invalid_arguments'
