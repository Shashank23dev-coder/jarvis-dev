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
