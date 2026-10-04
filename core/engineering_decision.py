"""Small conservative preflight policy; decisions are evidence, never permission."""
import json
import re
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field
from tools.schemas import StrictModel
from tools.policy import ToolDenied
from .candidate_action import goal_digest
from .database import timestamp, valid_id
from .engineering_portfolio import Identifier, Portfolio, PortfolioSnapshot

Outcome = Literal['USE_EXISTING', 'INTEGRATE', 'BUILD', 'NEED_MORE_INFORMATION']


class DecisionArguments(StrictModel):
    project_id: Identifier | None = None
    required_capabilities: Annotated[list[Identifier], Field(max_length=16)] = Field(default_factory=list)


class Decision(StrictModel):
    id: str = Field(pattern=r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$')
    goal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    project_id: Identifier | None
    outcome: Outcome
    rationale: str = Field(min_length=1, max_length=2000)
    capability_ids: list[Identifier] = Field(max_length=16)
    capability_states: dict[str, str]
    missing_capabilities: list[Identifier] = Field(max_length=16)
    appropriate_resources: list[Identifier] = Field(max_length=16)
    execution_scope: Literal['configured_dev_only'] = 'configured_dev_only'
    observed_at: str
    task_id: str | None = None


def associate(goal, projects, explicit=None):
    ids = {p['project_id'] for p in projects}
    if explicit:
        if explicit not in ids:
            raise ToolDenied('Unknown portfolio project')
        return explicit
    text = goal.casefold()
    matches = {pid for pid, aliases in {
        'jarvis-core': ('jarvis-core', 'jarvis'), 'trading-bot': ('trading-bot', 'trading bot'),
        'creator-os': ('creator-os', 'creatoros', 'creator os')}.items()
        if pid in ids and any(re.search(r'\b' + re.escape(alias) + r'\b', text) for alias in aliases)}
    # "Connect trading bot to Jarvis" refers to the external project.
    if len(matches) > 1: matches.discard('jarvis-core')
    return next(iter(matches)) if len(matches) == 1 else None


class Decisions:
    def __init__(self, database):
        self.database = database
        with database.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS portfolio_decisions (id TEXT PRIMARY KEY, task_id TEXT, record TEXT NOT NULL)')

    def evaluate(self, goal, snapshot, *, project_id=None, required_capabilities=(), task_id=None, engineering=False):
        from tools.candidate_edit import safe_text
        safe_text(goal)
        args = DecisionArguments(project_id=project_id, required_capabilities=list(required_capabilities))
        snapshot = PortfolioSnapshot.model_validate(snapshot).model_dump()
        project_id = associate(goal, snapshot['projects'], args.project_id)
        project = next((p for p in snapshot['projects'] if p['project_id'] == project_id), None)
        caps = {c['capability_id']: c for c in snapshot['capabilities']}
        text = goal.casefold()
        required = list(args.required_capabilities)
        # Only recognized existing feature requests get automatic reuse. A change
        # to an existing implementation is engineering, not mere registration.
        reuse = re.search(r'\b(?:add|enable|use|set up) (?:claude(?: code)?|codex)(?: (?:code )?support| provider)?[.!]?$', text)
        integrate = re.search(r'\b(?:connect|integrate|link)\b', text)
        if reuse and not required:
            required = ['claude-code-engineering' if 'claude' in reuse[0] else 'codex-engineering', 'provider-routing']
        elif integrate and not required:
            required = ['repo-files', 'engineering-jobs', 'provider-routing']
        missing = [cid for cid in required if cid not in caps or not caps[cid]['registered']]
        outcome, rationale = 'NEED_MORE_INFORMATION', 'The goal does not establish a specific existing operation, integration boundary or engineering change.'
        if project and project['status'] == 'needs_discovery':
            rationale = 'Project identity is known, but its authoritative repository and runtime need discovery before engineering.'
        elif reuse and not missing:
            outcome, rationale = 'USE_EXISTING', 'Provider support and routing already exist. Use the existing adapter; authentication and remote usability must be checked separately.'
        elif integrate and project:
            if missing:
                rationale = 'Integration prerequisites are missing; establish access and the requested interaction before choosing new engineering.'
            else:
                outcome, rationale = 'INTEGRATE', 'Use existing repo inspection, jobs and routing to define the integration boundary. External process/SSH access is not established; confirm the desired interaction and runtime before implementation.'
        elif integrate:
            rationale = 'Identify the project and desired integration boundary; do not invent a protocol from an unspecified connection request.'
        elif required:
            if missing and re.search(r'\b(?:build|implement|create|add)\b', text):
                outcome, rationale = 'BUILD', 'The requested capability is absent from current registrations; a scoped implementation is justified after requirements and access are established.'
            elif missing:
                rationale = 'The requested capabilities are not registered; more requirements or access evidence are needed.'
            elif all(caps[cid]['current_usability'] == 'available' and
                     (not caps[cid]['project_scope'] or project_id in caps[cid]['project_scope']) for cid in required):
                outcome, rationale = 'USE_EXISTING', 'All requested capabilities have fresh usable evidence within this project scope.'
            else:
                rationale = 'Existing capabilities are unavailable, unproven or outside the project scope; resolve those conditions before execution.'
        elif engineering or re.search(r'\b(?:fix|build|implement|create|add|change|improve|update|repair)\b', text):
            outcome, rationale = 'BUILD', 'A specific engineering change was requested; preserve existing systems and use the controlled candidate workflow in configured dev.'
        if project and project['read_only'] and outcome == 'BUILD':
            outcome, rationale = 'NEED_MORE_INFORMATION', 'This project permits metadata inspection only. Define a Jarvis-side integration task; the project reference grants no editing or runtime access.'
        relevant = required or (['codex-engineering', 'claude-code-engineering', 'engineering-jobs', 'repo-files', 'testing', 'independent-review'] if outcome == 'BUILD' else [])
        resources = [cid for cid in relevant if cid in caps and caps[cid]['registered'] and
                     caps[cid]['current_usability'] in {'available', 'unknown', 'unproven'} and
                     (not caps[cid]['project_scope'] or project_id in caps[cid]['project_scope'])]
        # The existing provider router, rather than this preflight, owns actual
        # provider selection, bounded unknown attempts and independent review.
        record = Decision(id=str(uuid4()), goal_digest=goal_digest(goal), project_id=project_id,
            outcome=outcome, rationale=rationale, capability_ids=relevant[:16],
            capability_states={cid: caps[cid]['current_usability'] for cid in relevant if cid in caps},
            missing_capabilities=missing, appropriate_resources=resources[:16],
            observed_at=timestamp(), task_id=task_id if valid_id(task_id) else None).model_dump()
        with self.database.transaction() as db:
            db.execute('INSERT INTO portfolio_decisions VALUES (?,?,?)', (record['id'], record['task_id'], json.dumps(record)))
        return record

    def recent(self, *, task_id=None):
        with self.database.transaction() as db:
            return [Decision.model_validate(json.loads(r[0])).model_dump() for r in db.execute(
                'SELECT record FROM portfolio_decisions WHERE (? IS NULL OR task_id=?) ORDER BY rowid DESC LIMIT 5', (task_id, task_id))]


def preflight(database, goal, *, project_id=None, required_capabilities=(), task_id=None, engineering=False):
    from .engineering_providers import EngineeringRegistry
    from .self_edit import model_request
    snapshot = Portfolio(database).snapshot(provider_registry=EngineeringRegistry.defaults(database, model_request).snapshot())
    return Decisions(database).evaluate(goal, snapshot, project_id=project_id,
        required_capabilities=required_capabilities, task_id=task_id, engineering=engineering)
