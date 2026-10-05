"""Executive-owned resources in existing SQLite storage; records grant no authority."""
import json
from pathlib import Path
from typing import Annotated, Literal
from types import MappingProxyType

from pydantic import Field, field_validator
from tools.schemas import StrictModel
from .database import timestamp
from .engineering_providers import utc_time

PROJECT_ROOTS = MappingProxyType({'jarvis-core': Path('/home/ubuntu/jarvis-dev'),
                                'trading-bot': Path('/srv/jarvis/projects/trading-bot')})

Identifier = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
Names = Annotated[list[Text], Field(max_length=32)]
Usability = Literal['available', 'unavailable', 'unproven', 'unknown',
                    'not_authenticated', 'quota_blocked', 'invocation_failed']
Category = Literal['reasoning_provider', 'engineering_provider', 'research',
    'repo_filesystem', 'sandbox', 'testing', 'persistent_jobs', 'deployment',
    'memory', 'evaluation_review', 'external_integration', 'browser_web', 'managed_agent', 'self_state', 'routing', 'engineering_workflow']


class Evidence(StrictModel):
    source: Literal['native_registration', 'user_baseline', 'project_git', 'provider_registry',
        'bound_tools', 'brain_route', 'task_runtime', 'sandbox_result', 'controller_observation']
    scope: Text
    observed_at: str

    @field_validator('observed_at')
    @classmethod
    def date(cls, value):
        return utc_time(value).replace(tzinfo=None).isoformat()


class RepositoryObservation(StrictModel):
    available: bool
    reason: Literal['available', 'project_unknown', 'location_unknown', 'root_missing',
        'root_inaccessible', 'root_unsafe', 'git_unavailable', 'baseline_conflict']
    root: Text | None = None
    current_commit: Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')] | None = None
    baseline_conflict: bool = False
    observed_at: str

    @field_validator('observed_at')
    @classmethod
    def date(cls, value):
        return Evidence.date(value)


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
    repository_inspection: RepositoryObservation | None = None
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
    contradictions: Annotated[list['ResourceContradiction'], Field(max_length=64)] = Field(default_factory=list)


class ResourceContradiction(StrictModel):
    resource: Identifier
    field: Literal['configured', 'current_usability', 'commit', 'repository']
    registry: Text | bool | None
    runtime: Text | bool | None


class ProjectArguments(StrictModel):
    project_id: Identifier


class ProjectEvidence(StrictModel):
    project: Project
    contradictions: Annotated[list[ResourceContradiction], Field(max_length=64)] = Field(default_factory=list)


def overlay_capabilities(capabilities, changes, now):
    result, conflicts = [], []
    for saved in capabilities:
        value = Capability.model_validate(dict(saved, **changes.get(saved['capability_id'], {}))).model_dump()
        age = (utc_time(now) - utc_time(value['evidence']['observed_at'])).total_seconds()
        if not 0 <= age < value['freshness_seconds']:
            value['current_usability'] = 'unknown'
        if saved['capability_id'] in changes:
            for field in ('configured', 'current_usability'):
                if saved[field] not in {None, 'unknown', 'unproven'} and saved[field] != value[field]:
                    conflicts.append(dict(resource=value['capability_id'], field=field, registry=saved[field], runtime=value[field]))
        result.append(value)
    return result, conflicts


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
        source = str(PROJECT_ROOTS['jarvis-core'])
        projects = [dict(project_id='jarvis-core', name='Jarvis', description='Jarvis Executive and its native systems.',
            category='assistant', status='active', repository=source,
            runtime_notes=['Linux development checkout; production is a separate controlled runtime.'],
            dependencies=['SQLite', 'Python', 'model providers'], capabilities=['provider-routing', 'engineering-jobs', 'candidate-workflow'],
            limitations=['Routine development is autonomous; consequential promotion and deployment require owner approval.']),
            dict(project_id='trading-bot', name='Trading Bot', description='Independent Windows-origin trading project.',
            category='trading', status='unproven', repository='/srv/jarvis/projects/trading-bot',
            baseline_commit='e03c307c64fdaaf267ecb7be3898325af555f966', read_only=True,
            runtime_notes=['Windows runtime is external; Linux compatibility and runtime are unproven.'],
            limitations=['Registered source supports inspection and isolated development; Windows runtime is unverified; credentials remain protected.']),
            dict(project_id='creator-os', name='CreatorOS', description='Creator operating-system project; conceptual identity only.',
            category='creator', status='needs_discovery', limitations=['Authoritative repository, location and runtime unknown.'])]
        for project in projects:
            self.register_project(dict(project, last_updated=self.clock(), evidence=self.evidence('user_baseline', 'initial project identity').model_dump()))
        # Upgrade only the previous native seed's obsolete description. Existing
        # host records, roots, baselines and read-only flags remain authoritative.
        for project in self.projects():
            obsolete = {
                'Metadata inspection only; no file changes, credentials, runtime or service operations.': projects[1]['limitations'][0],
                'Bounded read-only repository inspection; no file changes, credentials, runtime or service operations.': projects[1]['limitations'][0],
                'Apply and deployment require separate human approval.': projects[0]['limitations'][0],
            }
            if project['project_id'] in {'trading-bot', 'jarvis-core'} and any(limit in obsolete for limit in project['limitations']):
                project['limitations'] = [obsolete.get(limit, limit) for limit in project['limitations']]
                self.register_project(project, replace=True)
        capabilities = [
            ('codex-engineering', 'Codex engineering', 'engineering_provider', 'codex', 'external', ['code', 'documentation'], ['Live provider evidence required.']),
            ('claude-code-engineering', 'Claude Code engineering', 'engineering_provider', 'claude_code', 'external', ['code', 'documentation'], ['Installation is not authentication or proven usability.']),
            ('jarvisbrain-reasoning', 'OpenAI / JarvisBrain reasoning', 'reasoning_provider', 'JarvisBrain', 'native', ['reasoning', 'planning'], ['Current route may use a different configured provider or legacy fallback.']),
            ('engineering-jobs', 'Persistent engineering jobs', 'persistent_jobs', 'EngineeringJobs', 'native', ['code', 'documentation'], ['Worker heartbeat required for execution; configured dev checkout only.']),
            ('docker-sandbox', 'Docker sandbox', 'sandbox', 'tools.sandbox', 'native', ['testing'], ['Hardened runtime/image probe required; no host execution fallback.']),
            ('repo-files', 'Repository and file tools', 'repo_filesystem', 'RepositoryTools', 'native', ['inspect_repository', 'read_files'], ['Configured repository only; protected files denied; no arbitrary external process or SSH access.']),
            ('testing', 'Isolated testing', 'testing', 'tools.sandbox', 'native', ['testing'], ['A registered test tool does not prove a usable sandbox.']),
            ('independent-review', 'Independent review and evaluation', 'evaluation_review', 'SelfEditService', 'native', ['review'], ['Requires a usable non-author provider and test evidence.']),
            ('candidate-workflow', 'Controlled candidate workflow', 'engineering_workflow', 'SelfEditService', 'native', ['code', 'documentation'], ['Isolated registered-project candidates; consequential promotion needs approval.']),
            ('candidate-apply', 'Controlled candidate apply', 'engineering_workflow', 'CandidateApply', 'native', ['apply_candidate'], ['Routine reviewed development apply is autonomous; consequential promotion requires exact approval; no deployment.']),
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
        """Refresh registered identity via the same bounded repository adapter."""
        project = next((p for p in self.projects() if p['project_id'] == project_id), None)
        if not project or not project['repository']:
            return None
        from .engineering_repository import resolve_repository
        resolve_repository(self.database, project_id)
        return next(p for p in self.projects() if p['project_id'] == project_id)

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
        projects = self.projects()
        inspected = [p for p in projects if p['repository_inspection'] and
            0 <= (utc_time(self.clock()) - utc_time(p['repository_inspection']['observed_at'])).total_seconds() < 300]
        if inspected:
            usable = [p['project_id'] for p in inspected if p['repository_inspection']['available']]
            latest = max(p['repository_inspection']['observed_at'] for p in inspected)
            changes['repo-files'] = dict(current_usability='available' if usable else 'unavailable', configured=True,
                project_scope=usable, evidence=dict(source='project_git', scope='registered project root/Git access; individual paths remain policy-checked', observed_at=latest))
        capabilities, conflicts = overlay_capabilities(capabilities, changes, self.clock())
        for p in projects:
            if p['baseline_commit'] and p['current_commit'] and p['baseline_commit'] != p['current_commit']:
                conflicts.append(dict(resource=p['project_id'], field='commit', registry=p['baseline_commit'], runtime=p['current_commit']))
        return PortfolioSnapshot(projects=projects, capabilities=capabilities, contradictions=conflicts[:64]).model_dump()

    def repository_snapshot(self, *, provider_registry=None):
        from .engineering_repository import resolve_repository
        for project in self.projects():
            resolve_repository(self.database, project['project_id'])
        return self.snapshot(provider_registry=provider_registry)


def inspect_project_record(database, project_id):
    from tools.policy import ToolDenied
    portfolio = Portfolio(database)
    project = next((p for p in portfolio.projects() if p['project_id'] == project_id), None)
    if project is None:
        raise ToolDenied('Unknown portfolio project')
    portfolio.inspect_project(project_id)
    snapshot = portfolio.snapshot()
    return ProjectEvidence(project=next(p for p in snapshot['projects'] if p['project_id'] == project_id),
        contradictions=[c for c in snapshot['contradictions'] if c['resource'] == project_id]).model_dump()
