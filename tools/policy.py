"""Central, deny-first policy for the agent-facing repository tools."""

from pathlib import PurePosixPath


class ToolError(RuntimeError):
    """A tool could not complete its operation."""


class ToolDenied(ToolError):
    """An operation was rejected by the repository policy."""

    # Closed diagnostics only. Exception prose can contain paths or credentials.
    reasons = {
        'candidate_id_required': 'candidate_apply',
        'integration_not_configured': 'candidate_apply',
        'approval_required': 'owner_escalation',
        'host_approval_required': 'owner_escalation',
        'owner_required': 'owner_authentication',
        'relative_path_required': 'repository_path',
        'path_escape': 'repository_path',
        'protected_path': 'repository_path',
        'linked_or_special_file': 'repository_path',
        'path_unavailable_or_unsafe': 'repository_path',
        'directory_required': 'repository_path',
        'file_size_limit': 'repository_read',
        'utf8_required': 'repository_read',
        'binary_content': 'repository_read',
        'protected_content': 'repository_read',
        'invalid_search': 'repository_search',
        'transport_limit': 'repository_result',
        'invalid_read_operation': 'project_operation',
        'read_only_operation_required': 'project_operation',
        'project_tests_disabled': 'project_operation',
        'unknown_project': 'project_context',
        'project_ambiguous': 'project_context',
        'project_context_unavailable': 'project_context',
        'deployment_status_unavailable': 'deployment_observation',
        'git_metadata_required': 'repository_git',
        'development_checkout_required': 'candidate_workspace',
        'unsupported_action': 'broker_request',
        'invalid_request': 'broker_request',
        'expired_request': 'broker_request',
        'legacy_rejection': 'broker_response',
        'invalid_response': 'broker_response',
        'unavailable': 'broker_transport',
        'mailbox_unsafe': 'broker_transport',
        'mailbox_limit': 'broker_transport',
        'mailbox_full': 'broker_transport',
        'source_metadata_required': 'broker_request',
        'execution_failed': 'broker_execution',
        'operation_denied': 'repository_policy',
    }

    def __init__(self, message='Operation denied', *, reason='operation_denied'):
        super().__init__(message)
        self.reason = reason if reason in self.reasons else 'operation_denied'
        self.gate = self.reasons[self.reason]

    def public_error(self):
        messages = {
            'candidate_id_required': 'Name the exact candidate ID in the current request before applying it.',
            'integration_not_configured': 'The candidate is prepared; integration is not configured. Its tests and review remain available. No source checkout was changed.',
            'approval_required': 'This action needs your approval before execution. I can prepare it first.',
            'host_approval_required': ('This change is prepared and needs owner approval on the host controller '
                '(python -m core.candidate_broker --approve-apply <candidate ID>). Approval typed in chat is not accepted for it.'),
            'owner_required': 'Sign in as the owner to inspect registered project source.',
            'relative_path_required': 'Choose a file within the registered project using a relative path.',
            'path_escape': 'I can inspect files inside the registered project; that path goes outside it.',
            'protected_path': 'That file is protected because it may contain secrets, credentials or security controls. I can inspect allowed source files.',
            'protected_content': 'That file contains protected content. I can continue with other allowed source files.',
            'linked_or_special_file': 'That file is linked or is not a regular source file, so I cannot safely read it.',
            'path_unavailable_or_unsafe': 'That file is missing or cannot be safely opened. I can inspect other allowed files.',
            'directory_required': 'Choose a directory inside the registered project for this listing.',
            'file_size_limit': 'That file exceeds the safe read size. I can inspect smaller source files.',
            'utf8_required': 'That file is not readable UTF-8 text.',
            'binary_content': 'That file is binary; source inspection supports text files.',
            'read_only_operation_required': 'I can inspect this project source, but cannot change files, control its live services or execute trades.',
            'project_tests_disabled': 'This project permits source inspection. Running its tests or runtime is not authorized.',
            'unknown_project': 'Which registered project should I work on? Specify its identity before engineering.',
            'project_ambiguous': 'Which registered project should I inspect?',
            'project_context_unavailable': 'The current project catalog is unavailable, so I cannot choose its source safely. A known registered project can still be inspected by its identity.',
            'deployment_status_unavailable': 'The deployment controller snapshot is unavailable, so I cannot confirm release status or prepare a verified deployment preview.',
            'git_metadata_required': 'Git history is unavailable here. Source files can still be inspected.',
            'development_checkout_required': 'The development workspace is unavailable for engineering. Registered source inspection is separate.',
            'legacy_rejection': 'The connected host is running an older dispatcher that does not accept this source read. Its dispatcher needs updating.',
            'unavailable': 'The source inspection host is not responding. I can use available project metadata, but cannot confirm its source or runtime.',
            'invalid_response': 'The source inspection host returned an invalid response; source access could not be confirmed.',
            'mailbox_unsafe': 'The connection to the source inspection host is not safe to use.',
            'mailbox_limit': 'The source inspection request exceeds the connection size limit.',
            'mailbox_full': 'The source inspection host is busy. Available metadata can still be assessed.',
        }
        return dict(code='denied', gate=self.gate, reason=self.reason,
                    message=messages.get(self.reason, 'That action is outside the authorized tools for this request. I can continue with permitted inspection.'))


class ToolPolicy:
    """Protected paths are omitted from discovery and rejected on direct access.

    Additional protected paths may be supplied by trusted application code.
    There is deliberately no option to remove the built-in protections.
    """

    _directories = frozenset({
        ".git", ".venv", "venv", ".agents", ".codex", "policy", "policies",
        "security", ".ssh", ".aws", ".gnupg", ".docker", "state", "__pycache__", ".pytest_cache",
    })
    _files = frozenset({"agents.md", "skill.md", ".gitmodules", ".gitattributes"})

    def __init__(self, additional_protected: tuple[str, ...] = ()):
        self._additional = tuple(self.parts(path) for path in additional_protected)

    @staticmethod
    def parts(path: str) -> tuple[str, ...]:
        if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
            raise ToolDenied("Expected a relative repository path", reason='relative_path_required')
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ToolDenied("Paths must stay inside the repository", reason='path_escape')
        return parsed.parts

    def check(self, path: str) -> tuple[str, ...]:
        parts = self.parts(path)
        for part in parts:
            name = part.casefold()
            stem = name.split(".")[0]
            if (name in self._directories or name in self._files
                    or name == ".env" or name.startswith(".env.")
                    or name.startswith(".env-") or name.startswith(".env_")
                    or (not name.endswith('.py') and (stem in {"security", "policy", "policies"}
                        or name.startswith(("security_", "security-", "policy_", "policy-"))))
                    or any(word in name for word in ("credential", "secret", "private_key", "private-key"))
                    or (not name.endswith('.py') and any(word in name for word in ('password', 'token')))
                    or any(suffix in name for suffix in (".sqlite", ".db", ".p12", ".pfx", ".kdbx"))
                    or name.endswith(("-policy.json", ".pem", ".key"))):
                raise ToolDenied("Protected repository path", reason='protected_path')
        if any(parts[:len(prefix)] == prefix for prefix in self._additional):
            raise ToolDenied("Protected repository path", reason='protected_path')
        return parts
