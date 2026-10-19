import os
from pathlib import Path

import pytest

from core.engineering_authority import OwnerDevAuthority, current_owner


@pytest.fixture
def owner(monkeypatch):
    """Trusted owner context for tests of the internal development tools."""
    monkeypatch.setenv('JARVIS_MODE', 'development')
    binding = current_owner.set(OwnerDevAuthority())
    try:
        yield
    finally:
        current_owner.reset(binding)


@pytest.fixture
def executable_script(tmp_path):
    """Factory for a real executable fake CLI: create(name, text) -> absolute path.

    The Docker test sandbox mounts /tmp noexec and has no other writable
    mount, so a file there fails os.access(X_OK) and execve. There the script
    is held in an anonymous memfd and run via /proc/<pid>/fd/<n>; elsewhere
    it is an ordinary file in tmp_path.
    """
    descriptors = []

    def create(name, text):
        if not os.statvfs(tmp_path).f_flag & os.ST_NOEXEC:
            path = tmp_path / name
            path.write_text(text)
            path.chmod(0o755)
            return path
        descriptor = os.memfd_create(name, os.MFD_CLOEXEC)
        descriptors.append(descriptor)
        os.write(descriptor, text.encode())
        return Path(f'/proc/{os.getpid()}/fd/{descriptor}')

    yield create
    for descriptor in descriptors:
        os.close(descriptor)
