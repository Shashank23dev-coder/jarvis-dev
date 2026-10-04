"""Executive-owned resources in existing SQLite storage; records grant no authority."""
import json
from pathlib import Path
import re
import subprocess
from typing import Annotated, Literal

from pydantic import Field, field_validator
from tools.schemas import StrictModel
from .database import timestamp
from .engineering_providers import utc_time

Identifier = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
Names = Annotated[list[Text], Field(max_length=32)]
Usability = Literal['available', 'unavailable', 'unproven', 'unknown',
                    'not_authenticated', 'quota_blocked', 'invocation_failed']
Category = Literal['reasoning_provider', 'engineering_provider', 'research',
    'repo_filesystem', 'sandbox', 'testing', 'persistent_jobs', 'deployment',
    'memory', 'evaluation_review', 'external_integration', 'browser_web', 'managed_agent', 'self_state', 'routing']


class Evidence(StrictModel):
    source: Literal['native_registration', 'user_baseline', 'project_git', 'provider_registry',
        'bound_tools', 'brain_route', 'task_runtime', 'sandbox_result', 'controller_observation']
    scope: Text
    observed_at: str

    @field_validator('observed_at')
    @classmethod
    def date(cls, value):
        return utc_time(value).replace(tzinfo=None).isoformat()


class Project(StrictModel):
    project_id: Identifier
    name: Text
    description: Text
    category: Text
    status: Literal['active', 'inactive', 'archived', 'needs_discovery', 'unproven']
    repository: Text | None = None
    runtime_notes: Names = Field(default_factory=list)
    executive: Literal['Jarvis'] = 'Jarvis'
    dependencies: Names = Field(default_factory=list)
    capabilities: Names = Field(default_factory=list)
    limitations: Names = Field(default_factory=list)
    read_only: bool = False
    baseline_commit: Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')] | None = None
    current_commit: Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')] | None = None
    repository_state: Literal['present', 'unavailable', 'unknown'] = 'unknown'
    last_inspected: str | None = None
    last_updated: str
    evidence: Evidence

    @field_validator('last_updated', 'last_inspected')
    @classmethod
    def date(cls, value):
        return Evidence.date(value) if value else None


class Capability(StrictModel):
    capability_id: Identifier
    name: Text
    category: Category
    system: Text
    kind: Literal['native', 'external', 'managed'] = 'native'
    registered: bool = True
    configured: bool | None = None
    current_usability: Usability = 'unproven'
    task_types: Names = Field(default_factory=list)
    project_scope: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)
    limitations: Names = Field(default_factory=list)
    cost_notes: Text | None = None
    latency_notes: Text | None = None
    last_success: Evidence | None = None
    last_failure: Evidence | None = None
    evidence: Evidence
    freshness_seconds: int = Field(default=300, ge=1, le=86400)


class PortfolioSnapshot(StrictModel):
    executive: Literal['Jarvis'] = 'Jarvis'
    projects: Annotated[list[Project], Field(max_length=32)]
    capabilities: Annotated[list[Capability], Field(max_length=64)]
    contradictions: Annotated[list[dict], Field(max_length=64)] = Field(default_factory=list)


class Portfolio:
    """Host-only registration. No model-selected paths, executables, or probes."""
    def __init__(self, database, *, clock=timestamp):
        self.database, self.clock = database, clock
        with database.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS portfolio_projects (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS portfolio_capabilities (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        self.seed()

    def _register(self, table, record, key, *, replace=False):
        from tools.candidate_edit import safe_text
        value = record.model_dump()
        safe_text(json.dumps(value))
        with self.database.transaction() as db:
            count = db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
            exists = db.execute('SELECT 1 FROM ' + table + ' WHERE id=?', (value[key],)).fetchone()
            if not exists and count >= (32 if table == 'portfolio_projects' else 64):
                raise ValueError('Portfolio registration limit')
            db.execute(('INSERT OR REPLACE' if replace else 'INSERT OR IGNORE') +
                       ' INTO ' + table + ' VALUES (?,?)', (value[key], json.dumps(value)))
        return record

    def register_project(self, record, *, replace=False):
        return self._register('portfolio_projects', Project.model_validate(record), 'project_id', replace=replace)

    def register_capability(self, record, *, replace=False):
        return self._register('portfolio_capabilities', Capability.model_validate(record), 'capability_id', replace=replace)

    def evidence(self, source, scope):
        return Evidence(source=source, scope=scope, observed_at=self.clock())

    def seed(self):
        native = self.evidence('native_registration', 'implemented; runtime execution unproven')
        source = str(Path(__file__).resolve().parents[1])
        projects = [dict(project_id='jarvis-core', name='Jarvis', description='Jarvis Executive and its native systems.',
            category='assistant', status='active', repository=source,
            runtime_notes=['Linux development checkout; production is a separate controlled runtime.'],
            dependencies=['SQLite', 'Python', 'model providers'], capabilities=['provider-routing', 'engineering-jobs', 'candidate-workflow'],
            limitations=['Apply and deployment require separate human approval.']),
            dict(project_id='trading-bot', name='Trading Bot', description='Independent Windows-origin trading project.',
            category='trading', status='unproven', repository='/srv/jarvis/projects/trading-bot',
            baseline_commit='e03c307c64fdaaf267ecb7be3898325af555f966', read_only=True,
            runtime_notes=['Windows runtime is external; Linux compatibility and runtime are unproven.'],
            limitations=['Metadata inspection only; no file changes, credentials, runtime or service operations.']),
            dict(project_id='creator-os', name='CreatorOS', description='Creator operating-system project; conceptual identity only.',
            category='creator', status='needs_discovery', limitations=['Authoritative repository, location and runtime unknown.'])]
        for project in projects:
            self.register_project(dict(project, last_updated=self.clock(), evidence=self.evidence('user_baseline', 'initial project identity').model_dump()))
        capabilities = [
            ('codex-engineering', 'Codex engineering', 'engineering_provider', 'codex', 'external', ['code', 'documentation'], ['Live provider evidence required.']),
            ('claude-code-engineering', 'Claude Code engineering', 'engineering_provider', 'claude_code', 'external', ['code', 'documentation'], ['Installation is not authentication or proven usability.']),
            ('jarvisbrain-reasoning', 'OpenAI / JarvisBrain reasoning', 'reasoning_provider', 'JarvisBrain', 'native', ['reasoning', 'planning'], ['Current route may use a different configured provider or legacy fallback.']),
            ('engineering-jobs', 'Persistent engineering jobs', 'persistent_jobs', 'EngineeringJobs', 'native', ['code', 'documentation'], ['Worker heartbeat required for execution; configured dev checkout only.']),
            ('docker-sandbox', 'Docker sandbox', 'sandbox', 'tools.sandbox', 'native', ['testing'], ['Hardened runtime/image probe required; no host execution fallback.']),
            ('repo-files', 'Repository and file tools', 'repo_filesystem', 'RepositoryTools', 'native', ['inspect_repository', 'read_files'], ['Configured repository only; protected files denied; no arbitrary external process or SSH access.']),
            ('testing', 'Isolated testing', 'testing', 'tools.sandbox', 'native', ['testing'], ['A registered test tool does not prove a usable sandbox.']),
            ('independent-review', 'Independent review and evaluation', 'evaluation_review', 'SelfEditService', 'native', ['review'], ['Requires a usable non-author provider and test evidence.']),
            ('deployment-pipeline', 'Controlled deployment pipeline', 'deployment', 'deployment controller', 'native', ['deployment'], ['Separate human approval; controller availability unproven.']),
            ('memory', 'Persistent memory and experience', 'memory', 'SQLite memory / experience', 'native', ['memory'], ['Task access and existing retention/privacy rules apply.']),
            ('self-state', 'Runtime self-state', 'self_state', 'SelfState', 'native', ['capability_status'], ['Bounded observations; unknown is not absent.']),
            ('provider-routing', 'Engineering provider routing', 'routing', 'EngineeringRegistry', 'native', ['route_engineering'], ['Codex then Claude; usable independent reviewers exclude authors.'])]
        for cid, name, category, system, kind, tasks, limits in capabilities:
            self.register_capability(dict(capability_id=cid, name=name, category=category, system=system,
                kind=kind, task_types=tasks, limitations=limits, evidence=native.model_dump(),
                project_scope=['jarvis-core'] if cid in {'repo-files', 'engineering-jobs', 'testing', 'docker-sandbox', 'deployment-pipeline'} else []))

    def projects(self):
        with self.database.transaction() as db:
            return [Project.model_validate(json.loads(r[0])).model_dump() for r in
                    db.execute('SELECT record FROM portfolio_projects ORDER BY id LIMIT 32')]

    def inspect_project(self, project_id):
        """Optional Git identity probe only, never status, file contents or runtime."""
        roots = {'jarvis-core': Path(__file__).resolve().parents[1], 'trading-bot': Path('/srv/jarvis/projects/trading-bot')}
        if project_id not in roots:
            return None
        root = roots[project_id]
        value = next(p for p in self.projects() if p['project_id'] == project_id)
        value.update(repository=str(root), current_commit=None, repository_state='unavailable',
                     last_inspected=self.clock(), last_updated=self.clock(),
                     evidence=self.evidence('project_git', 'repository identity only; runtime unproven').model_dump())
        try:
            if root.is_symlink() or (root / '.git').is_symlink():
                raise ValueError('Unsafe repository identity')
            result = subprocess.run(['/usr/bin/git', '--no-optional-locks', '-C', str(root),
                'rev-parse', '--show-toplevel', 'HEAD'], capture_output=True, timeout=3,
                env={'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})
            lines = result.stdout.decode('ascii').splitlines() if len(result.stdout) < 8192 else []
            if result.returncode == 0 and len(lines) == 2 and lines[0] == str(root) and re.fullmatch('[0-9a-f]{40}', lines[1]):
                value.update(repository_state='present', current_commit=lines[1])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self.register_project(value, replace=True)
        return value

    def snapshot(self, *, provider_registry=None, observations=None):
        """Fresh observations overlay saved claims; never infer execution from config."""
        with self.database.transaction() as db:
            capabilities = [Capability.model_validate(json.loads(r[0])).model_dump() for r in
                            db.execute('SELECT record FROM portfolio_capabilities ORDER BY id LIMIT 64')]
        changes = dict(observations or {})
        if provider_registry is not None:
            from .engineering_providers import project_registry
            for provider in project_registry(provider_registry)['providers']:
                cid = {'codex': 'codex-engineering', 'claude_code': 'claude-code-engineering'}.get(provider['provider_id'])
                if not cid: continue
                def evidence(item):
                    return dict(source='provider_registry', scope=item['scope'], observed_at=item['observed_at']) if item else None
                observation = provider['observation']
                changes[cid] = dict(configured=provider['configured'] and provider['enabled'],
                    current_usability=provider['current_usability'],
                    evidence=evidence(observation) or self.evidence('provider_registry', 'unobserved').model_dump(),
                    last_success=evidence(provider['last_success']), last_failure=evidence(provider['last_failure']))
        conflicts = []
        for index, value in enumerate(capabilities):
            saved = value.copy()
            age = (utc_time(self.clock()) - utc_time(value['evidence']['observed_at'])).total_seconds()
            if not 0 <= age < value['freshness_seconds']:
                value['current_usability'] = 'unknown'
            if value['capability_id'] in changes:
                value.update(changes[value['capability_id']])
                value = Capability.model_validate(value).model_dump()
                for field in ('configured', 'current_usability'):
                    if saved[field] not in {None, 'unknown', 'unproven'} and saved[field] != value[field]:
                        conflicts.append(dict(resource=value['capability_id'], field=field, registry=saved[field], runtime=value[field]))
            # Update the list object, including validated fresh projections.
            age = (utc_time(self.clock()) - utc_time(value['evidence']['observed_at'])).total_seconds()
            if not 0 <= age < value['freshness_seconds']:
                value['current_usability'] = 'unknown'
            capabilities[index] = value
        projects = self.projects()
        for p in projects:
            if p['baseline_commit'] and p['current_commit'] and p['baseline_commit'] != p['current_commit']:
                conflicts.append(dict(resource=p['project_id'], field='commit', registry=p['baseline_commit'], runtime=p['current_commit']))
            if p['project_id'] == 'jarvis-core':
                root = str(Path(__file__).resolve().parents[1])
                if p['repository'] != root:
                    conflicts.append(dict(resource=p['project_id'], field='repository', registry=p['repository'], runtime=root))
                    p['repository'] = root
        return PortfolioSnapshot(projects=projects, capabilities=capabilities, contradictions=conflicts[:64]).model_dump()
