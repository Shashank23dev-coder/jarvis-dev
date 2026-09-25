"""Candidate authority, isolation and durable gates. Provider calls are scripted."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

import main
from core.database import Database, SCHEMA
from core.self_edit import Limits, SelfEditService, REVIEW, PROPOSE
from tools import CommandResult, RepositoryTools, ToolDenied
from tools.candidate_edit import CandidateEditor, CandidatePolicy, scan


def git(root, *args):
    return subprocess.run(['/usr/bin/git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def tree_bytes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file() and not path.is_symlink()}


def propose(payload, instructions, timeout):
    assert timeout > 0
    if instructions == REVIEW:
        assert 'diff' in payload and 'tests' in payload and 'original_files' in payload
        assert 'edits' not in payload and 'reasoning' not in payload
        return {'verdict': 'passed', 'summary': 'Independent review found the small fix correct.'}
    assert instructions == PROPOSE and payload['files']['src/app.py'] == 'value = 1\n'
    return {'edits': [{'path': 'src/app.py', 'content': 'value = 2\n'}]}


def passed(repository, phase, paths):
    assert repository.root.name == 'worktree'
    assert (repository.root / 'src/app.py').read_text() == 'value = 2\n'
    return CommandResult(('pytest',), 0, '1 passed')


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    git(source, 'config', 'user.email', 'tests@example.invalid')
    git(source, 'config', 'user.name', 'Tests')
    for directory in ('src', 'tests', 'docs'):
        (source / directory).mkdir()
    (source / 'src/app.py').write_text('value = 1\n')
    (source / 'tests/test_app.py').write_text('from src.app import value\ndef test_value(): assert value == 2\n')
    (source / 'docs/readme.md').write_text('Example\n')
    (source / '.gitignore').write_text('.env\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Baseline fixture')
    database = Database(tmp_path / 'state.db')
    def service(**kwargs):
        return SelfEditService(database, source_root=source, candidate_root=tmp_path / 'candidates',
                               model=kwargs.pop('model', propose), tester=kwargs.pop('tester', passed), **kwargs)
    return source, database, service


def start(service):
    return service.start('Fix the value', ['src/app.py'], ['tests/test_app.py'])


def test_worktree_isolation_main_metadata_immutability_and_success(setup):
    source, _, factory = setup
    before = tree_bytes(source)
    service = factory()
    candidate = start(service)
    assert candidate['state'] == 'passed', candidate
    worktree = Path(candidate['worktree'])
    assert (worktree / '.git').is_file()
    assert git(worktree, 'branch', '--show-current').strip() == candidate['branch']
    assert git(worktree, 'rev-parse', 'HEAD').strip() == candidate['base_commit']
    assert (worktree / 'src/app.py').read_text() == 'value = 2\n'
    assert tree_bytes(source) == before
    assert git(source, 'branch', '--list', 'jarvis/*') == ''
    assert candidate['files_changed'] == ['src/app.py']
    assert candidate['diff_summary'] == {'files': 1, 'added': 1, 'deleted': 1}
    assert '-value = 1' in candidate['diff'] and '+value = 2' in candidate['diff']
    assert [item['phase'] for item in candidate['test_results']] == ['targeted', 'full_python']
    assert candidate['reviewer_result']['verdict'] == 'passed'
    assert candidate['iterations'] == 2 and candidate['tool_calls'] <= 64
    assert 'manifest' not in candidate


@pytest.mark.parametrize('path', ['.env', '.git/config', '.venv/a.py', 'tools/policy.py',
    'src/policy.py', 'src/security_rules.py', 'src/access_policy.py',
    'src/secrets.py', 'src/credentials.py', 'docs/deployment.md', 'src/state.db',
    'src/state.sqlite3', 'docs/key.pem', '.aws/config', 'main.py/../.env',
    '../outside.py', '/tmp/escape.py', 'src/link/../../outside.py', 'src\\escape.py',
    'core/self_edit.py', 'core/candidate_store.py', 'tools/repository.py',
    'src/a.py\x00', 'src/a.py\n', 'src/./app.py'])
def test_protected_paths_rejected_before_writes(setup, path):
    source, _, factory = setup
    before = tree_bytes(source)
    def model(payload, instructions, timeout):
        return {'edits': [{'path': path, 'content': 'changed\n'}]}
    service = factory(model=model)
    candidate = start(service)
    assert candidate['state'] == 'rejected', candidate
    if candidate['worktree']:
        assert (Path(candidate['worktree']) / 'src/app.py').read_text() == 'value = 1\n'
    assert tree_bytes(source) == before


def test_inspection_denial_is_recorded_without_model_or_worktree(setup):
    _, _, factory = setup
    service = factory(model=lambda *args: pytest.fail('Must not call model'))
    candidate = service.start('Read protected', ['.env'], ['tests/test_app.py'])
    assert candidate['state'] == 'rejected' and candidate['worktree'] is None
    assert service.inspect(candidate['id'])['error']


@pytest.mark.parametrize('kind', ['file_symlink', 'directory_symlink', 'hardlink', 'fifo'])
def test_link_and_special_file_escapes(setup, tmp_path, kind):
    source, _, factory = setup
    service = factory()
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    worktree = Path(candidate['worktree'])
    outside = tmp_path / 'outside.py'
    outside.write_text('untouched\n')
    target = worktree / 'src/escape.py'
    if kind == 'file_symlink':
        target.symlink_to(outside)
    elif kind == 'directory_symlink':
        (worktree / 'escape').symlink_to(tmp_path, target_is_directory=True)
    elif kind == 'hardlink':
        os.link(outside, target)
    else:
        os.mkfifo(target)
    editor = CandidateEditor(worktree, record['manifest'])
    with pytest.raises(ToolDenied):
        editor.apply([{'path': 'src/app.py', 'content': 'value = 3\n'}], lambda path: 'value = 1\n')
    assert outside.read_text() == 'untouched\n'
    assert (source / 'src/app.py').read_text() == 'value = 1\n'
    assert service.inspect(candidate['id'])['state'] == 'rejected'


def test_symlink_in_committed_baseline_is_rejected(setup, tmp_path):
    source, _, factory = setup
    (source / 'src/link.py').symlink_to(tmp_path / 'outside')
    git(source, 'add', 'src/link.py')
    git(source, 'commit', '-m', 'Unsafe baseline fixture')
    candidate = start(factory())
    assert candidate['state'] == 'rejected'
    assert not candidate['test_results']


@pytest.mark.parametrize('limits', [Limits(files=1), Limits(lines=1), Limits(tool_calls=1), Limits(iterations=1)])
def test_hard_limits(setup, limits):
    _, _, factory = setup
    def two_edits(payload, instructions, timeout):
        if instructions == REVIEW:
            return propose(payload, instructions, timeout)
        return {'edits': [{'path': 'src/app.py', 'content': 'value = 2\n'},
                          {'path': 'docs/readme.md', 'content': 'Updated\n'}]}
    candidate = start(factory(limits=limits, model=two_edits))
    assert candidate['state'] == 'rejected', candidate
    assert candidate['iterations'] <= limits.iterations
    assert candidate['tool_calls'] <= limits.tool_calls
    if limits.files == 1 or limits.lines == 1:
        assert (Path(candidate['worktree']) / 'src/app.py').read_text() == 'value = 1\n'


def test_hard_wall_clock_stops_worker_and_late_writes(setup, tmp_path):
    _, _, factory = setup
    marker = tmp_path / 'late'
    def slow_model(*args):
        time.sleep(5)
        marker.write_text('must not run')
        return {'edits': []}
    before = time.monotonic()
    candidate = start(factory(limits=Limits(seconds=0.5), model=slow_model))
    assert time.monotonic() - before < 2
    assert candidate['state'] == 'rejected' and not marker.exists()
    assert not candidate['busy']


@pytest.mark.parametrize('failure_phase', ['targeted', 'full_python', 'full_ui'])
def test_test_failures_gate_full_suite_and_review(setup, failure_phase):
    source, _, factory = setup
    (source / 'tests/ui.test.cjs').write_text("require('node:test')('ok', () => {});\n")
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'UI fixture')
    def tester(repository, phase, paths):
        return CommandResult(('test',), int(phase == failure_phase), 'test result')
    candidate = start(factory(tester=tester))
    assert candidate['state'] == 'needs_revision', candidate
    phases = [item['phase'] for item in candidate['test_results']]
    assert phases == ['targeted', 'full_python', 'full_ui'][:phases.index(failure_phase) + 1]
    assert candidate['reviewer_result'] is None and candidate['iterations'] == 1
    assert candidate['tested_fingerprint'] is None


@pytest.mark.parametrize('flag', ['timed_out', 'truncated'])
def test_zero_exit_with_incomplete_evidence_does_not_pass(setup, flag):
    _, _, factory = setup
    candidate = start(factory(tester=lambda *args: CommandResult(('test',), 0, '', **{flag: True})))
    assert candidate['state'] == 'needs_revision'
    assert len(candidate['test_results']) == 1 and candidate['reviewer_result'] is None


@pytest.mark.parametrize('verdict', ['rejected', 'needs_revision'])
def test_independent_reviewer_can_veto_successful_tests(setup, verdict):
    _, _, factory = setup
    def model(payload, instructions, timeout):
        if instructions == REVIEW:
            assert all(item['returncode'] == 0 for item in payload['tests'])
            return {'verdict': verdict, 'summary': 'The proposed change does not satisfy the goal.'}
        return propose(payload, instructions, timeout)
    service = factory(model=model)
    candidate = start(service)
    assert candidate['state'] == verdict
    assert service.inspect(candidate['id'])['state'] == verdict
    assert Path(candidate['worktree']).exists()


def test_rerun_invalidates_review_and_requires_new_review(setup):
    _, _, factory = setup
    service = factory()
    candidate = start(service)
    rerun = service.tests(candidate['id'])
    assert rerun['state'] == 'needs_revision' and rerun['reviewer_result'] is None
    reviewed = service.review(candidate['id'])
    assert reviewed['state'] == 'passed', reviewed
    assert len(reviewed['test_results']) == 4


def test_external_tampering_invalidates_test_and_review_evidence(setup):
    _, _, factory = setup
    service = factory()
    candidate = start(service)
    worktree = Path(candidate['worktree'])
    (worktree / 'src/app.py').write_text('value = 3\n')
    inspected = service.inspect(candidate['id'])
    assert inspected['state'] == 'needs_revision' and inspected['tested_fingerprint'] is None
    assert service.review(candidate['id'])['state'] == 'needs_revision'
    (worktree / '.env').write_text('protected')
    rejected = service.tests(candidate['id'])
    assert rejected['state'] == 'rejected'
    assert 'protected' not in rejected['diff']


@pytest.mark.parametrize('content', ['Additional docs\n', ''], ids=['text', 'empty'])
def test_new_file_diff_and_duplicate_edit_rejection(setup, content):
    _, _, factory = setup
    def model(payload, instructions, timeout):
        if instructions == REVIEW:
            assert 'diff --git a/docs/new.md b/docs/new.md' in payload['diff']
            assert 'new file mode 100644' in payload['diff']
            if content:
                assert '+Additional docs' in payload['diff']
            return {'verdict': 'passed', 'summary': 'Documentation added.'}
        return {'edits': [{'path': 'docs/new.md', 'content': content}]}
    candidate = start(factory(model=model, tester=lambda *args: CommandResult(('test',), 0, 'passed')))
    assert candidate['state'] == 'passed' and candidate['files_changed'] == ['docs/new.md']
    def duplicate(*args):
        return {'edits': [{'path': 'src/app.py', 'content': 'one'}, {'path': 'src/app.py', 'content': 'two'}]}
    rejected = start(factory(model=duplicate))
    assert rejected['state'] == 'rejected'


@pytest.mark.parametrize('content', ['\x00binary', 'x' * 256001,
                                    'API_KEY = "sensitive-value"', '-----BEGIN RSA PRIVATE KEY-----'],
                         ids=['binary', 'oversized', 'credential', 'private-key'])
def test_bad_content_blocked(setup, content):
    _, _, factory = setup
    candidate = start(factory(model=lambda *args: {'edits': [{'path': 'src/app.py', 'content': content}]}))
    assert candidate['state'] in {'rejected', 'needs_revision'}
    assert not candidate['test_results']
    assert (Path(candidate['worktree']) / 'src/app.py').read_text() == 'value = 1\n'


def test_explicit_cleanup_and_restart_persistence(setup):
    source, database, factory = setup
    service = factory()
    candidate = start(service)
    before = tree_bytes(source)
    database.close()
    restarted = factory()
    assert restarted.inspect(candidate['id']) == candidate
    discarded = restarted.discard(candidate['id'])
    assert discarded['state'] == 'discarded'
    assert not Path(candidate['worktree']).parent.exists()
    assert tree_bytes(source) == before
    database.close()
    assert factory().inspect(candidate['id'])['state'] == 'discarded'
    assert factory().discard(candidate['id'])['state'] == 'discarded'
    with pytest.raises(ValueError):
        factory().tests(candidate['id'])


def test_cleanup_does_not_follow_symlinks_or_tampered_database_path(setup, tmp_path):
    _, _, factory = setup
    service = factory()
    candidate = start(service)
    outside = tmp_path / 'keep.txt'
    outside.write_text('keep')
    (Path(candidate['worktree']) / 'escape').symlink_to(tmp_path, target_is_directory=True)
    service.discard(candidate['id'])
    assert outside.read_text() == 'keep'
    other = start(service)
    record = service.store.get(other['id'], internal=True)
    record['worktree'] = str(tmp_path)
    service.store.save(record)
    with pytest.raises(ToolDenied):
        service.discard(other['id'])
    assert outside.exists()


def test_unborn_and_dirty_sources_fail_without_mutation(setup, tmp_path):
    source, database, factory = setup
    (source / 'src/app.py').write_text('dirty\n')
    before = tree_bytes(source)
    candidate = start(factory())
    assert candidate['state'] == 'rejected' and candidate['worktree'] is None
    assert tree_bytes(source) == before
    unborn = tmp_path / 'unborn'
    unborn.mkdir()
    git(unborn, 'init')
    service = SelfEditService(database, source_root=unborn, candidate_root=tmp_path / 'other')
    candidate = start(service)
    assert candidate['state'] == 'rejected' and 'baseline commit' in candidate['error']


def test_api_contract_invalid_requests_and_restart(setup):
    _, database, factory = setup
    application = main.create_app(database.path)
    application.dependency_overrides[main.get_self_edit] = lambda: factory()
    with TestClient(application) as client:
        for method, url in [('get', '/candidates/missing'), ('post', '/candidates/missing/tests'),
                            ('post', '/candidates/missing/review'), ('delete', '/candidates/missing')]:
            assert getattr(client, method)(url).status_code == 404
        assert client.post('/candidates', json={'goal': 'Fix'}).status_code == 422
        assert client.post('/candidates', json={'goal': 'Fix', 'inspect_paths': ['src/app.py'],
                            'targeted_tests': ['tests/test_app.py'], 'root': '/tmp'}).status_code == 422
        response = client.post('/candidates', json={'goal': 'Fix', 'inspect_paths': ['src/app.py'],
                                                   'targeted_tests': ['tests/test_app.py']})
        assert response.status_code == 200, response.text
        candidate = response.json()
        assert candidate['state'] == 'passed', candidate
    with TestClient(application) as client:
        assert client.get('/candidates/' + candidate['id']).json() == candidate
        assert client.post('/candidates/' + candidate['id'] + '/tests').json()['state'] == 'needs_revision'
        assert client.post('/candidates/' + candidate['id'] + '/review').json()['state'] == 'passed'
        assert client.delete('/candidates/' + candidate['id']).json()['state'] == 'discarded'
        assert client.post('/candidates/' + candidate['id'] + '/tests').status_code == 409


def test_version_one_migration_preserves_data_and_is_repeatable(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        connection.execute("INSERT INTO conversations VALUES ('old','timestamp')")
        connection.execute('PRAGMA user_version=1')
    database = Database(path)
    database.initialize()
    database.close()
    database.initialize()
    with database.transaction() as connection:
        assert connection.execute('SELECT id FROM conversations').fetchone()[0] == 'old'
        assert connection.execute('SELECT count(*) FROM candidates').fetchone()[0] == 0
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
    database.close()


def test_real_candidate_tests_use_bubblewrap_and_remain_readonly(setup):
    source, _, factory = setup
    candidate = start(factory(tester=None))
    # Restricted hosts may deny namespace creation, but never run tests on host.
    if candidate['state'] != 'passed':
        assert candidate['test_results'], candidate
        assert 'bwrap:' in candidate['test_results'][0]['stderr'], candidate
    else:
        assert all(result['returncode'] == 0 for result in candidate['test_results'])
    assert (source / 'src/app.py').read_text() == 'value = 1\n'


def test_entire_edit_batch_is_validated_before_first_write(setup):
    _, _, factory = setup
    def model(*args):
        return {'edits': [{'path': 'src/app.py', 'content': 'value = 2\n'},
                          {'path': '.env', 'content': 'blocked'}]}
    candidate = start(factory(model=model))
    assert candidate['state'] == 'rejected'
    assert (Path(candidate['worktree']) / 'src/app.py').read_text() == 'value = 1\n'


def test_empty_new_files_still_count_against_file_limit(setup):
    _, _, factory = setup
    def model(*args):
        return {'edits': [{'path': 'docs/one.md', 'content': ''},
                          {'path': 'docs/two.md', 'content': ''}]}
    candidate = start(factory(model=model, limits=Limits(files=1)))
    assert candidate['state'] == 'rejected'
    assert not (Path(candidate['worktree']) / 'docs/one.md').exists()


def test_post_test_protected_change_is_rejected(setup):
    _, _, factory = setup
    def tester(repository, phase, paths):
        (repository.root / '.env').write_text('blocked')
        return CommandResult(('test',), 0, 'passed')
    candidate = start(factory(tester=tester))
    assert candidate['state'] == 'rejected' and candidate['reviewer_result'] is None
    assert 'blocked' not in candidate['diff']


def test_supervisor_kills_detached_test_descendants(setup, tmp_path):
    _, _, factory = setup
    marker = tmp_path / 'escaped-worker'
    def tester(*args):
        import sys
        subprocess.Popen([sys.executable, '-c',
                          "import time,pathlib; time.sleep(1.5); pathlib.Path(" + repr(str(marker)) + ").write_text('late')"],
                         start_new_session=True)
        time.sleep(10)
    candidate = start(factory(tester=tester, limits=Limits(seconds=0.7)))
    assert any(event['event'] == 'test_targeted' for event in candidate['events']), candidate
    assert candidate['state'] == 'rejected'
    time.sleep(1)
    assert not marker.exists()


def test_production_subprocess_rejects_unborn_repository_without_provider(setup, tmp_path):
    _, database, _ = setup
    source = tmp_path / 'unborn-production'
    source.mkdir()
    git(source, 'init')
    service = SelfEditService(database, source_root=source, candidate_root=tmp_path / 'production')
    candidate = start(service)
    assert candidate['state'] == 'rejected', candidate
    assert 'baseline commit' in candidate['error']
    assert not candidate['busy']
    assert service.discard(candidate['id'])['state'] == 'discarded'


def test_real_candidate_full_python_ui_and_secret_visibility(setup):
    source, _, factory = setup
    (source / '.env').write_text('DO_NOT_EXPOSE')
    (source / 'src/private.sqlite3').write_text('DO_NOT_EXPOSE')
    (source / 'src/credentials.py').write_text('DO_NOT_EXPOSE')
    (source / 'tests/test_app.py').write_text(
        'from pathlib import Path\nimport pytest\nfrom src.app import value\n'
        'def test_value():\n'
        '    assert value == 2\n'
        "    assert not Path('/workspace/.env').exists()\n"
        "    assert not Path('/workspace/.git').exists()\n"
        "    assert not Path('/workspace/src/private.sqlite3').exists()\n"
        "    assert not Path('/workspace/src/credentials.py').exists()\n"
        "    with pytest.raises(OSError): Path('/workspace/src/app.py').write_text('bad')\n")
    (source / 'tests/ui.test.cjs').write_text(
        "const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs');\n"
        "test('readonly', () => { assert.throws(() => fs.writeFileSync('/workspace/src/app.py', 'bad')); });\n")
    git(source, 'add', '.')
    git(source, 'add', '-f', '.env')
    git(source, 'commit', '-m', 'Sandbox isolation fixtures')
    before = tree_bytes(source)
    candidate = start(factory(tester=None))
    if candidate['state'] != 'passed':
        assert candidate['test_results'] and 'bwrap:' in candidate['test_results'][0]['stderr'], candidate
    else:
        assert [result['phase'] for result in candidate['test_results']] == ['targeted', 'full_python', 'full_ui']
        assert all(result['returncode'] == 0 for result in candidate['test_results'])
    assert 'DO_NOT_EXPOSE' not in json.dumps(candidate)
    assert tree_bytes(source) == before


def test_busy_lease_and_interrupted_restart_cleanup(setup):
    _, _, factory = setup
    service = factory()
    candidate = start(service)
    service.store.claim(candidate['id'])
    with pytest.raises(ValueError):
        factory().tests(candidate['id'])
    assert service.store.get(candidate['id'])['busy']
    record = service.store.get(candidate['id'], internal=True)
    record['lease_until'] = time.time() - 1
    service.store.save(record)
    restarted = factory()
    recovered = restarted.inspect(candidate['id'])
    assert recovered['state'] == 'rejected' and not recovered['busy']
    assert recovered['tested_fingerprint'] is None
    assert restarted.discard(candidate['id'])['state'] == 'discarded'


def test_tool_budget_cannot_be_reset_by_restart(setup):
    _, database, factory = setup
    service = factory()
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    record['tool_calls'] = 64
    service.store.save(record)
    database.close()
    rejected = factory().review(candidate['id'])
    assert rejected['state'] == 'rejected' and rejected['tool_calls'] == 64
    assert factory().discard(candidate['id'])['state'] == 'discarded'


@pytest.mark.parametrize('options', [{'iterations': 5}, {'files': 9}, {'lines': 401},
    {'tool_calls': 65}, {'seconds': 301}, {'seconds': float('inf')}, {'files': True}])
def test_host_cannot_raise_absolute_limit_ceilings(options):
    with pytest.raises(ValueError):
        Limits(**options)


def test_storage_root_cannot_be_inside_source_or_symlinked(setup, tmp_path):
    source, database, _ = setup
    with pytest.raises(ValueError):
        SelfEditService(database, source_root=source, candidate_root=source / 'candidates')
    link = tmp_path / 'link'
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError):
        SelfEditService(database, source_root=source, candidate_root=link)


def test_lowered_budget_survives_restart_with_default_host_limits(setup):
    _, _, factory = setup
    service = factory(limits=Limits(seconds=10))
    candidate = start(service)
    record = service.store.get(candidate['id'], internal=True)
    record['elapsed_seconds'] = 10
    service.store.save(record)
    rejected = factory().tests(candidate['id'])
    assert rejected['state'] == 'rejected'
    assert rejected['tool_calls'] == candidate['tool_calls']
    assert factory().discard(candidate['id'])['state'] == 'discarded'


def test_bounded_evidence_does_not_claim_untruncated_success(setup):
    _, _, factory = setup
    candidate = start(factory(tester=lambda *args: CommandResult(('test',), 0, 'x' * 16001)))
    assert candidate['state'] == 'needs_revision'
    assert len(candidate['test_results']) == 1
    assert candidate['test_results'][0]['truncated']
    assert len(candidate['test_results'][0]['stdout']) == 16000
