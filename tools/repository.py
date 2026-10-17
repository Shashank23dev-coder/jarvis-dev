"""Read-only repository tools. No shell evaluation or file-editing interface."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import math
import os
from pathlib import Path
import selectors
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from typing import Sequence

from .policy import ToolDenied, ToolError, ToolPolicy


NODE_TEST_SUFFIXES = (".cjs", ".js")


def runner_results(result, paths=()):
    """Per-runner outcome of a test result or stored evidence: runner, returncode, timed_out,
    and its [start, end) spans of the combined stdout/stderr. Results without runners
    (test doubles, older evidence) count as one runner, judged by their paths."""
    get = result.get if isinstance(result, dict) else lambda key: getattr(result, key, None)
    if get("runners"):
        return [dict(runner) for runner in get("runners")]
    node = bool(paths) and all(path.endswith(NODE_TEST_SUFFIXES) for path in paths)
    return [dict(runner="node" if node else "pytest", returncode=get("returncode"), timed_out=bool(get("timed_out")),
                 stdout_span=[0, len(get("stdout") or "")], stderr_span=[0, len(get("stderr") or "")])]


EXCERPT_HEAD, EXCERPT_TAIL = 2000, 6000


def _excerpt(text):
    if len(text) <= EXCERPT_HEAD + EXCERPT_TAIL:
        return text
    cut = len(text) - EXCERPT_HEAD - EXCERPT_TAIL
    return f"{text[:EXCERPT_HEAD]}\n[… {cut} chars cut …]\n{text[-EXCERPT_TAIL:]}"


def stored_output(result, paths=()):
    """(stdout, stderr, runners) to store as test evidence: each runner's own output cut to
    its first 2,000 and last 6,000 characters, concatenated, with spans recomputed against
    the stored text, so one runner's long output never hides another runner's failure."""
    runners = runner_results(result, paths)
    texts = dict(stdout="", stderr="")
    for runner in runners:
        for stream in texts:
            piece = _excerpt((getattr(result, stream) or "")[slice(*runner[stream + "_span"])])
            runner[stream + "_span"] = [len(texts[stream]), len(texts[stream]) + len(piece)]
            texts[stream] += piece
    return texts["stdout"], texts["stderr"], runners


@dataclass(frozen=True)
class CommandResult:
    """Bounded process output; nonzero exit codes are returned, not hidden."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False
    sandbox: dict = field(default_factory=dict)
    runners: tuple = ()  # test commands only: see runner_results


class RepositoryTools:
    """Construct in trusted host code; expose only the public tool methods.

    The root, limits, executable paths and policy are host configuration, never
    agent-supplied arguments. Linux is required for descriptor-safe file access
    and the Bubblewrap pytest sandbox.
    """

    def __init__(self, root: str | Path | None = None, *,
                 policy: ToolPolicy | None = None, max_file_bytes: int = 1_000_000,
                 max_output_bytes: int = 1_000_000, max_files: int = 10_000,
                 timeout: float = 30, pytest_timeout: float = 120):
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Repository root must be a directory")
        for value in (max_file_bytes, max_output_bytes, max_files):
            if type(value) is not int or value <= 0:
                raise ValueError("Limits must be positive integers")
        for value in (timeout, pytest_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be finite and positive")
        self.policy = policy or ToolPolicy()
        self.max_file_bytes = max_file_bytes
        self.max_output_bytes = max_output_bytes
        self.max_files = max_files
        self.timeout = timeout
        self.pytest_timeout = pytest_timeout
        # Do not search PATH, which may contain repository-controlled programs.
        self._git = "/usr/bin/git"
        self._bwrap = "/usr/bin/bwrap"

    @contextmanager
    def _open(self, path: str, *, directory: bool = False):
        parts = self.policy.check(path)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            info = os.fstat(descriptor)
            if directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise ToolDenied("Expected a directory", reason='directory_required')
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ToolDenied("Only regular, non-linked files are readable", reason='linked_or_special_file')
            yield descriptor
        except OSError as exc:
            raise ToolDenied("Path unavailable or unsafe", reason='path_unavailable_or_unsafe') from exc
        finally:
            os.close(descriptor)

    def list_files(self, path: str = ".") -> list[str]:
        """Recursively list safe regular files, in repository-relative order."""
        self.policy.check(path)
        result = []

        def visit(relative: str):
            with self._open(relative, directory=True) as descriptor:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        child = str(Path(relative) / entry.name)
                        try:
                            self.policy.check(child)
                        except ToolDenied:
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            visit(child)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            result.append(child)
                            if len(result) > self.max_files:
                                raise ToolError("File listing exceeds configured limit")

        try:
            visit(path)
        except (OSError, RecursionError) as exc:
            raise ToolError("Could not list repository") from exc
        return sorted(result)

    def _read_bytes(self, path: str) -> bytes:
        with self._open(path) as descriptor:
            if os.fstat(descriptor).st_size > self.max_file_bytes:
                raise ToolDenied("File exceeds configured size limit", reason='file_size_limit')
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                data = stream.read(self.max_file_bytes + 1)
            if len(data) > self.max_file_bytes:
                raise ToolDenied("File exceeds configured size limit", reason='file_size_limit')
            return data

    def read_text(self, path: str) -> str:
        """Read bounded UTF-8 text, rejecting links, devices and binary files."""
        data = self._read_bytes(path)
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolDenied("File is not UTF-8 text", reason='utf8_required') from exc
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ToolDenied("File contains binary control characters", reason='binary_content')
        return value

    def search_text(self, query: str, path: str = '.', *, max_matches: int = 50) -> CommandResult:
        """Literal, bounded search through the same filtered descriptor-safe reads."""
        if not isinstance(query, str) or not 1 <= len(query) <= 256 or '\n' in query or '\x00' in query:
            raise ToolDenied('Expected a bounded literal search term', reason='invalid_search')
        if type(max_matches) is not int or not 1 <= max_matches <= 100:
            raise ToolDenied('Invalid search limit', reason='invalid_search')
        names = self.list_files(path)
        lines, scanned, size = [], 0, 0
        for name in names:
            try:
                content = self.read_text(name)
            except ToolDenied:
                continue  # Binary, oversized or otherwise unreadable files.
            scanned += len(content.encode('utf-8'))
            if scanned > 5_000_000:
                return CommandResult(('search_text',), 0, '\n'.join(lines), truncated=True)
            for number, line in enumerate(content.splitlines(), 1):
                if query in line:
                    entry = f'{name!r}:{number}: {line[:1000]}'
                    size += len(entry.encode('utf-8'))
                    if size > self.max_output_bytes or len(lines) >= max_matches:
                        return CommandResult(('search_text',), 0, '\n'.join(lines), truncated=True)
                    lines.append(entry)
        return CommandResult(('search_text',), 0, '\n'.join(lines))

    def git_revision(self) -> str:
        """Fixed identity probe only; never arbitrary Git arguments or log prose."""
        import re
        raw = self._git_command('rev-parse', '--show-toplevel', 'HEAD')
        lines = raw.stdout.splitlines()
        if (raw.returncode or raw.timed_out or raw.truncated or len(lines) != 2
                or lines[0] != str(self.root) or not re.fullmatch('[0-9a-f]{40}', lines[1])):
            raise ToolError('Repository identity unavailable')
        return lines[1]

    def git_log(self) -> CommandResult:
        """Ten revisions, parent hashes and timestamps; no commit prose or patches."""
        raw = self._git_command('log', '-10', '--format=%H %P %ct', '--no-decorate',
                                '--no-show-signature', '--no-notes')
        if raw.returncode or raw.timed_out or raw.truncated:
            raise ToolError('Git log failed or exceeded its limits')
        import re
        if any(not re.fullmatch(r'[0-9a-f]{40}(?: [0-9a-f]{40})*  ?[0-9]+', line)
               for line in raw.stdout.splitlines()):
            raise ToolError('Git log identity unavailable')
        return CommandResult(('git', 'log'), 0, raw.stdout)

    def _execute(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        """Internal bounded executor, never exposed as a tool."""
        environment = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
        }
        try:
            process = subprocess.Popen(
                list(argv), cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                shell=False,
            )
        except OSError as exc:
            raise ToolError("Required executable is unavailable") from exc
        output = {"stdout": bytearray(), "stderr": bytearray()}
        timed_out = truncated = False
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = self.max_output_bytes - sum(map(len, output.values()))
                        output[key.data].extend(chunk[:available])
                        if len(chunk) > available:
                            truncated = True
                            break
                    if truncated:
                        break
                if not (timed_out or truncated):
                    try:
                        process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
        finally:
            # Kill descendants as well, including those that closed their pipes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
        return CommandResult(tuple(argv), process.returncode,
                             output["stdout"].decode("utf-8", errors="replace"),
                             output["stderr"].decode("utf-8", errors="replace"),
                             timed_out, truncated)

    def _git_command(self, *arguments: str) -> CommandResult:
        if not (self.root / ".git").is_dir() or (self.root / ".git").is_symlink():
            raise ToolDenied("Expected a standalone repository with a local .git directory", reason='git_metadata_required')
        return self._execute((
            self._git, "--no-optional-locks", "--git-dir=" + str(self.root / ".git"),
            "--work-tree=" + str(self.root), "-c", "core.bare=false", "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false", "-c", "core.quotePath=true",
            "-c", "diff.renames=false", "-c", "core.hooksPath=/dev/null",
            "-c", "core.attributesFile=/dev/null", *arguments,
        ), timeout=self.timeout)

    def _visible_git_path(self, path: str) -> bool:
        try:
            parts = self.policy.check(path)
            # Check parents even for deleted paths; never traverse links.
            with self._open(str(Path(*parts).parent), directory=True):
                pass
            if os.path.lexists(self.root / path):
                with self._open(path):
                    pass
            return True
        except ToolError:
            return False

    def git_status(self) -> CommandResult:
        """Return porcelain status with protected and linked paths omitted."""
        raw = self._git_command("status", "--porcelain=v1", "-z", "--untracked-files=all",
                                "--no-renames", "--ignore-submodules=all")
        if raw.returncode or raw.timed_out or raw.truncated:
            raise ToolError("Git status failed or exceeded its limits")
        # repr keeps unusual filenames from injecting fake status lines.
        lines = [f"{record[:2]} {record[3:]!r}" for record in raw.stdout.split("\0")
                 if record and self._visible_git_path(record[3:])]
        return CommandResult(("git", "status"), 0, "\n".join(lines))

    def git_diff(self, path: str | None = None, *, staged: bool = False) -> CommandResult:
        """Diff safe tracked paths only; no external diff, textconv or renames."""
        if type(staged) is not bool:
            raise ToolDenied("staged must be a boolean")
        if path is not None:
            self.policy.check(path)
            if not self._visible_git_path(path):
                raise ToolDenied("Path unavailable or unsafe", reason='path_unavailable_or_unsafe')
            paths = [path]
        else:
            tracked = self._git_command("ls-files", "-z", "--cached")
            if tracked.returncode or tracked.timed_out or tracked.truncated:
                raise ToolError("Could not enumerate tracked files within limits")
            paths = sorted({p for p in tracked.stdout.split("\0") if p and self._visible_git_path(p)})
        if not paths:
            return CommandResult(("git", "diff"), 0, "")
        if len(paths) > self.max_files:
            raise ToolError("Diff exceeds configured file limit")
        result = self._git_command(
            "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
            "--no-color", "--ignore-submodules=all", *( ["--cached"] if staged else []),
            "--", *paths,
        )
        if result.returncode and not (result.timed_out or result.truncated):
            raise ToolError("Git diff failed")
        return CommandResult(("git", "diff"), result.returncode, result.stdout,
                             "", result.timed_out, result.truncated)

    def run_pytest(self, paths: Sequence[str] = ("tests",)) -> CommandResult:
        """Run tests in a read-only, secret-free snapshot with no host network.

        A functional Bubblewrap or development Docker backend is mandatory. Never falls back
        to running repository Python on the host. Only test paths are accepted;
        agent-provided pytest options/plugins/configuration are not supported.
        Node test files (.cjs/.js) are refused here; run_tests routes them to node.
        """
        if not isinstance(paths, (str, bytes)) and any(path.endswith(NODE_TEST_SUFFIXES) for path in paths):
            raise ToolDenied("Node test files run under node, not pytest")
        result = self._run_test_command(paths)
        return replace(result, runners=tuple(runner_results(result)))

    def run_ui_tests(self, paths: Sequence[str] = ("tests/ui.test.cjs",)) -> CommandResult:
        """Node test files (the fixed UI suite by default), same read-only sandbox snapshot."""
        result = self._run_test_command(paths, ui=True)
        return replace(result, runners=tuple(runner_results(result, paths)))

    def run_tests(self, paths: Sequence[str]) -> CommandResult:
        """Route test paths by runner: .cjs/.js files to node, everything else to pytest.

        Both runners must pass; their output is combined into one result, whose
        runners keep each runner's own return code and span of the output.
        """
        node = [path for path in paths if path.endswith(NODE_TEST_SUFFIXES)]
        python = [path for path in paths if path not in node]
        results = ([self.run_pytest(python)] if python else []) + ([self.run_ui_tests(node)] if node else [])
        if len(results) == 1:
            return results[0]
        first, second = results
        shifted = [dict(runner, stdout_span=[start + len(first.stdout) for start in runner["stdout_span"]],
                        stderr_span=[start + len(first.stderr) for start in runner["stderr_span"]])
                   for runner in second.runners]
        return CommandResult(first.command + second.command, first.returncode or second.returncode,
                             first.stdout + second.stdout, first.stderr + second.stderr,
                             first.timed_out or second.timed_out, first.truncated or second.truncated,
                             dict(first.sandbox, node=second.sandbox), first.runners + tuple(shifted))

    def _runtime_policy_bytes(self):
        runtime_root = Path(__file__).resolve().parents[1]
        if self.root != runtime_root:
            return None
        policy_source = runtime_root / "tools" / "policy.py"
        descriptor = os.open(policy_source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ToolDenied("Unsafe test runtime dependency")
            return source.read(self.max_file_bytes + 1)

    def _runtime_deployment_files(self):
        # Fixed host-owned dependencies: main imports the deployment perimeter,
        # whose names are intentionally blocked by CandidatePolicy. Include its
        # regression tests too, without granting candidate read/edit authority.
        if self.root != Path(__file__).resolve().parents[1]:
            return {}
        return {path: self._read_bytes(path) for path in (
            'core/deployment.py', 'core/deployment_server.py',
            'core/deployment_healthcheck.py', 'tests/test_deployment.py',
            'core/deployment_release.py', 'core/deployment_runtime.py',
            'core/deployment_tools.py', 'tests/test_deployment_release.py',
            'core/deployment_status.py', 'tests/test_deployment_status.py')}

    def _run_test_command(self, paths, *, ui=False):
        if isinstance(paths, (str, bytes)) or not paths:
            raise ToolDenied("Supply a nonempty sequence of test paths")
        for path in paths:
            self.policy.check(path)
            if path.startswith("-") or "::" in path:
                raise ToolDenied("Only test file or directory paths are accepted")
            with self._open(path, directory=(self.root / path).is_dir()):
                pass
        with tempfile.TemporaryDirectory(prefix="jarvis-tests-") as temporary:
            snapshot = Path(temporary) / "repository"
            snapshot.mkdir()
            total = 0
            for name in self.list_files():
                data = self._read_bytes(name)
                total += len(data)
                if total > 50_000_000:
                    raise ToolDenied("Test snapshot exceeds 50 MB")
                destination = snapshot / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            # Provision only host-approved runtime dependencies, never arbitrary
            # protected sources nominated by a model or test path.
            dependencies = self._runtime_deployment_files()
            data = self._runtime_policy_bytes()
            if data is not None:
                dependencies['tools/policy.py'] = data
            for name, data in dependencies.items():
                total += len(data)
                if len(data) > self.max_file_bytes or total > 50_000_000:
                    raise ToolDenied("Test runtime dependency exceeds snapshot limits")
                destination = snapshot / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            # Readable by the fixed non-root container UID even under umask 077.
            # The parent temporary directory remains private to the host user.
            snapshot.chmod(0o755)
            for item in snapshot.rglob("*"):
                item.chmod(0o755 if item.is_dir() else 0o444)
            from .sandbox import run_tests
            result = run_tests(self, snapshot, paths, ui=ui)
            return replace(result, command=(("node", "--test") if ui else ("pytest",)) + tuple(paths))

    def run_command(self, command: str | Sequence[str]) -> CommandResult:
        """Controlled command facade, not a general-purpose shell.

        Accepted grammar: pwd; ls [path]; cat path; git status;
        git diff [--cached] [path]; pytest [test-path ...].
        """
        try:
            argv = shlex.split(command) if isinstance(command, str) else list(command)
        except (ValueError, TypeError) as exc:
            raise ToolDenied("Invalid command") from exc
        if not argv or any(not isinstance(arg, str) or not arg or
                           any(char in arg for char in "\x00\n\r;|&><`$") for arg in argv):
            raise ToolDenied("Shell syntax and empty commands are disabled")
        if argv == ["pwd"]:
            return CommandResult(tuple(argv), 0, str(self.root) + "\n")
        if argv[0] == "ls" and len(argv) <= 2:
            return CommandResult(tuple(argv), 0, "\n".join(self.list_files(argv[1] if len(argv) == 2 else ".")))
        if argv[0] == "cat" and len(argv) == 2:
            return CommandResult(tuple(argv), 0, self.read_text(argv[1]))
        if argv == ["git", "status"]:
            return self.git_status()
        if argv[:2] == ["git", "diff"]:
            args = argv[2:]
            staged = bool(args and args[0] == "--cached")
            if staged:
                args = args[1:]
            if len(args) <= 1 and not any(arg.startswith("-") for arg in args):
                return self.git_diff(args[0] if args else None, staged=staged)
        if argv[0] == "pytest":
            return self.run_pytest(argv[1:] or ("tests",))
        raise ToolDenied("Command or arguments are not allowlisted")
