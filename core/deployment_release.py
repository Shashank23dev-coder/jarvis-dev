"""Host-only release controller. Git objects, durable journal, exact authorization.

No runtime paths, commands, credentials or approval text come from model arguments.
Production has neither the opt-in configuration nor the Docker socket.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from uuid import uuid4

from tools.policy import ToolDenied

SHA = re.compile(r'[0-9a-f]{40}')
TERMINAL = {'deployed', 'rolled_back', 'failed', 'rollback_failed'}

# Three fixed batches, balanced by measured in-sandbox durations, keep each
# Python run under the existing 600 s deadline. Inventory changes fail closed.
PYTHON_SHARDS = (
    ('tests/test_assistant_experience.py', 'tests/test_self_edit.py',
     'tests/test_candidate_bridge.py', 'tests/test_brain_status.py',
     'tests/test_authority_model.py', 'tests/test_engineering_providers.py'),
    ('tests/test_candidate_apply.py', 'tests/test_engineering.py',
     'tests/test_deployment_release.py', 'tests/test_candidate_tools.py',
     'tests/test_engineering_authority.py', 'tests/test_engineering_operator.py', 'tests/test_experience.py',
     'tests/test_sandbox_docker.py', 'tests/test_sandbox_imports.py'),
    ('tests/test_owner_readonly_flow.py', 'tests/test_engineering_jobs.py',
     'tests/test_project_repository.py', 'tests/test_authority_acceptance.py',
     'tests/test_tools.py', 'tests/test_recall.py',
     'tests/test_portfolio_integration.py', 'tests/test_openai_client.py',
     'tests/test_engineering_action.py', 'tests/test_persistence.py',
     'tests/test_deployment.py', 'tests/test_brain.py', 'tests/test_agent.py',
     'tests/test_experience_evaluation.py', 'tests/test_deployment_status.py',
     'tests/test_self_state.py', 'tests/test_api.py',
     'tests/test_engineering_portfolio.py', 'tests/test_registry.py',
     'tests/test_direct_dev.py'),
)


def python_shards(root):
    assigned = [path for shard in PYTHON_SHARDS for path in shard]
    discovered = {path.relative_to(root).as_posix()
                  for pattern in ('test_*.py', '*_test.py')
                  for path in (root / 'tests').rglob(pattern)}
    if len(assigned) != len(set(assigned)) or sorted(assigned) != sorted(discovered):
        raise ToolDenied('Release Python test inventory changed; assign every test file to one shard')
    return PYTHON_SHARDS

# Test fixtures construct secret markers at runtime; production context has no fixture exemptions.


def now():
    return datetime.now(timezone.utc).isoformat()


def approval(sha):
    return f'Approve and deploy commit {sha} to production'


def approved_commit(message):
    match = re.fullmatch(r'Approve and deploy commit ([0-9a-f]{40}) to production', message or '')
    return match[1] if match else None


class ReleaseConfig:
    def __init__(self, source, journal, env_file, container='jarvis-jarvis-1', volume='jarvis_state'):
        self.source, self.journal, self.env_file = map(Path, (source, journal, env_file))
        self.container, self.volume = container, volume
        for path in (self.source, self.journal, self.env_file):
            if not path.is_absolute() or path.resolve() != path:
                raise ToolDenied('Deployment paths must be absolute and non-symlinked')
        if self.journal.is_relative_to(self.source) or self.env_file.is_relative_to(self.source):
            raise ToolDenied('Deployment journal and production secrets must be outside source')
        if not all(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', s) for s in (container, volume)):
            raise ToolDenied('Invalid fixed deployment target')

    @classmethod
    def configured(cls):
        if os.environ.get('JARVIS_MODE', 'development') != 'development':
            raise ToolDenied('Deployment controller is development-only')
        name = os.environ.get('JARVIS_DEPLOY_CONFIG')
        if not name:
            raise ToolDenied('Deployment controller is not configured')
        path = Path(name)
        if (not path.is_absolute() or path.resolve() != path or not path.is_file() or
                path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
            raise ToolDenied('Deployment config must be a private absolute regular file')
        return cls(**json.loads(path.read_text()))


class ReleaseService:
    # True only for the configured deployment controller, whose journal deploy reads.
    controller = True

    def __init__(self, config, runtime=None):
        self.config = config
        if runtime is None:
            from .deployment_runtime import DockerRuntime
            runtime = DockerRuntime(config)
        self.runtime = runtime

    def git(self, *args):
        result = subprocess.run(['/usr/bin/git', '--no-optional-locks', '--no-replace-objects', '-c', 'core.fsmonitor=false',
                                 '-c', 'core.hooksPath=/dev/null', '-C', str(self.config.source), *args],
                                env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
                                     'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'},
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        if result.returncode or len(result.stdout) > 50_000_000:
            raise ToolDenied('Release Git validation failed')
        return result.stdout

    def clean(self, sha):
        if not SHA.fullmatch(sha):
            raise ToolDenied('A full lowercase commit SHA is required')
        if self.git('rev-parse', '--verify', sha + '^{commit}').decode().strip() != sha:
            raise ToolDenied('Commit does not exist')
        if self.git('rev-parse', 'HEAD').decode().strip() != sha:
            raise ToolDenied('Approval is stale: commit must be current HEAD')
        if self.git('status', '--porcelain=v1', '--untracked-files=all'):
            raise ToolDenied('Deployment requires a clean source checkout')
        if any(not entry.startswith(b'H ') for entry in self.git('ls-files', '-v', '-z').split(b'\0') if entry):
            raise ToolDenied('Hidden index changes or sparse checkout cannot authorize deployment')

    @contextmanager
    def lock(self):
        root = self.config.journal
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.resolve() != root or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise ToolDenied('Deployment journal must be private and operator-owned')
        fd = os.open(root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ToolDenied('Another deployment or validation is active') from None
            yield
        finally:
            os.close(fd)

    def load(self):
        path = self.config.journal / 'releases.json'
        return json.loads(path.read_text()) if path.exists() else {'records': [], 'evidence': {}}

    def save(self, data):
        with tempfile.NamedTemporaryFile(mode='w', dir=self.config.journal, delete=False) as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stream.name, self.config.journal / 'releases.json')
        fd = os.open(self.config.journal, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def event(self, data, record, status):
        record['status'] = status
        record['updated_at'] = now()
        record['events'].append({'status': status, 'at': record['updated_at']})
        if status in TERMINAL:
            record['completed_at'] = record['updated_at']
        self.save(data)

        # A sanitized informational copy is the only release data production can
        # read. Publication must never alter transaction/rollback decisions.
        if hasattr(self.runtime, 'publish_status'):
            try:
                from .deployment_status import projection
                self.runtime.publish_status(projection(self.runtime.current(), data['records'], now()))
                record['status_publication_failed'] = False
            except Exception:
                record['status_publication_failed'] = True
            self.save(data)

    def history(self):
        return self.load()['records'][-50:][::-1]

    def inspect(self, identifier):
        for record in self.load()['records']:
            if record['id'] == identifier:
                return record
        raise ToolDenied('Deployment not found')

    def snapshot(self, sha, destination):
        """Export regular allowlisted Git blobs, never working-tree/archive filters."""
        roots = {'core', 'models', 'agents', 'evaluator', 'tools', 'static', 'tests', 'docs'}
        files = {'main.py', 'Dockerfile', 'compose.yaml', '.dockerignore', 'requirements.txt'}
        total = 0
        for entry in self.git('ls-tree', '-rz', '--full-tree', sha).split(b'\0'):
            if not entry:
                continue
            meta, raw_path = entry.split(b'\t', 1)
            mode, kind, blob = meta.decode().split()
            path = raw_path.decode('utf-8')
            parts = Path(path).parts
            if parts[0] not in roots and path not in files:
                continue
            if (not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in parts or
                    any(p.startswith('.') for p in parts) and path != '.dockerignore' or
                    any(re.search(r'(?i)secret|credential|password|private.key|\.sqlite|\.db|\.pem|\.key|\.p12|\.pfx|\.kdbx', p)
                        for p in parts) or any(p in {'state', 'backups', '__pycache__', 'id_rsa', 'id_dsa',
                                                    'id_ecdsa', 'id_ed25519'} for p in parts)):
                continue
            if kind != 'blob' or mode not in {'100644', '100755'}:
                raise ToolDenied('Release context cannot contain links or submodules')
            content = self.git('cat-file', 'blob', blob)
            if re.search(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----', content):
                raise ToolDenied('Private key material is forbidden in release context')
            total += len(content)
            if len(content) > 1_000_000 or total > 50_000_000:
                raise ToolDenied('Release context exceeds size limit')
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o755 if mode == '100755' else 0o644)
        if not all((destination / path).is_file() for path in files):
            raise ToolDenied('Required release files are missing')

    def validate(self, sha):
        """Trusted CLI only: run complete suites in existing isolated test backend."""
        from tools import RepositoryTools
        from tools.sandbox import run_tests
        with self.lock():
            self.clean(sha)
            data = self.load()
            if any(r['status'] not in TERMINAL for r in data['records']):
                raise ToolDenied('Recover the interrupted deployment before validation')
            data['evidence'].pop(sha, None)
            self.save(data)  # A failed rerun must not leave earlier passing evidence.
            with tempfile.TemporaryDirectory(prefix='jarvis-release-validation-') as directory:
                root = Path(directory) / 'source'
                root.mkdir(mode=0o755)
                self.snapshot(sha, root)
                results = []
                repository = RepositoryTools(root, pytest_timeout=600)
                try:
                    shards = python_shards(root)
                except ToolDenied:
                    shards = ()
                    results.append(dict(phase='python', shard='inventory', paths=[], passed=False,
                                        returncode=None, sandbox=None, summary=[],
                                        failure_kind='test_inventory_changed'))
                phases = [(f'python-{number}', list(paths), False)
                          for number, paths in enumerate(shards, 1)]
                phases.append(('ui', ['tests/ui.test.cjs'], True))
                for shard, paths, ui in phases:
                    from tools.policy import ToolError
                    started = time.monotonic()
                    try:
                        result = run_tests(repository, root, paths, ui=ui)
                    except (ToolError, OSError):
                        # Context security checks above have already passed.
                        # An unavailable test backend is validation evidence,
                        # never a claim that the production source is unsafe.
                        results.append(dict(phase='ui' if ui else 'python', shard=shard,
                            paths=paths, elapsed_seconds=round(time.monotonic() - started, 2),
                            passed=False, returncode=None, sandbox=None, summary=[],
                            failure_kind='test_environment'))
                        continue
                    summary = re.findall(r'(?:\d+ (?:passed|failed|skipped|errors?)\b|# (?:tests|pass|fail|skipped) \d+)', result.stdout)
                    failures = re.findall(r'(?m)^(?:FAILED|ERROR) (tests/[A-Za-z0-9_./:-]+)', result.stdout)
                    results.append(dict(phase='ui' if ui else 'python', shard=shard,
                                        paths=paths, elapsed_seconds=round(time.monotonic() - started, 2),
                                        failures=failures[:20],
                                        passed=not (result.returncode or result.timed_out or result.truncated),
                                        returncode=result.returncode, sandbox=result.sandbox,
                                        summary=summary[-8:],
                                        failure_kind=('environment_timeout' if result.timed_out else
                                            'environment_output_limit' if result.truncated else
                                            'test_fixture_failure' if re.search(r'ERROR at (?:setup|teardown)', result.stdout) else
                                            'test_failure' if result.returncode == 1 else
                                            'test_environment' if result.returncode else None)))
                self.clean(sha)
                data = self.load()
                evidence = dict(commit=sha, at=now(), tests=results,
                                review='Automated context policy checked; human source review required before approval',
                                passed=all(r['passed'] for r in results))
                data['evidence'][sha] = evidence
                self.save(data)
                return evidence

    def prepare(self):
        """Preparation is autonomous; existing exact-commit evidence is reusable."""
        sha = self.git('rev-parse', 'HEAD').decode().strip()
        self.clean(sha)
        evidence = self.load()['evidence'].get(sha)
        if not (evidence and evidence.get('passed') and
                0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(evidence['at'])).total_seconds() < 86400):
            evidence = self.validate(sha)
        # Private preparation evidence is not the controller journal that deploy
        # reads, so it never offers the approval phrase as if deploy were ready.
        ready = bool(evidence['passed'] and self.controller)
        return dict(commit=sha, prepared=evidence['passed'], evidence=evidence,
            controller_validated=ready, required_confirmation=approval(sha) if ready else None)

    def preview(self, sha):
        self.clean(sha)
        current = self.runtime.current()
        previous = current.get('revision')
        changed = None
        if previous and SHA.fullmatch(previous):
            try:
                changed = self.git('diff', '--name-only', previous, sha, '--').decode().splitlines()
            except ToolDenied:
                pass
        data = self.load()
        evidence = data['evidence'].get(sha)
        fresh = bool(evidence and evidence['passed'] and
                     0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(evidence['at'])).total_seconds() < 86400)
        result = dict(commit=sha, message=self.git('show', '-s', '--format=%B', sha).decode().strip(),
                    previous=current, changed_files=changed, evidence=evidence, eligible=fresh,
                    image_tag='jarvis-release:' + sha, image=self.runtime.image(sha),
                    required_confirmation=approval(sha),
                    note='Requires fresh validation and human review. Unknown baseline means changed_files is null.')
        return self.runtime.redact(result)

    def deploy(self, sha, current_message):
        if approved_commit(current_message) != sha:
            from .engineering_authority import assess_action, EscalationRequired
            raise EscalationRequired(assess_action('production_deploy'))
        with self.lock():
            data = self.load()
            pending = [r for r in data['records'] if r['status'] not in TERMINAL]
            if pending:
                record = pending[-1]
                if record['commit'] != sha:
                    raise ToolDenied('Interrupted deployment requires approval of its original commit for recovery')
                self.rollback(data, record)
                return record
            if any(r['status'] == 'rollback_failed' for r in data['records'][-1:]):
                raise ToolDenied('Rollback failed; operator reconciliation required')
            self.clean(sha)
            preview = self.preview(sha)
            for old in reversed(data['records']):
                if old['commit'] == sha and old['status'] == 'deployed' and preview['previous']['image'] == old['image']:
                    self.runtime.verify(self.config.container, 8000, public=True)
                    return old
            if not preview['eligible']:
                raise ToolDenied('Fresh passing validation is required before deployment')
            record = dict(id=uuid4().hex, commit=sha, approval=current_message, requested_at=now(),
                          previous=preview['previous'], image=None, evidence=preview['evidence'],
                          image_tag=preview['image_tag'], message=preview['message'],
                          changed_files=preview['changed_files'], human_review='Approved in current message',
                          verification=[], rollback=None, events=[], switched=False)
            data['records'].append(record)
            self.event(data, record, 'building')
            try:
                with tempfile.TemporaryDirectory(prefix='jarvis-release-build-') as directory:
                    self.snapshot(sha, Path(directory))
                    record['database_hash'] = hashlib.sha256((Path(directory) / 'core/database.py').read_bytes()).hexdigest()
                    record['image'] = self.runtime.build(sha, Path(directory))
                self.event(data, record, 'prechecking')
                self.clean(sha)
                self.runtime.assert_current(record['previous'])
                baseline = self.runtime.verify(self.config.container, 8000, public=True)
                record['baseline'] = baseline
                record['verification'].append(dict(phase='previous', passed=True, at=now()))
                self.runtime.precheck(record)
                record['verification'].append(dict(phase='pre_switch', passed=True, at=now()))
                self.clean(sha)
                self.runtime.assert_current(record['previous'])
                # Journal intent before the first production mutation.
                record['switched'] = True
                self.event(data, record, 'switching')
                self.runtime.switch(record)
                self.event(data, record, 'verifying')
                self.runtime.verify(self.config.container, 8000, public=True, baseline=baseline)
                record['verification'].append(dict(phase='post_switch', passed=True, at=now()))
                self.event(data, record, 'deployed')
            except Exception:
                record['verification'].append(dict(phase=record['status'], passed=False, at=now()))
                record['error'] = 'Deployment verification or operation failed; raw diagnostics withheld'
                self.rollback(data, record)
            return record

    def rollback(self, data, record):
        if not record['switched']:
            try:
                self.runtime.cleanup(record)
            except Exception:
                record['cleanup_failed'] = True
            self.event(data, record, 'failed')
            return
        self.event(data, record, 'rolling_back')
        try:
            self.runtime.restore(record)
            self.runtime.verify(self.config.container, 8000, public=True, baseline=record.get('baseline'))
            record['rollback'] = dict(verified=True, image=record['previous']['image'],
                                      revision=record['previous'].get('revision'), at=now())
            self.event(data, record, 'rolled_back')
        except Exception:
            record['rollback'] = dict(verified=False, at=now(), error='Operator reconciliation required')
            self.event(data, record, 'rollback_failed')


def preparation_service(*, source=None, state=None):
    """Source validation needs no production credentials or runtime access."""
    if os.environ.get('JARVIS_DEPLOY_CONFIG'):
        return configured_service()
    from .candidate_broker import SOURCE, STATE
    service = ReleaseService(ReleaseConfig(source or SOURCE,
        (state or STATE) / 'release-preparation', (state or STATE) / 'production.env'))
    service.controller = False  # Private journal; the controller re-validates.
    return service


def configured_service():
    return ReleaseService(ReleaseConfig.configured())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate', 'preview', 'deploy', 'inspect', 'history'])
    parser.add_argument('identifier', nargs='?')
    args = parser.parse_args()
    try:
        service = configured_service()
        if args.action == 'deploy':
            result = service.deploy(args.identifier, input('Exact approval: '))
        elif args.action == 'history':
            result = service.history()
        else:
            result = getattr(service, args.action)(args.identifier)
        print(json.dumps(result, indent=2))
    except Exception:
        parser.exit(1, 'Deployment operation denied or failed; no raw diagnostics emitted.\n')


if __name__ == '__main__':
    main()
