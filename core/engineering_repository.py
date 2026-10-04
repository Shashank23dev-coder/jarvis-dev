"""Registered roots → existing RepositoryTools. Read-only owner broker adapter."""
import copy
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Literal

from pydantic import Field
from tools.policy import ToolDenied, ToolError
from tools.repository import RepositoryTools
from tools.registry import ToolRegistry, ToolDefinition
from tools.schemas import StrictModel, PathArgument, ProcessResult
from .database import timestamp
from .engineering_portfolio import Identifier, Portfolio, RepositoryObservation

Operation = Literal['inspect', 'list_files', 'read_text', 'search_text', 'git_status', 'git_diff']


class RepositoryArguments(StrictModel):
    project_id: Identifier
    operation: Operation = 'inspect'
    path: PathArgument | None = None
    query: str | None = Field(default=None, min_length=1, max_length=256)
    staged: bool = False

    def checked(self):
        if ((self.operation == 'read_text' and self.path is None)
                or (self.operation == 'search_text') != (self.query is not None)
                or (self.operation in {'inspect', 'git_status'} and self.path is not None)
                or (self.operation != 'git_diff' and self.staged)):
            raise ToolDenied('Invalid project repository operation')
        if self.path is not None:
            from tools.policy import ToolPolicy
            ToolPolicy().check(self.path)
        return self


class RepositoryEvidence(RepositoryObservation):
    project_id: Identifier
    registered_repository: str | None = None
    baseline_commit: str | None = Field(default=None, pattern=r'^[0-9a-f]{40}$')
    read_only: Literal[True] = True


class RepositoryResult(StrictModel):
    repository: RepositoryEvidence
    operation: Operation
    files: list[str] | None = None
    text: str | None = None
    process: ProcessResult | None = None
    truncated: bool = False


def resolve_repository(database, project_id):
    """A project ID is the only selector. No prompt, argument or cwd supplies root."""
    from pydantic import TypeAdapter
    TypeAdapter(Identifier).validate_python(project_id)
    portfolio = Portfolio(database)
    project = next((p for p in portfolio.projects() if p['project_id'] == project_id), None)
    evidence = dict(project_id=project_id, registered_repository=(project or {}).get('repository'),
        baseline_commit=(project or {}).get('baseline_commit'), available=False,
        reason='project_unknown' if project is None else 'location_unknown', observed_at=timestamp())
    repository = None
    root_value = evidence['registered_repository']
    if root_value:
        root = Path(root_value)
        try:
            if not root.is_absolute() or '..' in root.parts or str(root) != root_value:
                evidence['reason'] = 'root_unsafe'
            elif root.resolve(strict=True) != root:
                evidence['reason'] = 'root_unsafe'
            elif not root.is_dir():
                evidence['reason'] = 'root_inaccessible'
            elif not (root / '.git').is_dir():
                evidence['reason'] = 'git_unavailable'
            elif (root / '.git').is_symlink():
                evidence['reason'] = 'root_unsafe'
            else:
                # Preserve all native protections and subprocess bounds.
                repository = RepositoryTools(root, max_output_bytes=16000, timeout=3)
                if repository.root != root:
                    raise ToolDenied('Repository root changed')
                with repository._open('.', directory=True):
                    pass
                revision = repository.git_revision()
                evidence.update(current_commit=revision, root=str(root), available=True, reason='available')
                if evidence['baseline_commit'] and revision != evidence['baseline_commit']:
                    evidence.update(available=False, reason='baseline_conflict', baseline_conflict=True)
        except FileNotFoundError:
            evidence['reason'] = 'root_missing'
        except (PermissionError, OSError):
            evidence['reason'] = 'root_inaccessible'
        except ToolDenied:
            evidence['reason'] = 'root_inaccessible'
        except (ToolError, ValueError, RuntimeError):
            evidence['reason'] = 'git_unavailable'
    result = RepositoryEvidence(**evidence)
    if project is not None:
        observation = RepositoryObservation.model_validate({k: v for k, v in result.model_dump().items()
            if k in RepositoryObservation.model_fields}).model_dump()
        project.update(repository_inspection=observation, last_inspected=result.observed_at,
                       last_updated=result.observed_at, current_commit=result.current_commit,
                       repository_state='present' if result.current_commit else 'unavailable' if root_value else 'unknown')
        if result.current_commit:
            project['evidence'] = dict(source='project_git', scope='registered repository identity; runtime unproven', observed_at=result.observed_at)
        portfolio.register_project(project, replace=True)
    return result, repository if result.available else None


def project_result(value):
    """Closed response projection plus the existing mailbox's bounded payload."""
    result = RepositoryResult.model_validate(value).model_dump()
    if result['files'] is not None:
        result['files'] = result['files'][:200]
        result['truncated'] |= len(value.get('files') or []) > 200
    if result['text'] is not None and len(result['text']) > 16000:
        result['text'], result['truncated'] = result['text'][:16000], True
    while len(json.dumps(result).encode('utf-8')) > 32000:
        result['truncated'] = True
        if result['files']:
            result['files'] = result['files'][:len(result['files']) // 2]
        elif result['text']:
            result['text'] = result['text'][:len(result['text']) // 2]
        elif result['process'] and result['process']['stdout']:
            process = result['process']
            process['stdout'], process['truncated'] = process['stdout'][:len(process['stdout']) // 2], True
        else:
            raise ToolDenied('Repository evidence exceeds transport limit')
    return result


def inspect_repository(database, **arguments):
    args = RepositoryArguments(**arguments).checked()
    evidence, repository = resolve_repository(database, args.project_id)
    result = dict(repository=evidence.model_dump(), operation=args.operation)
    if repository is not None:
        from tools.candidate_edit import inspection_text
        def public_text(content):
            # Existing source redaction plus quoted JSON credential keys.
            content = re.sub(r'''(?i)((?:api[_-]?key|password|secret|access[_-]?token)["']\s*:\s*)(["'])([^"']{8,})(["'])''',
                lambda m: m[1] + m[2] + 'OMITTED' + m[4], content)
            return inspection_text(content)
        if args.operation == 'list_files':
            result['files'] = repository.list_files(args.path or '.')
        elif args.operation == 'read_text':
            result['text'] = public_text(repository.read_text(args.path))
        elif args.operation == 'search_text':
            raw = repository.search_text(args.query, args.path or '.')
            result['process'] = ProcessResult(returncode=raw.returncode, stdout=public_text(raw.stdout),
                stderr='', timed_out=raw.timed_out, truncated=raw.truncated).model_dump()
        elif args.operation in {'git_status', 'git_diff'}:
            raw = repository.git_status() if args.operation == 'git_status' else repository.git_diff(args.path, staged=args.staged)
            result['process'] = ProcessResult(returncode=raw.returncode, stdout=public_text(raw.stdout),
                stderr='', timed_out=raw.timed_out, truncated=raw.truncated).model_dump()
    return project_result(result)


def bind_project_tools(tools, registry):
    """Per-request copies; the shared/default native registry remains unchanged."""
    if tools.authority is None or not isinstance(registry, ToolRegistry): return registry
    from .engineering_decision import associate
    # Initial identities are host-owned. Production resolutions always happen
    # in the existing host broker's trusted database, not its local chat DB.
    known = [dict(project_id=pid) for pid in ('jarvis-core', 'trading-bot', 'creator-os')]
    project_id = associate(tools.goal, known)
    scoped, definitions = copy.copy(registry), dict(registry._definitions)
    definitions['inspect_project_repository'] = ToolDefinition(
        'Use existing bounded read-only repository tools on a trusted registered project ID. Operations: inspect Git identity, list_files, read_text, literal search_text, filtered git_status/git_diff. Relative paths only; no root override, execution, tests, writes or services. Returns structured unavailable/baseline conflict evidence.',
        RepositoryArguments, RepositoryResult, tools.inspect_repository)
    if project_id:
        for name in ('list_files', 'read_text', 'git_status', 'git_diff'):
            definition = definitions[name]
            def invoke(name=name, **arguments):
                return tools.inspect_repository(project_id=project_id, operation=name, **arguments)
            definitions[name] = ToolDefinition(definition.description + f' Scoped to registered {project_id}; no fallback root.',
                definition.arguments, RepositoryResult, invoke)
        def command(command):
            # Reuse the native grammar's read operations, never child-project pytest.
            if not command or any(any(char in arg for char in '\x00\n\r;|&><`$') for arg in command):
                raise ToolDenied('Shell syntax is disabled')
            if command == ['pwd']:
                return tools.inspect_repository(project_id=project_id)
            if command[0:1] == ['ls'] and len(command) <= 2:
                return tools.inspect_repository(project_id=project_id, operation='list_files', path=command[1] if len(command) == 2 else '.')
            if command[0:1] == ['cat'] and len(command) == 2:
                return tools.inspect_repository(project_id=project_id, operation='read_text', path=command[1])
            if command == ['git', 'status']:
                return tools.inspect_repository(project_id=project_id, operation='git_status')
            if command[:2] == ['git', 'diff']:
                args = command[2:]
                staged = bool(args and args[0] == '--cached')
                args = args[1:] if staged else args
                if len(args) <= 1 and not any(a.startswith('-') for a in args):
                    return tools.inspect_repository(project_id=project_id, operation='git_diff', path=args[0] if args else None, staged=staged)
            raise ToolDenied('Project repository context permits inspection only')
        definitions['run_command'] = ToolDefinition('Read-only native command facade in the registered project context; no project runtime or tests.',
            definitions['run_command'].arguments, RepositoryResult, command)
        if project_id != 'jarvis-core':
            def no_tests(**arguments): raise ToolDenied('External projects are inspection-only')
            previous = definitions['run_pytest']
            definitions['run_pytest'] = ToolDefinition('Child project context permits no test/runtime execution.',
                previous.arguments, previous.result, no_tests)
    scoped._definitions = MappingProxyType(definitions)
    return scoped
