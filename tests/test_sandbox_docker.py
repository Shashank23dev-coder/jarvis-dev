"""Backend contracts and opt-in real Docker adversarial/integration checks."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from tools import RepositoryTools, CommandResult, ToolDenied
from tools import sandbox
from test_self_edit import setup, start, git
from core.candidate_apply import CandidateApply


@pytest.fixture
def repository(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_ok.py').write_text('def test_ok(): assert True\n')
    return RepositoryTools(tmp_path)


def test_functional_bubblewrap_preferred(repository, monkeypatch):
    repository._bwrap = '/usr/bin/true'
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == '/usr/bin/true'
        return CommandResult(tuple(argv), 0, '')
    monkeypatch.setattr(repository, '_execute', execute)
    assert sandbox.select_backend(repository, repository.root)[0] == 'bubblewrap'
    assert len(calls) == 1


@pytest.mark.parametrize('present', [False, True])
def test_docker_fallback_only_after_bubblewrap_failure(repository, monkeypatch, present):
    repository._bwrap = '/usr/bin/false' if present else '/missing/bwrap'
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[0] == repository._bwrap:
            return CommandResult(tuple(argv), 1, '', 'bwrap: denied')
        assert argv[0] == '/usr/bin/docker'
        if 'inspect' in argv:
            return CommandResult(tuple(argv), 0, json.dumps([{'Id': 'sha256:test', 'Config': {}}]))
        return CommandResult(tuple(argv), 0, '')
    monkeypatch.setattr(repository, '_execute', execute)
    monkeypatch.setattr(RepositoryTools, '_execute', lambda *a, **k: CommandResult((), 0, ''))
    assert sandbox.select_backend(repository, repository.root)[0] == 'docker'
    assert any('run' in call for call in calls)


def test_fail_closed_and_production_does_not_use_docker(repository, monkeypatch):
    repository._bwrap = '/missing/bwrap'
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        return CommandResult(tuple(argv), 1, '', 'unavailable')
    monkeypatch.setattr(repository, '_execute', execute)
    with pytest.raises(ToolDenied, match='host execution disabled'):
        repository.run_pytest()
    assert all(call[0] == '/usr/bin/docker' for call in calls)
    calls.clear()
    monkeypatch.setenv('JARVIS_MODE', 'production')
    with pytest.raises(ToolDenied, match='disabled in production'):
        repository.run_pytest()
    assert not calls


def test_docker_configuration_is_fixed():
    argv = sandbox.docker_command('/tmp/snapshot', 'sha256:test', 'name', 10)
    for option in ('--network=none', '--user=10001:10001', '--read-only', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges:true', '--pids-limit=128', '--cpus=1',
                   '--memory=536870912', '--memory-swap=536870912', '--pull=never'):
        assert option in argv
    assert argv.count('--mount') == 1
    assert 'readonly,bind-recursive=disabled' in argv[argv.index('--mount') + 1]
    assert not any('docker.sock' in item for item in argv[3:])


@pytest.mark.parametrize('backend', ['bubblewrap', 'docker'])
def test_identical_result_schema(repository, monkeypatch, backend):
    monkeypatch.setattr(sandbox, 'select_backend', lambda *a: (backend, None, []))
    result = CommandResult((), 7, 'out', 'err', False, True)
    monkeypatch.setattr(repository, '_execute', lambda *a, **k: result)
    monkeypatch.setattr(sandbox, 'docker_execute', lambda *a, **k: result)
    actual = repository.run_pytest()
    assert set(asdict(actual)) == {'command', 'returncode', 'stdout', 'stderr', 'timed_out', 'truncated', 'sandbox'}
    assert actual.command == ('pytest', 'tests') and actual.returncode == 7
    assert actual.sandbox['backend'] == backend
    assert actual.sandbox['started_at'] <= actual.sandbox['ended_at']
    assert actual.sandbox['truncated']


live = pytest.mark.skipif(os.environ.get('JARVIS_DOCKER_TESTS') != '1',
                          reason='Set JARVIS_DOCKER_TESTS=1 for required live Docker checks')


@live
def test_live_docker_boundaries(repository):
    repository._bwrap = '/missing/bwrap'
    for name in ('.env', 'state/production.sqlite3', '.docker/config.json', '.ssh/id_rsa', 'credentials.json'):
        file = repository.root / name
        file.parent.mkdir(exist_ok=True)
        file.write_text('DO_NOT_EXPOSE')
    (repository.root / 'linked').symlink_to('/etc/passwd')
    os.link(repository.root / '.env', repository.root / 'hardlinked')
    (repository.root / 'tests/test_ok.py').write_text('''
import os, socket
from pathlib import Path
import pytest

def test_boundaries():
    assert os.getuid() == 10001
    status = Path('/proc/self/status').read_text()
    assert 'CapEff:\\t0000000000000000' in status
    assert 'NoNewPrivs:\\t1' in status
    assert 'Seccomp:\\t2' in status
    with pytest.raises(OSError): Path('/proc/1/mem').open('rb')
    assert not any(key in os.environ for key in ('OPENAI_API_KEY', 'DOCKER_HOST', 'SSH_AUTH_SOCK'))
    for path in ('/workspace/.env', '/workspace/state', '/workspace/.docker', '/workspace/.ssh',
                 '/workspace/credentials.json', '/workspace/linked', '/workspace/hardlinked',
                 '/var/run/docker.sock', '/var/lib/jarvis', '/home/ubuntu', '/app'):
        assert not Path(path).exists(), path
    for path in ('/workspace/new', '/workspace/tests/test_ok.py', '/etc/new', '/runtime/new'):
        with pytest.raises(OSError): Path(path).write_text('mutated')
    Path('/tmp/allowed').write_text('scratch')
    assert sorted(p.name for p in Path('/sys/class/net').iterdir()) == ['lo']
    with socket.socket() as sock:
        sock.settimeout(0.2)
        with pytest.raises(OSError): sock.connect(('1.1.1.1', 443))
    cg = Path('/sys/fs/cgroup')
    assert (cg / 'memory.max').read_text().strip() == '536870912'
    assert (cg / 'memory.swap.max').read_text().strip() == '0'
    assert (cg / 'pids.max').read_text().strip() == '128'
    quota, period = map(int, (cg / 'cpu.max').read_text().split())
    assert quota == period
''')
    result = repository.run_pytest()
    assert result.returncode == 0, result
    assert result.sandbox['backend'] == 'docker'
    assert not (repository.root / 'new').exists()


@live
@pytest.mark.parametrize('kind', ['timeout', 'truncation', 'memory', 'pids'])
def test_live_limits_and_cleanup(repository, kind):
    repository._bwrap = '/missing/bwrap'
    backend, image, _ = sandbox.select_backend(repository, repository.root)
    assert backend == 'docker'
    command = {
        'timeout': 'import time; time.sleep(60)',
        'truncation': 'while True: print("x" * 10000, flush=True)',
        'memory': 'x = bytearray(1024 * 1024 * 1024)',
        'pids': '''import subprocess
children=[]
try:
 for i in range(200): children.append(subprocess.Popen(['/usr/bin/sleep','30']))
except OSError:
 print('PID_LIMIT', flush=True)
else:
 raise AssertionError('PID limit missing')
finally:
 for child in children: child.kill(); child.wait()
''',
    }[kind]
    repository.max_output_bytes = 4000
    started = time.monotonic()
    result = sandbox.docker_execute(repository, repository.root, image,
                                    ['/runtime/bin/python', '-c', command], 3 if kind == 'timeout' else 15)
    assert time.monotonic() - started < 25
    if kind == 'timeout': assert result.timed_out
    if kind == 'truncation': assert result.truncated and len(result.stdout) <= 4000
    if kind == 'memory': assert result.returncode != 0
    if kind == 'pids': assert result.returncode == 0 and 'PID_LIMIT' in result.stdout
    listing = subprocess.run([*sandbox.DOCKER, 'ps', '-aq', '--filter', 'label=jarvis.sandbox=true'],
                             capture_output=True, text=True, check=True)
    assert not listing.stdout.strip()


@live
@pytest.mark.parametrize('failure_phase', [None, 'targeted', 'full_python', 'full_ui'])
def test_live_candidate_and_apply_through_docker(setup, monkeypatch, failure_phase):
    source, _, factory = setup
    original = RepositoryTools.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._bwrap = '/missing/bwrap'
    monkeypatch.setattr(RepositoryTools, '__init__', init)
    (source / 'tests/ui.test.cjs').write_text("require('node:test')('ok', () => {});\n")
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'UI fixture')
    candidate = start(factory(tester=None))
    assert candidate['state'] == 'passed', candidate
    assert len(candidate['test_results']) == 3
    assert all(t['sandbox']['backend'] == 'docker' and
               t['fingerprint'] == candidate['tested_fingerprint'] for t in candidate['test_results'])
    # Inject a failing test into only the verification snapshot. Tests still
    # execute through the real hardened Docker launcher; reviewed bytes and
    # the source checkout are untouched.
    run_tests = sandbox.run_tests
    phases = iter(['targeted', 'full_python', 'full_ui'])
    def verification_tests(repo, snapshot, paths, *, ui=False):
        phase = next(phases)
        if phase == failure_phase:
            target = snapshot / ('tests/ui.test.cjs' if ui else 'tests/test_app.py')
            target.chmod(0o644)
            target.write_text("require('node:test')('failure', () => { throw Error('verification regression'); });\n"
                              if ui else 'def test_failure(): assert False, "verification regression"\n')
            target.chmod(0o444)
        return run_tests(repo, snapshot, paths, ui=ui)
    monkeypatch.setattr(sandbox, 'run_tests', verification_tests)
    apply = CandidateApply(factory(tester=None))
    save = apply._save
    persisted = []
    def save_and_check(event):
        save(event)
        if event == 'post_apply_test_finished':
            audit = apply.store.get(candidate['id'], internal=True)['apply']
            assert audit['status'] == 'verifying'
            assert audit['verification'] == apply.record['apply']['verification']
            persisted.append(audit['verification'][-1])
    monkeypatch.setattr(apply, '_save', save_and_check)
    applied = apply.apply(candidate['id'],
                f"Approve and apply candidate {candidate['id']} to main")['apply']
    assert applied['status'] == ('rolled_back' if failure_phase else 'applied'), applied
    expected_count = ['targeted', 'full_python', 'full_ui'].index(failure_phase) + 1 if failure_phase else 3
    assert len(applied['verification']) == expected_count
    assert persisted == applied['verification']
    assert all(t['sandbox']['backend'] == 'docker' for t in applied['verification'])
    assert applied['post_apply_verified'] is (failure_phase is None)
    assert git(source, 'status', '--porcelain') == ''
    if failure_phase:
        assert applied['rollback'] and applied['commit'] is None
        assert applied['verification'][-1]['returncode'] != 0
        assert 'verification regression' in applied['verification'][-1]['stdout']
        assert git(source, 'rev-parse', 'HEAD').strip() == candidate['base_commit']
        assert (source / 'src/app.py').read_text() == 'value = 1\n'
    else:
        assert all(t['returncode'] == 0 for t in applied['verification'])
        assert git(source, 'rev-parse', 'HEAD').strip() == applied['commit']
    assert CandidateApply(factory(tester=None)).inspect(candidate['id'])['apply'] == applied


@live
def test_live_timer_cannot_be_suspended_and_survives_client_death(repository):
    repository._bwrap = '/missing/bwrap'
    _, image, _ = sandbox.select_backend(repository, repository.root)
    name = 'jarvis-sandbox-orphan-test-' + str(os.getpid())
    code = '''import os, signal, time
try:
 os.kill(1, signal.SIGSTOP)
except PermissionError:
 print('TIMER_PROTECTED', flush=True)
else:
 raise AssertionError('Timer could be suspended')
time.sleep(60)
'''
    argv = sandbox.docker_command(repository.root, image, name, 3) + ['/runtime/bin/python', '-c', code]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'TIMER_PROTECTED'
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            info = subprocess.run([*sandbox.DOCKER, 'inspect', name], capture_output=True)
            if info.returncode:
                break
            time.sleep(0.2)
        else:
            pytest.fail('Container survived its wall clock deadline after client death')
    finally:
        process.kill()
        process.communicate(timeout=5)
        subprocess.run([*sandbox.DOCKER, 'rm', '-f', name], capture_output=True)
