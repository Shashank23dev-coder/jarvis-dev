"""Candidate-only text replacement. Never registered with the normal chat tools."""

from contextlib import contextmanager
import difflib
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .policy import ToolDenied, ToolPolicy
from .repository import RepositoryTools


class CandidatePolicy(ToolPolicy):
    """Additional denials; the existing protected-path policy is unchanged."""

    def check(self, path):
        parts = super().check(path)
        for part in parts:
            name = part.casefold()
            if (any(word in name for word in ('secret', 'credential', 'deploy', 'token', 'password', 'private_key', 'private-key'))
                    or any(suffix in name for suffix in ('.db', '.sqlite', '.p12', '.pfx', '.kdbx'))):
                raise ToolDenied('Sensitive candidate path')
        return parts

    def edit_path(self, path):
        parts = self.check(path)
        if not parts or '/'.join(parts) != path or not re.fullmatch(r'[A-Za-z0-9_./-]+', path):
            raise ToolDenied('Invalid edit path')
        approved = ((parts[0] in {'core', 'models', 'agents', 'evaluator', 'src'} and Path(path).suffix == '.py')
                    or (parts[0] == 'tests' and Path(path).suffix in {'.py', '.cjs', '.js'})
                    or (parts[0] == 'docs' and Path(path).suffix in {'.md', '.txt'})
                    or (parts[0] == 'static' and Path(path).suffix in {'.html', '.css', '.js'})
                    or path == 'main.py')
        if not approved or path.startswith(('core/candidate_', 'core/self_edit', 'core/engineering_')):
            raise ToolDenied('Path is outside approved editing scope')
        return parts


ASSIGNMENT = re.compile(r'''(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*)(["'])([^"']{8,})(["'])''')


def inspection_text(content):
    """Read approved source without forwarding credential-like fixture literals.

    Edit validation still rejects these literals. Redaction is for inspection,
    never a permission to write credentials or access protected files.
    """
    return safe_text(ASSIGNMENT.sub(lambda m: m[1] + m[2] + 'OMITTED' + m[4], content))


def safe_text(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > 256_000:
        raise ToolDenied('Invalid edit size or encoding')
    if any(ord(char) < 32 and char not in '\t\r\n' for char in content):
        raise ToolDenied('Binary content is not editable')
    # Defense in depth, not a claim to recognize arbitrary secrets in source.
    if re.search(r'-----BEGIN .*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b', content):
        raise ToolDenied('Credential-like content is blocked')
    if ASSIGNMENT.search(content):
        raise ToolDenied('Credential-like assignment is blocked')
    return content


def digest(data):
    return hashlib.sha256(data).hexdigest()


def scan(root, check=lambda: None):
    """Hash every file, including protected ones, without exposing their content."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ToolDenied('Candidate root is unsafe')
    result, total = {}, 0
    def visit(directory, prefix=''):
        nonlocal total
        check()
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                name = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    visit(Path(directory) / entry.name, name + '/')
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if info.st_size > 1_000_000 or len(result) >= 10000:
                        raise ToolDenied('Candidate snapshot limit exceeded')
                    descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, 'rb') as stream:
                        current = os.fstat(stream.fileno())
                        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                            raise ToolDenied('Unsafe candidate file')
                        data = stream.read(1_000_001)
                    total += len(data)
                    if len(data) > 1_000_000 or total > 50_000_000:
                        raise ToolDenied('Candidate snapshot limit exceeded')
                    result[name] = [digest(data), stat.S_IMODE(info.st_mode)]
                else:
                    raise ToolDenied('Candidate links and special files are forbidden')
    visit(root)
    return result


class CandidateEditor:
    def __init__(self, root, baseline, *, max_files=8, max_lines=400, check=lambda: None):
        self.root = Path(root)
        self.baseline = baseline
        self.max_files, self.max_lines = max_files, max_lines
        self.check = check
        self.policy = CandidatePolicy()
        self.reader = RepositoryTools(root, policy=self.policy)

    def validate(self):
        current = scan(self.root, self.check)
        changed = sorted(name for name in set(current) | set(self.baseline)
                         if current.get(name) != self.baseline.get(name))
        for name in changed:
            self.policy.edit_path(name)
            if name not in current:
                raise ToolDenied('File deletion is not supported')
            if name in self.baseline and current[name][1] != self.baseline[name][1]:
                raise ToolDenied('File mode changes are not supported')
            safe_text(self.reader.read_text(name))
        if len(changed) > self.max_files:
            raise ToolDenied('Changed file limit exceeded')
        return current, changed

    @contextmanager
    def _parent(self, parts):
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def apply(self, edits, original):
        """Preflight the entire batch before any write; existing parent dirs only.

        original(path) is host-owned baseline text, never model-supplied.
        """
        _, changed = self.validate()
        proposed = {path: self.reader.read_text(path) for path in changed}
        seen = set()
        for edit in edits:
            self.check()
            path, content = edit['path'], edit['content']
            parts = self.policy.edit_path(path)
            if path in seen:
                raise ToolDenied('Duplicate edit path')
            seen.add(path)
            proposed[path] = safe_text(content)
            with self._parent(parts) as parent:
                try:
                    info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ToolDenied('Unsafe edit target')
                except FileNotFoundError:
                    pass
        changed_text = {path: value for path, value in proposed.items()
                        if path not in self.baseline or value != original(path)}
        lines = 0
        for path, content in changed_text.items():
            for line in difflib.ndiff(original(path).splitlines(keepends=True), content.splitlines(keepends=True)):
                if line.startswith(('+ ', '- ')):
                    lines += 1
        if len(changed_text) > self.max_files or lines > self.max_lines:
            raise ToolDenied('Candidate change limits exceeded')
        for edit in edits:
            self.check()
            parts = self.policy.edit_path(edit['path'])
            with self._parent(parts) as parent:
                temp = '.jarvis-edit-' + uuid4().hex
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(edit['content'].encode('utf-8'))
                        os.fchmod(stream.fileno(), self.baseline.get(edit['path'], [None, 0o644])[1])
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Recheck immediately before the descriptor-relative replacement.
                    try:
                        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ToolDenied('Unsafe edit target')
                    except FileNotFoundError:
                        pass
                    os.replace(temp, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                finally:
                    try:
                        os.unlink(temp, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        return self.validate()
