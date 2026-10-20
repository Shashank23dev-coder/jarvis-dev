"""Release browser check: headless Chromium against the exact release image.

Host-only, called by release validation (and an operator CLI for proofs).
- The app runs from the release image in production mode with throwaway
  secrets, a throwaway state volume and --network none (no egress, no ports).
  Only the OpenAI client is stubbed (core/release_browser/boot.py).
- Chromium runs in a pinned Playwright container that joins the app's network
  namespace, and a per-run TLS relay there serves https://localhost:8443 (like Caddy).
  Nothing is published on the host.
- Names are unique per run (jarvis-bcheck-*); production names are never used.
- Screenshots and console logs go to the artifact directory; everything else
  (containers, volume, temp files) is removed afterwards, pass or fail.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

DOCKER = ['/usr/bin/docker', '--host=unix:///var/run/docker.sock']
# Base: Playwright 1.48.0 image (tag v1.48.0-jammy) pinned by digest in runner.Dockerfile,
# plus playwright==1.48.0. The local tag is content-addressed by that Dockerfile.
RUNNER_BASE = ('mcr.microsoft.com/playwright/python@'
               'sha256:6bbd515848db4042068571135979b6ee6d330a794b28e037e3a6ccd7f2abfa26')
RUNNER_DOCKERFILE = Path(__file__).resolve().parent / 'release_browser' / 'runner.Dockerfile'
RUNNER_IMAGE = 'jarvis-bcheck-runner:' + hashlib.sha256(RUNNER_DOCKERFILE.read_bytes()).hexdigest()[:16]
BUDGET_SECONDS = 120
ORIGIN = 'https://localhost:8443'  # TLS relay inside the runner, like Caddy in production
FIXED_ENV = dict(JARVIS_MODE='production', JARVIS_HOST='0.0.0.0', JARVIS_PORT='8000',
                 JARVIS_STATE_DIR='/var/lib/jarvis', JARVIS_DB_PATH='/var/lib/jarvis/jarvis.sqlite3')
FILES = Path(__file__).resolve().parent / 'release_browser'
NAME = re.compile(r'jarvis-bcheck-(?:app|run|state)-[0-9a-f]{12}')
PROTECTED = {'jarvis-jarvis-1', 'jarvis_state', 'jarvis'}


def docker(*args, timeout=30, optional=False):
    with tempfile.TemporaryDirectory(prefix='jarvis-bcheck-home-') as home:
        try:
            result = subprocess.run([*DOCKER, *args], stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=timeout, env={'PATH': '/usr/bin:/bin', 'HOME': home})
        except subprocess.TimeoutExpired:
            if optional:
                return None
            raise RuntimeError('Docker browser-check operation timed out') from None
    if result.returncode:
        if optional:
            return None
        raise RuntimeError('Docker browser-check operation failed')
    return result.stdout.decode()


def throwaway_names(run_id):
    names = dict(app=f'jarvis-bcheck-app-{run_id}', runner=f'jarvis-bcheck-run-{run_id}',
                 volume=f'jarvis-bcheck-state-{run_id}')
    if any(not NAME.fullmatch(n) or n in PROTECTED for n in names.values()):
        raise RuntimeError('Unsafe browser-check name')
    return names


def write_env(path, values):
    for key, value in values.items():
        if not re.fullmatch(r'[A-Za-z0-9_.:/=+-]+', value):
            raise RuntimeError('Invalid throwaway environment value')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(''.join(f'{k}={v}\n' for k, v in values.items()))


class BrowserCheck:
    def __init__(self, docker=docker, runner_image=RUNNER_IMAGE, budget=BUDGET_SECONDS, clock=time.monotonic):
        self.docker, self.runner_image, self.budget, self.clock = docker, runner_image, budget, clock

    def ensure_runner(self):
        # Pulling the pinned runner happens before (and outside) the time budget.
        if self.docker('image', 'inspect', self.runner_image, optional=True) is None:
            with tempfile.TemporaryDirectory(prefix='jarvis-bcheck-build-') as context:
                shutil.copyfile(RUNNER_DOCKERFILE, Path(context) / 'Dockerfile')
                self.docker('build', '--pull=false', '--tag', self.runner_image, context, timeout=900)

    def app_args(self, names, image, code, env_file, run_id):
        args = ['run', '-d', '--pull=never', '--name', names['app'], '--label', 'jarvis.bcheck=' + run_id,
                '--network', 'none', '--user=10001:10001', '--init', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges:true', '--memory=1g', '--memory-swap=1g', '--cpus=2',
                '--pids-limit=256', '--ipc=private', '--restart=no',
                '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777',
                '--mount', f'type=volume,src={names["volume"]},dst=/var/lib/jarvis',
                '--mount', f'type=bind,src={code},dst=/bcheck,readonly', '--env-file', str(env_file)]
        for key, value in FIXED_ENV.items():
            args += ['--env', f'{key}={value}']
        return args + ['--entrypoint', 'python', image, '/bcheck/boot.py']

    def runner_args(self, names, code, out, env_file, run_id, deadline):
        return ['run', '--pull=never', '--name', names['runner'], '--label', 'jarvis.bcheck=' + run_id,
                '--network', 'container:' + names['app'], f'--user={os.getuid()}:{os.getgid()}',
                '--init', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                '--memory=2g', '--memory-swap=2g', '--cpus=2', '--pids-limit=512', '--shm-size=512m',
                '--restart=no', '--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777',
                '--mount', f'type=bind,src={code},dst=/bcheck,readonly',
                '--mount', f'type=bind,src={out},dst=/out', '--env-file', str(env_file),
                '--env', 'HOME=/tmp', '--env', f'BCHECK_DEADLINE={deadline:.0f}',
                self.runner_image, 'python', '/bcheck/runner.py']

    def teardown(self, names, run_id):
        clean = True
        for key in ('runner', 'app'):
            info = self.docker('container', 'inspect', names[key], optional=True)
            if info and json.loads(info)[0]['Config']['Labels'].get('jarvis.bcheck') == run_id:
                clean &= self.docker('rm', '--force', '--volumes', names[key], timeout=60, optional=True) is not None
        info = self.docker('volume', 'inspect', names['volume'], optional=True)
        if info and (json.loads(info)[0].get('Labels') or {}).get('jarvis.bcheck') == run_id:
            clean &= self.docker('volume', 'rm', names['volume'], optional=True) is not None
        return clean

    def run(self, image, artifacts):
        """Return JSON-safe evidence. Never raises for check failures."""
        artifacts = Path(artifacts)
        artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
        run_id = uuid4().hex[:12]
        names = throwaway_names(run_id)
        evidence = dict(phase='browser', shard='browser', image=image, runner_image=self.runner_image,
                        artifact_dir=str(artifacts), budget_seconds=self.budget, passed=False,
                        failures=[], failure_kind=None)
        try:
            self.ensure_runner()
        except Exception:
            evidence.update(failure_kind='browser_environment', failures=['runner image unavailable'])
            return evidence
        started = self.clock()
        result = None
        try:
            with tempfile.TemporaryDirectory(prefix='jarvis-bcheck-') as directory:
                code = Path(directory) / 'code'
                code.mkdir(mode=0o755)
                os.chmod(directory, 0o711)
                for name in ('boot.py', 'runner.py'):
                    shutil.copyfile(FILES / name, code / name)
                    os.chmod(code / name, 0o644)
                username = 'owner-' + secrets.token_hex(4)
                password = secrets.token_urlsafe(24)
                token = secrets.token_urlsafe(48)
                write_env(Path(directory) / 'app.env', dict(
                    JARVIS_AUTH_TOKEN=token, OPENAI_API_KEY='sk-bcheck-' + secrets.token_hex(8),
                    JARVIS_PUBLIC_ORIGIN=ORIGIN, BCHECK_USERNAME=username, BCHECK_PASSWORD=password))
                write_env(Path(directory) / 'runner.env', dict(
                    JARVIS_AUTH_TOKEN=token, BCHECK_USERNAME=username, BCHECK_PASSWORD=password))
                try:
                    self.docker('volume', 'create', '--label', 'jarvis.bcheck=' + run_id, names['volume'])
                    self.docker(*self.app_args(names, image, code, Path(directory) / 'app.env', run_id), timeout=60)
                    remaining = self.budget - (self.clock() - started) - 10
                    if remaining <= 0:
                        raise TimeoutError
                    self.docker(*self.runner_args(names, code, artifacts, Path(directory) / 'runner.env',
                                                  run_id, remaining), timeout=remaining + 5, optional=True)
                    logs = self.docker('logs', '--tail', '400', names['app'], optional=True)
                    if logs is not None:
                        (artifacts / 'app-container.log').write_text(logs[-200_000:].replace(token, '[redacted]'))
                finally:
                    evidence['teardown_clean'] = self.teardown(names, run_id)
        except Exception as exc:
            evidence['failures'].append('harness:' + type(exc).__name__)
        evidence['elapsed_seconds'] = round(self.clock() - started, 2)
        path = artifacts / 'result.json'
        if path.is_file():
            try:
                result = json.loads(path.read_text())
            except ValueError:
                result = None
        if result:
            evidence.update(viewports=result.get('viewports', {}), seed=result.get('seed'),
                            browser_version=result.get('browser_version'))
            evidence['failures'] += result.get('failures', [])
        elif not evidence['failures']:
            evidence['failures'].append('runner produced no result')
        if evidence['elapsed_seconds'] > self.budget:
            evidence['failures'].append(f'budget exceeded: {evidence["elapsed_seconds"]}s > {self.budget}s')
        if not evidence.get('teardown_clean'):
            evidence['failures'].append('teardown incomplete')
        evidence['passed'] = bool(result and result.get('passed') and not evidence['failures'])
        if not evidence['passed']:
            evidence['failure_kind'] = ('browser_budget_exceeded' if evidence['elapsed_seconds'] > self.budget else
                                        'browser_check_failure' if result else 'browser_environment')
        evidence['screenshots'] = sorted(p.name for p in artifacts.glob('*.png'))
        evidence['console_logs'] = sorted(p.name for p in artifacts.glob('*-console.json'))
        return evidence


def main():
    parser = argparse.ArgumentParser(description='Operator proof run of the release browser check.')
    parser.add_argument('commit', help='full SHA whose jarvis-release:<sha> image already exists')
    parser.add_argument('--out', required=True, help='new artifact directory')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.exit(2, 'A full lowercase commit SHA is required\n')
    info = docker('image', 'inspect', 'jarvis-release:' + args.commit, optional=True)
    if not info:  # e.g. a retained rollback image; its revision label must match exactly
        ids = (docker('image', 'ls', '-q', '--no-trunc', '--filter',
                      'label=org.opencontainers.image.revision=' + args.commit, optional=True) or '').split()
        info = docker('image', 'inspect', ids[0], optional=True) if ids else None
    if not info:
        parser.exit(2, 'Release image not found\n')
    evidence = BrowserCheck().run(json.loads(info)[0]['Id'], Path(args.out).resolve())
    print(json.dumps(evidence, indent=2))
    raise SystemExit(0 if evidence['passed'] else 1)


if __name__ == '__main__':
    main()
