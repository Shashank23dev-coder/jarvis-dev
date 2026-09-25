"""Read-only repository tools. No shell evaluation or file-editing interface."""

from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Sequence

from .policy import ToolDenied, ToolError, ToolPolicy


@dataclass(frozen=True)
class CommandResult:
    """Bounded process output; nonzero exit codes are returned, not hidden."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False


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
                    raise ToolDenied("Expected a directory")
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ToolDenied("Only regular, non-linked files are readable")
            yield descriptor
        except OSError as exc:
            raise ToolDenied("Path unavailable or unsafe") from exc
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
                raise ToolDenied("File exceeds configured size limit")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                data = stream.read(self.max_file_bytes + 1)
            if len(data) > self.max_file_bytes:
                raise ToolDenied("File exceeds configured size limit")
            return data

    def read_text(self, path: str) -> str:
        """Read bounded UTF-8 text, rejecting links, devices and binary files."""
        data = self._read_bytes(path)
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolDenied("File is not UTF-8 text") from exc
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ToolDenied("File contains binary control characters")
        return value

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
            raise ToolDenied("Expected a standalone repository with a local .git directory")
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
                raise ToolDenied("Path unavailable or unsafe")
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

        Bubblewrap and unprivileged namespaces are mandatory. Never falls back
        to running repository Python on the host. Only test paths are accepted;
        agent-provided pytest options/plugins/configuration are not supported.
        """
        return self._run_test_command(paths)

    def run_ui_tests(self) -> CommandResult:
        """Host-only fixed UI suite, using the same read-only Bubblewrap snapshot."""
        return self._run_test_command(("tests/ui.test.cjs",), ui=True)

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

    def _run_test_command(self, paths, *, ui=False):
        if isinstance(paths, (str, bytes)) or not paths:
            raise ToolDenied("Supply a nonempty sequence of test paths")
        for path in paths:
            self.policy.check(path)
            if path.startswith("-") or "::" in path:
                raise ToolDenied("Only test file or directory paths are accepted")
            with self._open(path, directory=(self.root / path).is_dir()):
                pass
        if not Path(self._bwrap).is_file():
            raise ToolDenied("Pytest requires Bubblewrap; host execution is disabled")
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
            data = self._runtime_policy_bytes()
            if data is not None:
                total += len(data)
                if len(data) > self.max_file_bytes or total > 50_000_000:
                    raise ToolDenied("Test runtime dependency exceeds snapshot limits")
                destination = snapshot / "tools" / "policy.py"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            argv = [self._bwrap, "--unshare-all", "--die-with-parent", "--new-session",
                    "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr",
                    "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
                    "--symlink", "usr/lib64", "/lib64", "--proc", "/proc",
                    "--dev", "/dev", "--tmpfs", "/tmp",
                    "--ro-bind", str(snapshot), "/workspace",
                    "--ro-bind", sys.prefix, "/runtime", "--chdir", "/workspace",
                    "--clearenv", "--setenv", "PATH", "/runtime/bin:/usr/bin",
                    "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
                    "--setenv", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1",
                    ]
            if ui:
                argv += ["/usr/bin/node", "--test", "tests/ui.test.cjs"]
            else:
                argv += ["/runtime/bin/python", "-I", "-B", "-m", "pytest",
                         "-p", "no:cacheprovider", "-c", "/dev/null", "--rootdir=/workspace",
                         "-o", "pythonpath=/workspace", "--", *paths]
            result = self._execute(argv, timeout=self.pytest_timeout)
            return CommandResult((("node", "--test") if ui else ("pytest",)) + tuple(paths), result.returncode, result.stdout,
                                 result.stderr, result.timed_out, result.truncated)

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
