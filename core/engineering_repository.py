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

Operation = Literal['inspect', 'list_files', 'read_text', 'search_text', 'git_status', 'git_diff', 'git_log', 'git_revision', 'run_pytest']


class RepositoryArguments(StrictModel):
    project_id: Identifier
    operation: Operation = 'inspect'
    path: PathArgument | None = None
    query: str | None = Field(default=None, min_length=1, max_length=256)
    staged: bool = False
    paths: list[PathArgument] | None = Field(default=None, min_length=1, max_length=8)

    def checked(self):
        if ((self.operation == 'read_text' and self.path is None)
                or (self.operation == 'search_text') != (self.query is not None)
                or (self.operation in {'inspect', 'git_status', 'git_log', 'git_revision'} and self.path is not None)
                or (self.operation != 'git_diff' and self.staged)
                or (self.operation != 'run_pytest' and self.paths is not None)):
            raise ToolDenied('Invalid project repository operation', reason='invalid_read_operation')
        # File safety belongs to RepositoryTools, once the registered root is known.
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
            elif (root / '.git').is_symlink():
                evidence['reason'] = 'root_unsafe'
            else:
                # Preserve all native protections and subprocess bounds.
                repository = RepositoryTools(root, max_output_bytes=16000, timeout=3)
                if repository.root != root:
                    raise ToolDenied('Repository root changed')
                with repository._open('.', directory=True):
                    pass
                evidence.update(root=str(root), available=True, reason='available')
                # Git observations describe the root; they never authorize reads.
                try:
                    revision = repository.git_revision()
                except (ToolError, ValueError, OSError):
                    evidence['reason'] = 'git_unavailable'
                else:
                    evidence.update(current_commit=revision,
                        baseline_conflict=bool(evidence['baseline_commit'] and revision != evidence['baseline_commit']))
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
                       repository_state='present' if result.available else 'unavailable' if root_value else 'unknown')
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
            raise ToolDenied('Repository evidence exceeds transport limit', reason='transport_limit')
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
            try:
                return inspection_text(content)
            except ToolDenied as exc:
                raise ToolDenied('Protected repository content', reason='protected_content') from exc
        if args.operation == 'list_files':
            result['files'] = repository.list_files(args.path or '.')
        elif args.operation == 'read_text':
            result['text'] = public_text(repository.read_text(args.path))
        elif args.operation == 'search_text':
            raw = repository.search_text(args.query, args.path or '.')
            result['process'] = ProcessResult(returncode=raw.returncode, stdout=public_text(raw.stdout),
                stderr='', timed_out=raw.timed_out, truncated=raw.truncated).model_dump()
        elif args.operation == 'git_revision':
            result['text'] = repository.git_revision()
        elif args.operation == 'run_pytest':
            raw = repository.run_pytest(args.paths or ['tests'])
            result['process'] = ProcessResult(returncode=raw.returncode, stdout=public_text(raw.stdout),
                stderr='', timed_out=raw.timed_out, truncated=raw.truncated).model_dump()
        elif args.operation in {'git_status', 'git_diff', 'git_log'}:
            raw = (repository.git_status() if args.operation == 'git_status' else
                   repository.git_log() if args.operation == 'git_log' else
                   repository.git_diff(args.path, staged=args.staged))
            result['process'] = ProcessResult(returncode=raw.returncode, stdout=public_text(raw.stdout),
                stderr='', timed_out=raw.timed_out, truncated=raw.truncated).model_dump()
    return project_result(result)


def bind_project_tools(tools, registry):
    """Per-request copies; the shared/default native registry remains unchanged."""
    if tools.authority is None or not isinstance(registry, ToolRegistry): return registry
    scoped, definitions = copy.copy(registry), dict(registry._definitions)
    # Store native definitions once: later rebinds must not wrap scoped handlers.
    if not hasattr(tools, '_native_repository_definitions'):
        tools._native_repository_definitions = {name: definitions[name] for name in
            ('list_files', 'read_text', 'git_status', 'git_diff', 'run_command', 'run_pytest')}
    native = tools._native_repository_definitions

    def selected():
        if tools.ambiguous_projects:
            raise ToolDenied('Choose a project before inspecting its source', reason='project_ambiguous')
        if not tools.project_id and not tools.portfolio_observed:
            raise ToolDenied('Project catalog could not be observed', reason='project_context_unavailable')
        # Unnamed reads keep the native read-only registry. Mutation and
        # preparation selectors use resolve_project and never default to Jarvis.
        return tools.project_id
    definitions['inspect_project_repository'] = ToolDefinition(
        'Inspect trusted registered project source or run isolated pytest. Roots come from Portfolio metadata; relative paths only. Capability inventory and baseline drift are observations. Tests run without host runtime or secrets.',
        RepositoryArguments, RepositoryResult, tools.inspect_repository)
    if tools.authority is not None:
        for name in ('list_files', 'read_text', 'git_status', 'git_diff'):
            definition = native[name]
            def invoke(name=name, **arguments):
                # Built-in secret/path exclusions apply even when project
                # discovery is unavailable. They require no root lookup.
                from tools.policy import ToolPolicy
                if arguments.get('path') is not None:
                    ToolPolicy().check(arguments['path'])
                project_id = selected()
                return (tools.inspect_repository(project_id=project_id, operation=name, **arguments)
                        if project_id else native[name].handler(**arguments))
            # Native default results and registered-project results share one
            # closed union; no unvalidated dictionary responses.
            definitions[name] = ToolDefinition(definition.description + ' Uses the project named in this request; ambiguous projects need clarification.',
                definition.arguments, RepositoryResult | definition.result, invoke)
        def command(command):
            project_id = selected()
            if project_id is None:
                return native['run_command'].handler(command=command)
            # Reuse the native grammar's read operations, never child-project pytest.
            if not command or any(any(char in arg for char in '\x00\n\r;|&><`$') for arg in command):
                raise ToolDenied('Shell syntax is disabled', reason='read_only_operation_required')
            if command == ['pwd']:
                return tools.inspect_repository(project_id=project_id)
            if command[0:1] == ['ls'] and len(command) <= 2:
                return tools.inspect_repository(project_id=project_id, operation='list_files', path=command[1] if len(command) == 2 else '.')
            if command[0:1] == ['cat'] and len(command) == 2:
                return tools.inspect_repository(project_id=project_id, operation='read_text', path=command[1])
            if command == ['git', 'status']:
                return tools.inspect_repository(project_id=project_id, operation='git_status')
            if command == ['git', 'log']:
                return tools.inspect_repository(project_id=project_id, operation='git_log')
            if command == ['git', 'rev-parse', 'HEAD']:
                return tools.inspect_repository(project_id=project_id, operation='git_revision')
            if command[:2] == ['git', 'diff']:
                args = command[2:]
                staged = bool(args and args[0] == '--cached')
                args = args[1:] if staged else args
                if len(args) <= 1 and not any(a.startswith('-') for a in args):
                    return tools.inspect_repository(project_id=project_id, operation='git_diff', path=args[0] if args else None, staged=staged)
            raise ToolDenied('Project repository context permits inspection only', reason='read_only_operation_required')
        definitions['run_command'] = ToolDefinition('Read-only native command facade in the registered project context; no project runtime or tests.',
            native['run_command'].arguments, RepositoryResult | native['run_command'].result, command)
        def tests(**arguments):
            project_id = selected()
            if project_id is not None:
                return tools.inspect_repository(project_id=project_id, operation='run_pytest', **arguments)
            return native['run_pytest'].handler(**arguments)
        previous = native['run_pytest']
        definitions['run_pytest'] = ToolDefinition('Run tests in a secret-free sandbox of the registered project. No host runtime access.',
            previous.arguments, RepositoryResult | previous.result, tests)
    scoped._definitions = MappingProxyType(definitions)
    return scoped


def development_service(service, project_id=None, *, candidate_id=None):
    """Select a registered source for isolated development; never accept a raw root."""
    if candidate_id:
        record = service.store.get(candidate_id, internal=True, recover=False)
        if not record:
            raise ToolDenied('Candidate unavailable')
        project_id = record.get('source_context', {}).get('project_id')
        if project_id is None:
            # Legacy evidence has no project ID. Only a unique registered root
            # can authorize further operations; never infer Jarvis by default.
            matches = [p['project_id'] for p in Portfolio(service.store.database).projects()
                       if p.get('repository') == record.get('source_root')]
            project_id = matches[0] if len(matches) == 1 else None
    if project_id is None:
        raise ToolDenied('Choose a registered project', reason='unknown_project')
    selected = service
    if project_id:
        evidence, repository = resolve_repository(service.store.database, project_id)
        if repository is None:
            raise ToolDenied('Registered project source is unavailable', reason='project_context_unavailable')
        if repository.root != service.source:
            from .self_edit import SelfEditService
            selected = SelfEditService(service.store.database, source_root=repository.root,
                candidate_root=service.root, limits=service.limits if service.explicit_limits else None,
                model=service.model, tester=service.tester)
            if hasattr(service, 'engineering_lock_fd'):
                selected.engineering_lock_fd = service.engineering_lock_fd
    if candidate_id and record.get('source_root') != str(selected.source):
        raise ToolDenied('Registered candidate source has changed')
    return selected
