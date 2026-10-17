"""Explicit human approval, exact-patch application and durable rollback journal."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import stat
import time

from .database import timestamp, valid_id
from .self_edit import CandidateRepository, GIT_OPTIONS
from tools.candidate_edit import CandidatePolicy, digest
from tools.policy import ToolDenied
from tools.repository import RepositoryTools


APPROVAL = re.compile(r'^(?:Please )?approve and apply candidate ([0-9a-f-]{36}) to main[.!]?$', re.I)


def approved_id(request):
    if not isinstance(request, str):
        return None
    match = APPROVAL.fullmatch(request.strip())
    return match[1] if match and valid_id(match[1]) else None


def require_candidate_id(request, candidate_id):
    from .database import valid_id
    ids = set(re.findall(r'(?<![\w-])[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}(?![\w-])', request or '', re.I))
    if not valid_id(candidate_id) or {i.lower() for i in ids} != {candidate_id}:
        raise ToolDenied('Name the exact candidate ID in the current request', reason='candidate_id_required')


class CandidateApply:
    """No model-supplied roots, commands, patches, budgets or approval strings.

    Approval is supplied by the HTTP request or the current user task, never by
    model tool arguments. Journal lives in the existing durable candidate record.
    """
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.source = service.source

    def _setup(self, candidate_id, *, read_only=False):
        record = self.store.get(candidate_id, internal=True, recover=not read_only)
        if record is None:
            raise ValueError('Candidate not found')
        if self.store.database.path == ':memory:' or record.get('source_root') != str(self.service.source):
            raise ToolDenied('Apply requires durable storage and the original source checkout')
        self.record = record
        self.service.record = record
        self.service.limits = self.service._effective_limits(record)
        self.started = time.monotonic()
        self.service.deadline = self.started + max(0, self.service.limits.seconds - record['elapsed_seconds'])
        self.service.originals = {}
        return record

    def _project(self):
        """The candidate's registered project from host Portfolio metadata."""
        from .engineering_portfolio import Portfolio
        projects = Portfolio(self.store.database).projects()
        pid = self.record.get('source_context', {}).get('project_id')
        if pid is None:
            # Legacy records: only a unique registered root identifies the project.
            matches = [p for p in projects if p.get('repository') and
                       Path(p['repository']).resolve() == self.service.source]
            return matches[0] if len(matches) == 1 else None
        return next((p for p in projects if p['project_id'] == pid), None)

    def _read_only(self):
        # Unresolvable projects are treated as read-only: escalate, never assume writable.
        project = self._project()
        return project is None or bool(project.get('read_only'))

    def _target(self):
        """Only host Portfolio metadata selects an integration destination.

        The executing checkout is never an apply target, even with approval.
        Promotion is a separate owner operation, with no executor here.
        """
        from .candidate_broker import SOURCE
        from .engineering_portfolio import Portfolio
        projects = Portfolio(self.store.database).projects()
        live = {SOURCE.resolve(), Path(__file__).resolve().parents[1], self.service.source}
        jarvis = next(p for p in projects if p['project_id'] == 'jarvis-core')
        if jarvis.get('repository'):
            live.add(Path(jarvis['repository']).resolve())
        pid = self.record.get('source_context', {}).get('project_id')
        is_jarvis = (pid == 'jarvis-core' or self.service.source in
            {SOURCE.resolve(), Path(__file__).resolve().parents[1]} or
            bool(jarvis.get('repository') and self.service.source == Path(jarvis['repository']).resolve()))
        self.target_branch = None
        if is_jarvis:
            root, branch = jarvis.get('integration_repository'), jarvis.get('integration_branch')
            if not root or not branch:
                raise ToolDenied('Integration is not configured', reason='integration_not_configured')
            target = Path(root)
            if (not target.is_absolute() or target.resolve(strict=True) != target or
                    not re.fullmatch(r'jarvis/integration(?:[-/][a-zA-Z0-9_-]+)*', branch)):
                raise ToolDenied('Unsafe integration target')
            forbidden = live | {Path(p['repository']).resolve() for p in projects if p.get('repository')}
            if any(target == path or target.is_relative_to(path) or path.is_relative_to(target) for path in forbidden):
                raise ToolDenied('Integration cannot target an executing or registered project checkout')
            self.source, self.target_branch = target, 'refs/heads/' + branch
        else:
            # Other projects apply only into their own registered root, never
            # into Jarvis's executing checkouts or another project's root.
            project, source = self._project(), self.service.source
            others = (live - {source}) | {Path(p['repository']).resolve() for p in projects
                if p.get('repository') and p['project_id'] != (project or {}).get('project_id')}
            if (project is None or project['project_id'] == 'jarvis-core' or not project.get('repository')
                    or Path(project['repository']).resolve() != source
                    or any(source == path or source.is_relative_to(path) or path.is_relative_to(source) for path in others)):
                raise ToolDenied('Apply target must be the registered project root', reason='unknown_project')
            self.source = source
        audit = self.record.get('apply')
        if audit and audit.get('target_root', str(self.service.source)) != str(self.source):
            raise ToolDenied('Apply journal target changed; host recovery required')

    def _save(self, event):
        self.store.save(self.record, event)

    def _git(self, *args, recovery=False):
        if not recovery:
            self.service._tick('apply_git')
        result = RepositoryTools(self.source, max_output_bytes=512000)._execute(
            ['/usr/bin/git', *GIT_OPTIONS, '--git-dir=' + str(self.source / '.git'),
             '--work-tree=' + str(self.source), *args],
            timeout=10 if recovery else min(10, self.service._check()))
        if result.returncode or result.timed_out or result.truncated:
            raise ToolDenied('Apply Git operation failed')
        return result.stdout

    def _head(self, recovery=False):
        return self._git('rev-parse', '--verify', 'HEAD', recovery=recovery).strip()

    def _status(self, recovery=False):
        return self._git('status', '--porcelain=v1', '--untracked-files=all', recovery=recovery)

    @contextmanager
    def _lock(self):
        git = self.source / '.git'
        if git.is_symlink() or not git.is_dir() or self.source.resolve() != self.source:
            raise ToolDenied('Source must be a standalone checkout')
        # Shared across app processes/databases targeting this checkout. Never
        # replace/delete the lock inode: flock ownership ends with the process.
        fd = os.open(git / 'jarvis-apply.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ToolDenied('Another apply is active for this checkout') from None
            yield
        finally:
            os.close(fd)

    def _eligible(self):
        r = self.record
        if r['state'] != 'passed' or r.get('apply'):
            raise ToolDenied('Only an unapplied passed candidate is eligible')
        if r.get('source_root') != str(self.service.source):
            raise ToolDenied('Source location mismatch')
        reviewed = r.get('reviewed_at')
        if not reviewed or not 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(reviewed).replace(tzinfo=timezone.utc)).total_seconds() < 86400:
            raise ToolDenied('Review expired or missing; request a fresh review')
        expected_diff = r['diff']
        expected = r.get('reviewed_fingerprint')
        if (not expected or r.get('tested_fingerprint') != expected
                or r.get('fingerprint') != expected or not r.get('reviewer_result')
                or r['reviewer_result']['verdict'] != 'passed'
                or r.get('reviewed_diff_hash') != digest(expected_diff.encode())):
            raise ToolDenied('Review evidence is incomplete or changed')
        phases = [('targeted', r['targeted_tests']), ('full_python', ['tests'])]
        if (Path(r['worktree']) / 'tests/ui.test.cjs').is_file():
            phases.append(('full_ui', ['tests/ui.test.cjs']))
        for phase, paths in phases:
            matches = [t for t in r['test_results'] if t['phase'] == phase and t['fingerprint'] == expected]
            if not matches or any(matches[-1][k] for k in ('returncode', 'timed_out', 'truncated')) or matches[-1]['paths'] != paths:
                raise ToolDenied('Passing test evidence is incomplete')
        if self._head() != r['base_commit'] or self._status():
            raise ToolDenied('Main must be clean at the candidate baseline; create a new candidate')
        branch = self._git('symbolic-ref', 'HEAD').strip()
        if not branch.startswith('refs/heads/'):
            raise ToolDenied('Main must be on a local branch')
        if self.target_branch and branch != self.target_branch:
            raise ToolDenied('Integration branch does not match host configuration')
        # Reuses whole-worktree manifest checks, protected path and line/file
        # limits, canonical paths, symlink/hardlink checks, and diff generation.
        if self.service._diff() != expected or r['diff'] != expected_diff or not r['files_changed']:
            raise ToolDenied('Candidate diff changed since review')
        for path in r['files_changed']:
            CandidatePolicy().edit_path(path)
            parent = self.source
            for part in path.split('/')[:-1]:
                parent = parent / part
                if parent.is_symlink() or not parent.is_dir():
                    raise ToolDenied('Unsafe main path')
            target = self.source / path
            if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_nlink != 1)):
                raise ToolDenied('Unsafe main target')
        current, _ = self.service._editor().validate()
        self.expected_files = {path: current[path] for path in r['files_changed']}
        return branch

    def preview(self, candidate_id, *, relayed=False):
        self._setup(candidate_id)
        self._target()
        if self.record.get('busy'):
            return {'candidate_id': candidate_id, 'eligible': False, 'reason': 'Candidate is busy'}
        self.store.claim(candidate_id)
        self.record['lease_until'] = self.store.get(candidate_id, internal=True)['lease_until']
        try:
            with self._lock():
                # Preview never recovers interrupted mutations or changes main.
                self._eligible()
                from .engineering_authority import assess_action
                authority = assess_action('candidate_apply', goal=self.record['goal'],
                    changed_paths=self.record['files_changed'], read_only_target=self._read_only())
                return {'candidate_id': candidate_id, 'eligible': True,
                        'files_changed': self.record['files_changed'], 'diff_summary': self.record['diff_summary'],
                        'authority': authority.model_dump(),
                        'required_confirmation': (None if not authority.approval_required else
                            f'On the host run: python -m core.candidate_broker --approve-apply {candidate_id}' if relayed else
                            f'Approve and apply candidate {candidate_id} to main')}
        except (ToolDenied, OSError, ValueError):
            return {'candidate_id': candidate_id, 'eligible': False,
                    'reason': 'Requires passed, unexpired, unchanged evidence and a clean matching baseline'}
        finally:
            self._finish()

    @contextmanager
    def _patch(self):
        # Trusted journal, not arbitrary files from the worktree. --index plus
        # Git's safe patch parser rejects path escapes, and matches the index.
        with tempfile.NamedTemporaryFile(prefix='jarvis-reviewed-', suffix='.patch') as stream:
            stream.write(self.record['apply']['patch'].encode())
            stream.flush()
            yield stream.name

    def _verify_patch_contents(self):
        audit = self.record['apply']
        names = self._git('diff', '--cached', '--no-ext-diff', '--no-textconv',
                          '--name-only', '--no-renames', audit['baseline'], '--').splitlines()
        if sorted(names) != sorted(audit['files_changed']):
            raise ToolDenied('Index contains changes outside the reviewed patch')
        reader = RepositoryTools(self.source, policy=CandidatePolicy())
        for path, (expected_hash, mode) in audit['expected_files'].items():
            CandidatePolicy().edit_path(path)
            if digest(reader.read_text(path).encode()) != expected_hash:
                raise ToolDenied('Applied bytes differ from reviewed candidate')
            if digest(self._git('show', ':' + path).encode()) != expected_hash:
                raise ToolDenied('Staged bytes differ from reviewed candidate')
            entry = self._git('--literal-pathspecs', 'ls-files', '--stage', '--', path).split()
            # Git stores only regular-file type and the owner executable bit,
            # not umask-dependent group/other permissions (e.g. 0664 vs 0644).
            git_mode = stat.S_IFREG | (0o755 if mode & stat.S_IXUSR else 0o644)
            if not entry or int(entry[0], 8) != git_mode:
                raise ToolDenied('Staged mode differs from reviewed candidate')

    def _verify_tree(self):
        audit = self.record['apply']
        if self._head() != self.record['base_commit']:
            raise ToolDenied('Main HEAD changed during apply')
        if self._git('symbolic-ref', 'HEAD').strip() != audit['branch']:
            raise ToolDenied('Main branch changed during apply')
        self._git('diff-files', '--quiet', '--')
        if self._git('ls-files', '--others', '--exclude-standard'):
            raise ToolDenied('Unexpected untracked files during apply')
        if self._git('write-tree').strip() != audit['tree']:
            raise ToolDenied('Index changed during apply')

    def _verify(self):
        r, audit = self.record, self.record['apply']
        repository = CandidateRepository(self.source, source_root=self.source, check=self.service._check)
        phases = [('targeted', r['targeted_tests']), ('full_python', ['tests'])]
        if (self.source / 'tests/ui.test.cjs').is_file():
            phases.append(('full_ui', ['tests/ui.test.cjs']))
        else:
            audit['ui'] = 'not_present'
        for phase, paths in phases:
            self.service._tick('post_apply_' + phase)
            if self.service.tester:
                result = self.service.tester(repository, phase, paths)
            else:
                result = repository.run_ui_tests() if phase == 'full_ui' else repository.run_tests(paths)
            evidence = dict(sandbox=result.sandbox, phase=phase, returncode=result.returncode, timed_out=result.timed_out,
                            truncated=result.truncated or len(result.stdout) > 16000 or len(result.stderr) > 16000,
                            stdout=result.stdout[:16000], stderr=result.stderr[:16000])
            audit['verification'].append(evidence)
            self._save('post_apply_test_finished')
            self.service._check()
            if result.returncode or evidence['timed_out'] or evidence['truncated']:
                raise ToolDenied('Post-apply verification failed')
        self._verify_tree()

    def _recover(self):
        audit = self.record.get('apply')
        if not audit or audit['status'] not in {'applying', 'verifying', 'committing', 'rolling_back'}:
            return
        # Process death releases flock. Durable journal records the patch before
        # mutation and commit object before ref update, allowing reconciliation.
        head = self._head(recovery=True)
        if audit.get('commit') == head and audit['status'] == 'committing' and not self._status(recovery=True):
            audit.update(status='applied', completed_at=timestamp(), post_apply_verified=True)
            self._save('apply_recovered_commit')
            return
        self._rollback()

    def _rollback(self):
        audit = self.record['apply']
        audit['status'] = 'rolling_back'
        self._save('rollback_started')
        try:
            if self._head(recovery=True) != self.record['base_commit']:
                raise ToolDenied('Concurrent HEAD change prevents rollback')
            if self._status(recovery=True):
                with self._patch() as patch:
                    self._git('apply', '--reverse', '--index', '--check', '--', patch, recovery=True)
                    self._git('apply', '--reverse', '--index', '--', patch, recovery=True)
            if self._status(recovery=True):
                raise ToolDenied('Rollback did not restore clean baseline')
            audit.update(status='rolled_back', rollback=True, completed_at=timestamp())
            self._save('apply_rolled_back')
        except Exception:
            audit.update(status='rollback_failed', rollback=False, completed_at=timestamp(),
                         error='Rollback could not safely restore baseline; manual intervention required')
            self._save('apply_rollback_failed')

    def _finish(self):
        self.record['elapsed_seconds'] += time.monotonic() - self.started
        self.store.save(self.record)
        self.store.release(self.record['id'])

    def inspect(self, candidate_id, *, read_only=False):
        """Read an audit snapshot, optionally retaining the legacy recovery behavior."""
        self._setup(candidate_id, read_only=read_only)
        if read_only:
            return self._public()
        self._target()
        # Recover interrupted mutations only while holding the source lock.
        with self._lock():
            pending = self.record.get('apply', {}).get('status') in {'applying', 'verifying', 'committing', 'rolling_back'}
            self._recover()
            if pending:
                self.store.release(candidate_id)
        return self._public()

    def _public(self):
        audit = self.record.get('apply')
        return {'candidate_id': self.record['id'], 'apply':
                {key: value for key, value in audit.items() if key != 'patch'} if audit else None}

    def _authorize(self, current_request, authority, relayed):
        from .engineering_authority import authorize, EscalationRequired, HostApprovalRequired
        try:
            # Relayed (mailbox) text is authored by the production container and
            # can never approve; only the host controller channel can.
            return authorize('candidate_apply', authority=authority,
                approved=not relayed and approved_id(current_request) == self.record['id'],
                goal=self.record['goal'], changed_paths=self.record['files_changed'],
                read_only_target=self._read_only())
        except EscalationRequired as exc:
            if relayed:
                raise HostApprovalRequired(exc.decision) from None
            raise

    def apply(self, candidate_id, current_request='', *, authority=None, relayed=False):
        require_candidate_id(current_request, candidate_id)
        self._setup(candidate_id)
        self._authorize(current_request, authority, relayed)
        self._target()
        if self.record.get('busy'):
            raise ToolDenied('Candidate is busy')
        self.store.claim(candidate_id)
        self.record['lease_until'] = self.store.get(candidate_id, internal=True)['lease_until']
        try:
            with self._lock():
                self._recover()
                if self.record.get('apply'):
                    return self._public()  # Durable idempotency: never apply twice.
                try:
                    branch = self._eligible()
                except Exception:
                    self.record['apply'] = dict(status='denied', candidate_id=candidate_id,
                        approval=current_request, target_root=str(self.source), requested_at=timestamp(), files_changed=[], verification=[],
                        commit=None, error='Eligibility validation failed', rollback=False)
                    self._save('apply_denied')
                    return self._public()
                # _eligible recomputes the actual diff from immutable review
                # evidence. Reclassify those verified paths before mutation.
                self._authorize(current_request, authority, relayed)
                audit = self.record['apply'] = dict(status='applying', candidate_id=candidate_id,
                    approval=current_request, requested_at=timestamp(), files_changed=list(self.record['files_changed']),
                    patch=self.record['diff'], patch_hash=digest(self.record['diff'].encode()),
                    baseline=self.record['base_commit'], branch=branch, target_root=str(self.source), verification=[], commit=None,
                    expected_files=self.expected_files,
                    rollback=False, post_apply_verified=False)
                self._save('apply_approved')
                try:
                    with self._patch() as patch:
                        self._git('apply', '--index', '--check', '--', patch)
                        self._git('apply', '--index', '--', patch)
                    self._verify_patch_contents()
                    audit['tree'] = self._git('write-tree').strip()
                    audit['status'] = 'verifying'
                    self._save('patch_applied')
                    self._verify()
                    audit['post_apply_verified'] = True
                    # commit-tree bypasses hooks, signing, filters and staging of
                    # unrelated files. update-ref uses a compare-and-swap baseline.
                    audit['commit'] = self._git('-c', 'commit.gpgsign=false', '-c', 'user.name=Jarvis', '-c', 'user.email=jarvis@localhost',
                        'commit-tree', audit['tree'], '-p', audit['baseline'], '-m',
                        f'Jarvis: apply candidate {candidate_id}').strip()
                    audit['status'] = 'committing'
                    self._save('apply_commit_prepared')
                    self._verify_tree()
                    self._git('update-ref', '-m', f'Jarvis apply {candidate_id}', branch,
                              audit['commit'], audit['baseline'])
                    audit.update(status='applied', completed_at=timestamp())
                    self._save('apply_succeeded')
                except Exception:
                    audit['error'] = 'Apply or post-apply verification failed'
                    self._recover()
                return self._public()
        finally:
            self._finish()
