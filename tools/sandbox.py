"""Trusted test launchers. No agent-controlled executables, images or Docker options.

Docker is a development-only fallback; only a filtered snapshot is bind-mounted.
The local daemon and the separately built runtime image are trusted infrastructure.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from .policy import ToolDenied, ToolError

DOCKER = ('/usr/bin/docker', '--host=unix:///var/run/docker.sock')
IMAGE = 'jarvis-sandbox:local'
LIMITS = dict(cpus=1, memory_bytes=536870912, pids=128, tmp_bytes=268435456,
              output_bytes=1000000)
PROBE = '''import os
from pathlib import Path
import pytest
assert os.getuid() == 10001
s = Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in s and 'Seccomp:\t2' in s
c = Path('/sys/fs/cgroup')
assert (c / 'memory.max').read_text().strip() == '536870912'
assert (c / 'memory.swap.max').read_text().strip() == '0'
assert (c / 'pids.max').read_text().strip() == '128'
a, b = map(int, (c / 'cpu.max').read_text().split())
assert a == b
'''
ENV = {'PATH': '/runtime/bin:/usr/bin', 'HOME': '/tmp', 'LANG': 'C.UTF-8',
       'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'JARVIS_MODE': 'development',
       'JARVIS_DB_PATH': ':memory:'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def bwrap_command(repo, snapshot):
    return [repo._bwrap, '--unshare-all', '--die-with-parent', '--new-session',
            '--cap-drop', 'ALL', '--ro-bind', '/usr', '/usr',
            '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
            '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc',
            '--dev', '/dev', '--tmpfs', '/tmp',
            '--ro-bind', str(snapshot), '/workspace',
            '--ro-bind', sys.prefix, '/runtime', '--chdir', '/workspace',
            '--clearenv', *[item for k, v in ENV.items() for item in ('--setenv', k, v)]]


def docker_command(snapshot, image, name, seconds):
    # No contexts, user Docker config, socket mount, automatic pulls or volumes.
    # A fresh snapshot has no submounts; recursive binds are disabled explicitly.
    return [*DOCKER, 'run', '--rm', '--pull=never', '--name', name,
            '--label', 'jarvis.sandbox=true', '--restart=no', '--network=none',
            '--user=10001:10001', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges:true', '--pids-limit=128',
            '--cpus=1', '--memory=536870912', '--memory-swap=536870912',
            '--ulimit', 'nofile=256:256', '--ulimit', 'core=0:0',
            '--log-driver=none', '--no-healthcheck', '--ipc=none', '--cgroupns=private',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=268435456,mode=1777',
            '--mount', f'type=bind,src={snapshot},dst=/workspace,readonly,bind-recursive=disabled',
            '--workdir=/workspace', '--entrypoint=/usr/bin/env',
            image, '-i',
            *[f'{k}={v}' for k, v in ENV.items()],
            '/runtime/bin/python', '-I', '-B', '/opt/jarvis/supervisor.py', str(seconds),
            '/runtime/bin/python', '-I', '-B', '/opt/jarvis/guard.py']


def docker_execute(repo, snapshot, image, command, seconds):
    name = 'jarvis-sandbox-' + uuid4().hex
    started = time.monotonic()
    try:
        result = repo._execute(docker_command(snapshot, image, name, seconds) + command,
                               timeout=seconds)
        # The protected supervisor is PID 1; its exit tears down all
        # descendants even if the client/worker disappears. Host cleanup also
        # handles output truncation, daemon errors and normal completion.
        return replace(result, timed_out=result.timed_out or (result.returncode in (124, 137) and
                                  time.monotonic() - started >= seconds))
    finally:
        # Bypass CandidateRepository's exhausted operation deadline for cleanup.
        from .repository import RepositoryTools
        cleanup = RepositoryTools._execute(repo, [*DOCKER, 'rm', '--force', name], timeout=10)
        if cleanup.returncode and 'No such container' not in cleanup.stderr:
            raise ToolDenied('Docker sandbox cleanup could not be confirmed')


def select_backend(repo, snapshot):
    reasons = []
    if not getattr(repo, 'requires_docker', False) and Path(repo._bwrap).is_file():
        try:
            probe = repo._execute(bwrap_command(repo, snapshot) + ['/usr/bin/true'], timeout=5)
            if not (probe.returncode or probe.timed_out or probe.truncated):
                return 'bubblewrap', None, reasons
            reasons.append('Bubblewrap functional probe failed')
        except ToolError:
            reasons.append('Bubblewrap probe unavailable')
    else:
        reasons.append('Bubblewrap executable unavailable')
    if os.environ.get('JARVIS_MODE', 'development') == 'production':
        raise ToolDenied('Bubblewrap unavailable; Docker fallback disabled in production')
    try:
        inspected = repo._execute([*DOCKER, 'image', 'inspect', IMAGE], timeout=5)
        if inspected.returncode or inspected.timed_out or inspected.truncated:
            raise ToolDenied('Docker runtime image unavailable')
        info = json.loads(inspected.stdout)[0]
        image = info['Id']
        if not image.startswith('sha256:') or info['Config'].get('Volumes'):
            raise ToolDenied('Docker runtime must have no implicit volumes')
        # Probe the actual hardened configuration, not just CLI/daemon existence.
        probe = docker_execute(repo, snapshot, image,
                               ['/runtime/bin/python', '-I', '-c',
                                PROBE], 10)
        if probe.returncode or probe.timed_out or probe.truncated:
            raise ToolDenied('Docker hardened runtime probe failed')
        return 'docker', image, reasons
    except (ToolError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise ToolDenied('No usable sandbox: Bubblewrap and Docker unavailable; host execution disabled') from exc


def run_tests(repo, snapshot, paths, *, ui=False):
    started = stamp()
    deadline = time.monotonic() + repo.pytest_timeout
    backend, image, reasons = select_backend(repo, snapshot)
    seconds = deadline - time.monotonic()
    if hasattr(repo, 'check'):
        seconds = min(seconds, repo.check())
    if seconds <= 0:
        raise ToolDenied('Sandbox setup exhausted test deadline')
    command = (['/usr/bin/node', '--test', 'tests/ui.test.cjs'] if ui else
               ['/runtime/bin/python', '-I', '-B', '-m', 'pytest', '-p', 'no:cacheprovider',
                '-c', '/dev/null', '--rootdir=/workspace', '-o', 'pythonpath=/workspace', '--', *paths])
    if backend == 'docker':
        result = docker_execute(repo, snapshot, image, command, seconds)
        limits = dict(LIMITS)
    else:
        result = repo._execute(bwrap_command(repo, snapshot) + command, timeout=seconds)
        # Existing Bubblewrap isolation and process-group wall/output bounds.
        limits = dict(cpus=None, memory_bytes=None, pids=None, tmp_bytes=None)
    limits.update(wall_seconds=seconds, output_bytes=repo.max_output_bytes)
    audit = dict(backend=backend, started_at=started, ended_at=stamp(), limits=limits,
                 image=image, selection=reasons, returncode=result.returncode,
                 timed_out=result.timed_out, truncated=result.truncated)
    return replace(result, sandbox=audit)
