"""Central, deny-first policy for the agent-facing repository tools."""

from pathlib import PurePosixPath


class ToolError(RuntimeError):
    """A tool could not complete its operation."""


class ToolDenied(ToolError):
    """An operation was rejected by the repository policy."""


class ToolPolicy:
    """Protected paths are omitted from discovery and rejected on direct access.

    Additional protected paths may be supplied by trusted application code.
    There is deliberately no option to remove the built-in protections.
    """

    _directories = frozenset({
        ".git", ".venv", "venv", ".agents", ".codex", "policy", "policies",
        "security", ".ssh", ".aws", ".gnupg", "__pycache__", ".pytest_cache",
    })
    _files = frozenset({"agents.md", "skill.md", ".gitmodules", ".gitattributes"})

    def __init__(self, additional_protected: tuple[str, ...] = ()):
        self._additional = tuple(self.parts(path) for path in additional_protected)

    @staticmethod
    def parts(path: str) -> tuple[str, ...]:
        if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
            raise ToolDenied("Expected a relative repository path")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ToolDenied("Paths must stay inside the repository")
        return parsed.parts

    def check(self, path: str) -> tuple[str, ...]:
        parts = self.parts(path)
        for part in parts:
            name = part.casefold()
            stem = name.split(".")[0]
            if (name in self._directories or name in self._files
                    or name == ".env" or name.startswith(".env.")
                    or name.startswith(".env-") or name.startswith(".env_")
                    or stem in {"security", "policy", "policies"}
                    or name.startswith(("security_", "security-", "policy_", "policy-"))
                    or name.endswith(("_policy.py", "-policy.json", ".pem", ".key"))):
                raise ToolDenied("Protected repository path")
        if any(parts[:len(prefix)] == prefix for prefix in self._additional):
            raise ToolDenied("Protected repository path")
        return parts
