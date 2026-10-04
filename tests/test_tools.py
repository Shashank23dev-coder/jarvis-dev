"""Security boundaries and behavior of the programmatic tool layer."""

from pathlib import Path
import os
import subprocess
import sys

import pytest

from tools import CommandResult, RepositoryTools, ToolDenied, ToolError, ToolPolicy


def git(root, *args):
    return subprocess.run(["/usr/bin/git", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "Tool tests")
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n")
    git(tmp_path, "add", "hello.txt", "src/app.py")
    git(tmp_path, "commit", "-m", "Initial files")
    return RepositoryTools(tmp_path)


@pytest.mark.parametrize("path", [
    "../outside", "src/../../outside", "/etc/passwd", "src/../hello.txt", "a\\b", "a\x00b",
    ".env", ".env.local", ".env-backup", ".ENV", "src/.env.production",
    ".git/config", ".venv/lib/code.py", "venv/bin/python", "policies/rules.txt",
    "security/secrets.txt", "SECURITY.md", "policy.json", "security_rules.py",
    "access_policy.py", "AGENTS.md", ".agents/a", ".codex/a", "SKILL.md",
    "certificate.pem", "private.key", ".gitmodules", ".gitattributes",
])
def test_protected_and_escaping_paths_rejected(repo, path):
    with pytest.raises(ToolDenied):
        repo.read_text(path)
    with pytest.raises(ToolDenied):
        repo.list_files(path)
    with pytest.raises(ToolDenied):
        repo.git_diff(path)


def test_listing_and_utf8_read(repo):
    (repo.root / "src" / "unicode.txt").write_text("café ☕\n", encoding="utf-8")
    (repo.root / ".env").write_text("SECRET=hidden")
    (repo.root / "policies").mkdir()
    (repo.root / "policies" / "rules.txt").write_text("hidden")
    assert repo.list_files() == ["hello.txt", "src/app.py", "src/unicode.txt"]
    assert repo.list_files("src") == ["src/app.py", "src/unicode.txt"]
    assert repo.read_text("src/unicode.txt") == "café ☕\n"
    with pytest.raises(ToolDenied):
        repo.read_text("src")
    with pytest.raises(ToolDenied):
        repo.read_text("missing")


def test_symlinks_hardlinks_and_special_files(repo, tmp_path):
    (repo.root / "link").symlink_to("hello.txt")
    (repo.root / "external").symlink_to("/etc/passwd")
    (repo.root / "dirlink").symlink_to("src", target_is_directory=True)
    os.link(repo.root / "hello.txt", repo.root / "hardlink")
    os.mkfifo(repo.root / "pipe")
    assert repo.list_files() == ["src/app.py"]
    for path in ("link", "external", "dirlink/app.py", "hardlink", "hello.txt", "pipe"):
        with pytest.raises(ToolDenied):
            repo.read_text(path)


@pytest.mark.parametrize("data", [b"a\0b", b"\xff", b"\x1b[31m"])
def test_binary_rejected(repo, data):
    (repo.root / "binary").write_bytes(data)
    with pytest.raises(ToolDenied):
        repo.read_text("binary")


def test_limits_and_custom_policy(repo):
    limited = RepositoryTools(repo.root, max_file_bytes=2, max_files=1)
    with pytest.raises(ToolDenied):
        limited.read_text("hello.txt")
    with pytest.raises(ToolError):
        limited.list_files()
    restricted = RepositoryTools(repo.root, policy=ToolPolicy(("src",)))
    assert restricted.list_files() == ["hello.txt"]
    with pytest.raises(ToolDenied):
        restricted.read_text("src/app.py")
    for options in ({"timeout": float("inf")}, {"timeout": 0}, {"max_files": -1},
                    {"max_output_bytes": True}):
        with pytest.raises(ValueError):
            RepositoryTools(repo.root, **options)


def test_git_status_and_staged_unstaged_diff(repo):
    assert repo.git_status().stdout == ""
    assert repo.git_diff().stdout == ""
    (repo.root / "hello.txt").write_text("first change\n")
    git(repo.root, "add", "hello.txt")
    (repo.root / "hello.txt").write_text("second change\n")
    (repo.root / "untracked.txt").write_text("new")
    assert "MM 'hello.txt'" in repo.git_status().stdout
    assert "?? 'untracked.txt'" in repo.git_status().stdout
    assert "+first change" in repo.git_diff(staged=True).stdout
    assert "+second change" in repo.git_diff("hello.txt").stdout
    assert "untracked" not in repo.git_diff().stdout


def test_git_protected_deletions_renames_and_links(repo):
    (repo.root / ".env").write_text("VERY_SECRET\n")
    (repo.root / "policy.json").write_text("PRIVATE_POLICY\n")
    git(repo.root, "add", "-f", ".env", "policy.json")
    git(repo.root, "commit", "-m", "Protected files")
    (repo.root / ".env").unlink()
    (repo.root / "policy.json").write_text("PRIVATE_CHANGED\n")
    git(repo.root, "mv", "hello.txt", "renamed.txt")
    (repo.root / "src" / "app.py").unlink()
    (repo.root / "src" / "app.py").symlink_to("../policy.json")
    status = repo.git_status().stdout
    assert ".env" not in status and "policy.json" not in status and "src/app.py" not in status
    assert "renamed.txt" in status
    for staged in (False, True):
        output = repo.git_diff(staged=staged).stdout
        assert "VERY_SECRET" not in output and "PRIVATE" not in output
        assert ".env" not in output and "policy.json" not in output


def test_git_literal_pathspec_and_deleted_file(repo):
    (repo.root / "*.txt").write_text("literal\n")
    git(repo.root, "add", "--", "*.txt")
    git(repo.root, "commit", "-m", "Literal path")
    (repo.root / "*.txt").write_text("literal changed\n")
    (repo.root / "hello.txt").write_text("other changed\n")
    result = repo.git_diff("*.txt")
    assert "literal changed" in result.stdout
    assert "other changed" not in result.stdout
    (repo.root / "hello.txt").unlink()
    assert "deleted file" in repo.git_diff("hello.txt").stdout


def test_git_disables_external_helpers_and_ignores_environment(repo, monkeypatch, tmp_path):
    marker = repo.root / "executed"
    helper = repo.root / "helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    helper.chmod(0o755)
    git(repo.root, "config", "diff.external", str(helper))
    git(repo.root, "config", "core.fsmonitor", str(helper))
    git(repo.root, "config", "core.worktree", "/tmp")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(helper))
    monkeypatch.setenv("GIT_DIR", "/does/not/exist")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "HOST_SECRET")
    (repo.root / "hello.txt").write_text("changed\n")
    assert "hello.txt" in repo.git_status().stdout
    assert "+changed" in repo.git_diff().stdout
    assert not marker.exists()
    result = repo._execute([sys.executable, "-c", "import os; print(os.environ.get('OPENAI_API_KEY'))"], timeout=5)
    assert result.stdout.strip() == "None"


def test_git_fails_cleanly_outside_repository(tmp_path):
    with pytest.raises(ToolDenied):
        RepositoryTools(tmp_path).git_status()


@pytest.mark.parametrize("command", [
    "rm hello.txt", "touch x", "python -c pass", "bash -c pwd", "sh", "git reset --hard",
    "git add hello.txt", "git checkout .", "git -c x=y status", "git status --short",
    "git diff --output=x", "git diff --ext-diff", "ls; pwd", "ls | cat", "pwd > x",
    "cat $(pwd)", "cat `pwd`", "pwd && ls", "pwd\nls", "pytest -p plugin",
    "pytest --override-ini=x", "pytest ../outside", "pytest .env", "cat .env",
    "cat .git/config", "cat /etc/passwd", "ls /tmp", "cd /tmp", "", "'",
    ["pwd", "extra"], ["cat", "hello.txt", "extra"], ["pwd", None],
])
def test_command_denials(repo, command):
    with pytest.raises(ToolDenied):
        repo.run_command(command)


def test_command_allowlist(repo, monkeypatch):
    assert repo.run_command("pwd").stdout.strip() == str(repo.root)
    assert repo.run_command("ls src").stdout == "src/app.py"
    assert repo.run_command(["cat", "hello.txt"]).stdout == "hello\n"
    assert repo.run_command("git status").returncode == 0
    assert repo.run_command("git diff --cached hello.txt").stdout == ""
    (repo.root / "space name.txt").write_text("spaces")
    assert repo.run_command('cat "space name.txt"').stdout == "spaces"
    monkeypatch.setattr(repo, "run_pytest", lambda paths: CommandResult(("pytest", *paths), 0, "passed"))
    assert repo.run_command("pytest tests/test_one.py").command == ("pytest", "tests/test_one.py")


def test_execution_timeout_output_limit_and_exit_code(repo):
    # Allow Python to start on a loaded host before testing process termination.
    result = repo._execute([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(10)"], timeout=2)
    assert result.timed_out and "start" in result.stdout
    limited = RepositoryTools(repo.root, max_output_bytes=32)
    result = limited._execute([sys.executable, "-c", "print('x' * 100000)"], timeout=5)
    assert result.truncated and len(result.stdout.encode()) <= 32
    result = repo._execute([sys.executable, "-c", "import sys; print('failure', file=sys.stderr); sys.exit(7)"], timeout=5)
    assert result.returncode == 7 and "failure" in result.stderr
    with pytest.raises(ToolError):
        repo._execute(["/no/such/executable"], timeout=5)


def test_status_never_returns_partial_unfiltered_output(repo, monkeypatch):
    monkeypatch.setattr(repo, "_git_command", lambda *args: CommandResult(("git",), 0, " M .env\0", truncated=True))
    with pytest.raises(ToolError):
        repo.git_status()
    with pytest.raises(ToolError):
        repo.git_diff()


def test_pytest_snapshot_and_sandbox_configuration(repo, monkeypatch):
    (repo.root / "tests").mkdir()
    (repo.root / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n")
    (repo.root / ".env").write_text("HOST_SECRET")
    (repo.root / "pytest.ini").write_text("[pytest]\naddopts = --bad-option\n")
    captured_snapshot = None

    def execute(argv, *, timeout):
        nonlocal captured_snapshot
        index = argv.index("/workspace")
        captured_snapshot = Path(argv[index - 1])
        assert (captured_snapshot / "tests" / "test_ok.py").is_file()
        assert not (captured_snapshot / ".env").exists()
        assert not (captured_snapshot / ".git").exists()
        assert "--unshare-all" in argv and "--clearenv" in argv
        assert argv[index - 2] == "--ro-bind"
        assert "-I" in argv and "-B" in argv
        assert argv[argv.index("-c") + 1] == "/dev/null"
        assert argv[-2:] == ["--", "tests"]
        assert 0 < timeout <= repo.pytest_timeout
        return CommandResult(tuple(argv), 1, "1 failed", "")

    monkeypatch.setattr(repo, "_execute", execute)
    monkeypatch.setattr("tools.sandbox.select_backend", lambda *args: ("bubblewrap", None, []))
    # This test validates construction even on hosts without Bubblewrap installed.
    monkeypatch.setattr(repo, "_bwrap", "/usr/bin/true")
    result = repo.run_pytest()
    assert result.returncode == 1 and result.command == ("pytest", "tests")
    assert captured_snapshot is not None and not captured_snapshot.exists()
    assert not (repo.root / ".pytest_cache").exists()


def test_pytest_requires_sandbox_and_valid_paths(repo, monkeypatch):
    monkeypatch.setattr(repo, "_bwrap", "/nonexistent/bwrap")
    monkeypatch.setattr("tools.sandbox.DOCKER", ("/nonexistent/docker",))
    with pytest.raises(ToolDenied, match="Bubblewrap"):
        repo.run_pytest(["src"])
    for paths in ([], "src", ["-x"], ["src/app.py::test_x"], ["missing"], [".git"]):
        with pytest.raises(ToolDenied):
            repo.run_pytest(paths)


def test_real_pytest_sandbox_is_readonly_or_fails_closed(repo):
    (repo.root / "tests").mkdir()
    (repo.root / "tests" / "test_isolation.py").write_text(
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "def test_isolation():\n"
        "    assert not Path('/workspace/.env').exists()\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n"
        "    with pytest.raises(OSError): Path('/workspace/mutated').write_text('x')\n"
    )
    if not Path(repo._bwrap).exists():
        with pytest.raises(ToolDenied):
            repo.run_pytest()
        return
    result = repo.run_pytest()
    assert not (repo.root / "mutated").exists()
    if result.returncode:
        # Restricted CI hosts must fail in Bubblewrap, never retry Python on host.
        assert "bwrap:" in result.stderr, result
    else:
        assert "1 passed" in result.stdout


def test_live_jarvis_pytest_imports_policy():
    repository = RepositoryTools()
    if not Path(repository._bwrap).exists():
        pytest.skip("Bubblewrap is unavailable")
    result = repository.run_pytest(["tests/test_sandbox_imports.py"])
    if result.returncode and "bwrap:" in result.stderr:
        pytest.skip("Host does not permit Bubblewrap namespaces")
    assert result.returncode == 0, result
    assert "2 passed" in result.stdout


def test_arbitrary_repository_policy_stays_protected(repo, monkeypatch):
    (repo.root / "tools").mkdir()
    (repo.root / "tools/policy.py").write_text("SECRET = 'protected'\n")

    def execute(argv, *, timeout):
        snapshot = Path(argv[argv.index("/workspace") - 1])
        assert not (snapshot / "tools/policy.py").exists()
        return CommandResult(tuple(argv), 0, "")

    monkeypatch.setattr(repo, "_execute", execute)
    monkeypatch.setattr(repo, "_bwrap", "/usr/bin/true")
    monkeypatch.setattr("tools.sandbox.select_backend", lambda *args: ("bubblewrap", None, []))
    repo.run_pytest(["src"])
