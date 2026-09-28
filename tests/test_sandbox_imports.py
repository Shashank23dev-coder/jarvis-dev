"""Import parity and isolation checks, also run through the live pytest tool."""

import os
from pathlib import Path
import sys

import pytest

import tools.policy
from tools import RepositoryTools, ToolDenied


def test_policy_import_matches_repository_root():
    root = Path(__file__).resolve().parents[1]
    assert Path(tools.policy.__file__).resolve() == root / "tools/policy.py"
    repository = RepositoryTools(root)
    assert "tools/policy.py" not in repository.list_files()
    with pytest.raises(ToolDenied):
        repository.read_text("tools/policy.py")


def test_sandbox_runtime_and_mounts():
    if Path.cwd() != Path("/workspace"):
        pytest.skip("Assertions for the Bubblewrap workspace")
    assert "/workspace" in sys.path
    assert "PYTHONPATH" not in os.environ
    assert os.environ["HOME"] == "/tmp"
    assert os.environ["PATH"] == "/runtime/bin:/usr/bin"
    assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "OPENAI_API_KEY" not in os.environ
    for name in (".env", ".git", ".agents", ".codex", "policies"):
        assert not Path("/workspace", name).exists()
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    for mountpoint in (("/workspace", "/runtime", "/usr") if Path("/usr/bin/bwrap").exists() else ("/workspace", "/")):
        assert any(line.split()[4] == mountpoint and
                   "ro" in line.split()[5].split(",") for line in mounts)
    with pytest.raises(OSError):
        Path(tools.policy.__file__).open("a")
