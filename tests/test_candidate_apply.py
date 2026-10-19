"""Human UUID approval, exact patch application, verification and recovery."""
import json
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

pytestmark = pytest.mark.usefixtures("owner")

import main
from core.candidate_apply import CandidateApply, approved_id
from core.candidate_tools import CandidateTools
from core.database import Database
from core.self_edit import SelfEditService
from core.task_store import TaskStore
from tools import ToolRegistry, CommandResult, ToolDenied
from test_self_edit import setup as source_setup, start, git, tree_bytes, require_live_sandbox


@pytest.fixture
def setup(tmp_path):
    # General apply integrity checks use a writable external project. Jarvis
    # integration separation is covered explicitly below.
    source, database, factory = source_setup.__wrapped__(tmp_path)
    from core.engineering_portfolio import Portfolio
    portfolio = Portfolio(database)
    jarvis = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    portfolio.register_project(jarvis | dict(repository=None), replace=True)
    portfolio.register_project(jarvis | dict(project_id='fixture', name='Fixture', repository=str(source)))
    return source, database, factory


def integration_target(setup, tmp_path):
    from core.engineering_portfolio import Portfolio
    source, database, _ = setup
    target = tmp_path / 'integration'
    git(tmp_path, 'clone', '--no-hardlinks', '--no-local', str(source), str(target))
    git(target, 'checkout', '-b', 'jarvis/integration')
    portfolio = Portfolio(database)
    project = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    portfolio.register_project(project | dict(integration_repository=str(target),
        integration_branch='jarvis/integration'), replace=True)
    return target


def apply_service(factory, tester=None):
    # Existing candidate fixture tester asserts candidate-only root. This adapter
    # separately asserts post-apply verification sees the approved main bytes.
    def verify(repository, phase, paths):
        assert (repository.root / 'src/app.py').read_text() == 'value = 2\n'
        return CommandResult(('test',), 0, '1 passed')
    return CandidateApply(factory(tester=tester or verify))


def confirmation(identifier):
    return f'Approve and apply candidate {identifier} to main'


def authority_candidate(factory):
    service = factory()
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    record['goal'] = 'Increase Jarvis authority'
    service.store.save(record)
    return candidate


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
    candidate = authority_candidate(factory)
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
    candidate = authority_candidate(factory)
    from core.deployment import Settings
    token = 'candidate-apply-owner-' * 3
    application = main.create_app(settings=Settings(mode='development', database_path=database.path,
        state_dir=Path(database.path).parent, auth_token=token))
    application.dependency_overrides[main.get_self_edit] = lambda: apply_service(factory).service
    url = '/candidates/' + candidate['id'] + '/apply'
    with TestClient(application, headers={'Authorization': 'Bearer ' + token}) as client:
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
        assert 'apply_candidate' in {t['name'] for t in tools}
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


# tests/test_self_edit.py and tests/test_candidate_apply.py are in release shards 1 and 2;
# the fixture's own tests/test_app.py is in none, so it runs in a final extra shard.
SHARD_PATHS = [['tests/test_self_edit.py'], ['tests/test_candidate_apply.py'], ['tests/test_app.py']]


def sharded_candidate(setup):
    source, _, factory = setup
    for paths, name in zip(SHARD_PATHS[:2], 'ab'):
        (source / paths[0]).write_text(f'def test_{name}(): pass\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Tests in two release shards')
    return start(factory())


def test_post_apply_full_python_runs_release_shards_within_their_budget(setup, monkeypatch):
    import time
    source, database, factory = setup
    candidate = sharded_candidate(setup)
    monkeypatch.setenv('JARVIS_PREPARE_SHARD_SECONDS', '240')
    monkeypatch.setenv('JARVIS_PREPARE_BUDGET_SECONDS', '1200')
    calls = []
    apply = CandidateApply(factory())
    def tester(repository, phase, paths):
        assert (repository.root / 'src/app.py').read_text() == 'value = 2\n'
        calls.append((phase, paths, repository.pytest_timeout, apply.service._check()))
        return CommandResult(('pytest', *paths), 0, '1 passed', sandbox=dict(backend='docker'))
    apply.service.tester = tester
    result = apply.apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'applied', result
    full = [call for call in calls if call[0] == 'full_python']
    # One sandbox run per release shard, in order, each with the configured per-shard limit.
    assert [call[1] for call in full] == SHARD_PATHS and {call[2] for call in full} == {240}
    # The verification deadline is the configured budget, not the 900 s candidate ceiling.
    assert all(900 < call[3] <= 1200 for call in full)
    assert [call[2] for call in calls if call[0] == 'targeted'] == [120]
    assert (result['shard_seconds'], result['budget_seconds']) == (240, 1200)
    evidence = next(t for t in result['verification'] if t['phase'] == 'full_python')
    assert [(s['shard'], s['paths'], s['returncode']) for s in evidence['sandbox']['shards']] == [
        (f'python-{n}', paths, 0) for n, paths in enumerate(SHARD_PATHS, 1)]
    assert [r['shard'] for r in evidence['runners']] == ['python-1', 'python-2', 'python-3']
    assert evidence['returncode'] == 0 and not evidence['timed_out'] and not evidence['truncated']
    record = factory().store.get(candidate['id'], internal=True)
    assert record['lease_until'] > time.time() + 900  # no recovery while the shards run
    assert git(source, 'rev-parse', 'HEAD').strip() == result['commit']


@pytest.mark.parametrize('outcome', ['failed', 'timed_out'])
def test_post_apply_failing_or_timed_out_shard_rolls_back(setup, outcome):
    source, _, factory = setup
    candidate = sharded_candidate(setup)
    calls = []
    def tester(repository, phase, paths):
        calls.append((phase, paths))
        if phase == 'full_python' and paths == SHARD_PATHS[1]:
            return CommandResult(('pytest',), -9 if outcome == 'timed_out' else 1, 'FAILED x',
                                 timed_out=outcome == 'timed_out')
        return CommandResult(('pytest',), 0, '1 passed')
    result = CandidateApply(factory(tester=tester)).apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'rolled_back' and result['rollback'] and not result['post_apply_verified']
    ran = [paths for phase, paths in calls if phase == 'full_python']
    # A failure still runs every shard for evidence; a timeout stops as an environment limit.
    assert ran == (SHARD_PATHS if outcome == 'failed' else SHARD_PATHS[:2])
    shards = result['verification'][-1]['sandbox']['shards']
    assert [s['returncode'] for s in shards] == ([0, 1, 0] if outcome == 'failed' else [0, -9])
    assert (source / 'src/app.py').read_text() == 'value = 1\n'
    assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']
    assert git(source, 'status', '--porcelain') == ''


def test_real_post_apply_shards_in_the_sandbox(setup, monkeypatch):
    source, _, factory = setup
    require_live_sandbox(source)
    candidate = sharded_candidate(setup)
    monkeypatch.setenv('JARVIS_PREPARE_SHARD_SECONDS', '300')
    result = CandidateApply(factory(tester=None)).apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'applied', (result.get('error'), result.get('failure_code'), [
        (t['phase'], t['returncode'], t['timed_out'], t['truncated'], t['stdout'][-800:], t['stderr'][-800:],
         t['sandbox']) for t in result['verification']])
    evidence = next(t for t in result['verification'] if t['phase'] == 'full_python')
    shards = evidence['sandbox']['shards']
    assert [(s['paths'], s['returncode'], s['summary'][-1:]) for s in shards] == [
        (paths, 0, ['1 passed']) for paths in SHARD_PATHS]
    assert all(0 < s['sandbox']['limits']['wall_seconds'] <= 300 for s in shards)


def test_unavailable_sandbox_rolls_back_with_its_failure_code(setup, monkeypatch):
    # Real select_backend: the image is missing on a Docker host, Docker itself inside the sandbox.
    from tools import sandbox
    source, _, factory = setup
    candidate = start(factory())
    monkeypatch.setattr(sandbox, 'IMAGE', 'jarvis-sandbox:missing-' + uuid4().hex)
    result = CandidateApply(factory(tester=None)).apply(candidate['id'], confirmation(candidate['id']))['apply']
    assert result['status'] == 'rolled_back' and result['rollback'] and result['verification'] == []
    assert (result['error'], result['failure_code']) == ('Apply or post-apply verification failed',
                                                         'sandbox_unavailable')
    assert (source / 'src/app.py').read_text() == 'value = 1\n'
    assert git(source, 'status', '--porcelain') == ''


def test_previous_turn_approval_does_not_authorize_current_chat(setup):
    from core.agent import AgentRunner
    from test_agent import ScriptedModel, call, final
    _, database, factory = setup
    candidate = authority_candidate(factory)
    store = TaskStore(database.path)
    first = store.create_task(confirmation(candidate['id']))
    current = store.create_task('looks good', conversation_id=first['conversation_id'])
    model = ScriptedModel(call('approve_and_apply_candidate', {'candidate_id': candidate['id']}), final('Denied'))
    runner = AgentRunner(model=model, candidate_service_factory=lambda db: apply_service(factory).service)
    runner.run(current['goal'], task_id=current['id'], task_store=store)
    assert json.loads(model.requests[1]['input'][-1]['output'])['error']['code'] == 'denied'


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
    candidate = authority_candidate(factory)
    before = tree_bytes(source)
    _, output, names = agent_apply_request(setup, goal.format(id=candidate['id']),
        'approve_and_apply_candidate', candidate['id'])
    if 'Do not apply' in goal:
        assert 'approve_and_apply_candidate' not in names
        assert output['error']['code'] == 'unknown_tool'
    else:
        assert 'apply_candidate' in names
        assert output['error']['reason'] == 'candidate_id_required'
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


def test_jarvis_apply_only_changes_host_configured_integration(tmp_path):
    fixture = source_setup.__wrapped__(tmp_path)
    source, _, factory = fixture
    target = integration_target(fixture, tmp_path)
    candidate = start(factory())
    before = tree_bytes(source)
    result = apply_service(factory).apply(candidate['id'], 'Apply candidate ' + candidate['id'])
    assert result['apply']['status'] == 'applied'
    assert (target / 'src/app.py').read_text() == 'value = 2\n'
    assert git(target, 'branch', '--show-current').strip() == 'jarvis/integration'
    assert tree_bytes(source) == before


@pytest.mark.parametrize('target_kind', ['missing', 'source', 'runtime', 'broker', 'symlink', 'wrong_branch'])
def test_jarvis_integration_fails_closed(tmp_path, target_kind):
    from core.engineering_portfolio import Portfolio
    from core.candidate_broker import SOURCE
    fixture = source_setup.__wrapped__(tmp_path)
    source, database, factory = fixture
    target = integration_target(fixture, tmp_path)
    portfolio = Portfolio(database)
    project = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    if target_kind == 'symlink':
        link = tmp_path / 'linked'
        link.symlink_to(target, target_is_directory=True)
        target = link
    chosen = {'missing': None, 'source': str(source), 'runtime': str(Path(__file__).resolve().parents[1]),
        'broker': str(SOURCE)}.get(target_kind, str(target))
    portfolio.register_project(project | dict(integration_repository=chosen,
        integration_branch='jarvis/integration-wrong' if target_kind == 'wrong_branch' else 'jarvis/integration'), replace=True)
    candidate = start(factory())
    before = tree_bytes(source)
    if target_kind == 'wrong_branch':
        result = apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))
        assert result['apply']['status'] == 'denied'
    else:
        with pytest.raises((ToolDenied, OSError)):
            apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))
    assert tree_bytes(source) == before


@pytest.mark.parametrize('message', ['', 'continue', 'looks good', 'Apply the candidate', 'Apply candidate ' + str(uuid4())])
def test_routine_apply_requires_current_explicit_identity(setup, message):
    source, database, factory = setup
    candidate = start(factory())
    before = tree_bytes(source)
    for apply in (apply_service(factory), CandidateTools(message, database, lambda _: factory())):
        with pytest.raises(ToolDenied, match='exact candidate ID'):
            if isinstance(apply, CandidateApply):
                apply.apply(candidate['id'], message)
            else:
                apply.apply(candidate['id'])
    assert tree_bytes(source) == before


@pytest.mark.parametrize('goal,category', [('Increase Jarvis authority', 'authority_expansion'),
    ('Major architecture migration of Jarvis', 'major_change')])
def test_forged_mailbox_approval_cannot_apply_escalated_candidate(tmp_path, monkeypatch, goal, category):
    """R1-3: approval text relayed by the production container is never authority."""
    from core.candidate_broker import Broker, approve_on_host
    from core.engineering_authority import HostApprovalRequired
    from test_candidate_bridge import request
    monkeypatch.setenv('JARVIS_MODE', 'development')
    fixture = source_setup.__wrapped__(tmp_path)
    source, _, factory = fixture
    target = integration_target(fixture, tmp_path)
    service = factory()
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    record['goal'] = goal
    service.store.save(record)
    broker = Broker(apply_service(factory).service, source=source)
    preview = broker.execute(request('preview', 'Preview ' + candidate['id'], {'candidate_id': candidate['id']}))
    assert preview['authority']['escalation'] == [category]
    assert 'Approve and apply' not in preview['required_confirmation']
    assert '--approve-apply ' + candidate['id'] in preview['required_confirmation']
    before, integration = tree_bytes(source), tree_bytes(target)
    with pytest.raises(HostApprovalRequired) as caught:
        broker.execute(request('apply', confirmation(candidate['id']), {'candidate_id': candidate['id']}))
    assert caught.value.public_error()['reason'] == 'host_approval_required'
    assert caught.value.decision.escalation == [category]
    assert tree_bytes(source) == before and tree_bytes(target) == integration
    assert factory().store.get(candidate['id'], internal=True).get('apply') is None
    # The host controller channel honors the exact owner phrase, still only in integration.
    result = approve_on_host(candidate['id'], confirmation(candidate['id']),
        service=apply_service(factory).service, source=source)
    assert result['apply']['status'] == 'applied'
    assert (target / 'src/app.py').read_text() == 'value = 2\n' and tree_bytes(source) == before


def test_forged_approval_through_production_mailbox_client_is_denied(tmp_path, monkeypatch):
    from core.candidate_bridge import CandidateClient
    from core.candidate_broker import Broker
    from test_candidate_bridge import request
    fixture = source_setup.__wrapped__(tmp_path)
    source, _, factory = fixture
    target = integration_target(fixture, tmp_path)
    candidate = authority_candidate(factory)
    broker = Broker(apply_service(factory).service, source=source)
    def invoke(client, action, arguments):
        raw = request(action, client.goal, arguments)
        with monkeypatch.context() as host:
            host.setenv('JARVIS_MODE', 'development')
            try:
                return broker.execute(raw)
            except ToolDenied as exc:
                # Same closed projection the real client applies to responses.
                raise ToolDenied('Candidate broker denied the request', reason=exc.public_error()['reason'])
    monkeypatch.setattr(CandidateClient, 'invoke', invoke)
    monkeypatch.setenv('JARVIS_MODE', 'production')
    before = tree_bytes(target)
    tools = CandidateTools(confirmation(candidate['id']), factory().store.database,
        source_context=dict(task_id=str(uuid4()), conversation_id=str(uuid4())))
    with pytest.raises(ToolDenied) as caught:
        tools.apply(candidate['id'])
    assert caught.value.reason == 'host_approval_required'
    assert 'not accepted' in caught.value.public_error()['message']
    assert tree_bytes(target) == before


def test_host_approval_still_requires_exact_phrase(tmp_path, monkeypatch):
    from core.candidate_broker import approve_on_host
    from core.engineering_authority import EscalationRequired
    monkeypatch.setenv('JARVIS_MODE', 'development')
    fixture = source_setup.__wrapped__(tmp_path)
    source, _, factory = fixture
    target = integration_target(fixture, tmp_path)
    candidate = authority_candidate(factory)
    before = tree_bytes(target)
    with pytest.raises(EscalationRequired):
        approve_on_host(candidate['id'], 'Apply candidate ' + candidate['id'], service=factory(), source=source)
    assert tree_bytes(target) == before


def test_host_approval_entrypoint_refuses_non_interactive_callers(monkeypatch):
    import core.candidate_broker as broker
    monkeypatch.setattr(broker.sys.stdin, 'isatty', lambda: False, raising=False)
    monkeypatch.setenv('JARVIS_MODE', 'development')
    with pytest.raises(ToolDenied):
        broker.approve_main(str(uuid4()))


def test_read_only_project_apply_requires_owner_approval(setup):
    """R1-4: operator read_only metadata gates apply, not inspection or candidates."""
    from core.engineering_authority import EscalationRequired
    from core.engineering_portfolio import Portfolio
    source, database, factory = setup
    portfolio = Portfolio(database)
    fixture = next(p for p in portfolio.projects() if p['project_id'] == 'fixture')
    portfolio.register_project(fixture | dict(read_only=True), replace=True)
    candidate = start(factory())
    assert candidate['state'] == 'passed'
    preview = apply_service(factory).preview(candidate['id'])
    assert preview['eligible'] and preview['authority']['escalation'] == ['major_change']
    before = tree_bytes(source)
    with pytest.raises(EscalationRequired) as caught:
        apply_service(factory).apply(candidate['id'], 'Apply candidate ' + candidate['id'])
    assert caught.value.decision.escalation == ['major_change']
    assert tree_bytes(source) == before
    result = apply_service(factory).apply(candidate['id'], confirmation(candidate['id']))
    assert result['apply']['status'] == 'applied' and result['apply']['target_root'] == str(source)


def test_writable_project_routine_apply_stays_in_its_own_root(setup, tmp_path):
    from core.engineering_portfolio import Portfolio
    source, database, factory = setup
    candidate = start(factory())
    result = apply_service(factory).apply(candidate['id'], 'Apply candidate ' + candidate['id'])
    assert result['apply']['status'] == 'applied' and result['apply']['target_root'] == str(source)
    # A changed or foreign registration can never redirect an apply.
    other = start(factory())
    portfolio = Portfolio(database)
    fixture = next(p for p in portfolio.projects() if p['project_id'] == 'fixture')
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    portfolio.register_project(fixture | dict(repository=str(elsewhere)), replace=True)
    before = tree_bytes(source)
    with pytest.raises(ToolDenied):
        apply_service(factory).apply(other['id'], 'Apply candidate ' + other['id'])
    assert tree_bytes(source) == before


def test_read_only_target_classification_is_host_metadata_only():
    from core.engineering_authority import assess_action
    assert assess_action('candidate_apply', changed_paths=['src/app.py']).allowed
    decision = assess_action('candidate_apply', changed_paths=['src/app.py'], read_only_target=True)
    assert decision.approval_required and decision.escalation == ['major_change']
    assert assess_action('candidate_apply', phase='prepare', read_only_target=True).allowed


def test_routine_mailbox_apply_needs_no_approval(tmp_path, monkeypatch):
    from core.candidate_broker import Broker
    from test_candidate_bridge import request
    monkeypatch.setenv('JARVIS_MODE', 'development')
    fixture = source_setup.__wrapped__(tmp_path)
    source, _, factory = fixture
    target = integration_target(fixture, tmp_path)
    candidate = start(factory())
    result = Broker(apply_service(factory).service, source=source).execute(
        request('apply', 'Apply candidate ' + candidate['id'], {'candidate_id': candidate['id']}))
    assert result['apply']['status'] == 'applied'
    assert (target / 'src/app.py').read_text() == 'value = 2\n'
