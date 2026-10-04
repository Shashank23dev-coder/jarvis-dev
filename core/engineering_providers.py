"""Small trusted registry: timestamped evidence and bounded role routing."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from typing import Literal

from pydantic import Field, field_validator
from tools.schemas import StrictModel
from .candidate_provider import ProviderFailure, ProposalProvider
from .database import timestamp

State = Literal['available', 'unavailable', 'not_authenticated', 'quota_blocked',
                'invocation_failed', 'unknown']
FAILURE_STATES = {'provider_authentication': 'not_authenticated',
                  'provider_quota': 'quota_blocked', 'provider_rate_limit': 'quota_blocked',
                  'provider_unavailable': 'unavailable'}


def utc_time(value):
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Observation(StrictModel):
    state: State
    observed_at: str
    source: Literal['configuration', 'claude_auth_status', 'provider_response', 'provider_failure']
    scope: Literal['local_configuration', 'local_authentication', 'engineering_request']
    stage: Literal['implementing', 'reviewing'] | None = None
    code: Literal['provider_authentication', 'provider_quota', 'provider_rate_limit',
                  'provider_unavailable', 'provider_timeout', 'provider_invalid_response',
                  'provider_output_limit', 'provider_request_rejected', 'provider_invocation_failed',
                  'provider_no_changes'] | None = None

    @field_validator('observed_at')
    @classmethod
    def valid_timestamp(cls, value):
        return utc_time(value).replace(tzinfo=None).isoformat()


class ProviderState(StrictModel):
    provider_id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    provider_type: Literal['api', 'cli']
    capabilities: list[Literal['implementing', 'reviewing']]
    task_types: list[Literal['code', 'documentation']]
    enabled: bool
    configured: bool
    state: State = 'unknown'
    current_usability: State = 'unknown'
    observation: Observation | None = None
    last_success: Observation | None = None
    last_failure: Observation | None = None
    unavailability: Observation | None = None


class ProviderBlocker(StrictModel):
    code: Literal['engineering_providers_blocked'] = 'engineering_providers_blocked'
    stage: Literal['implementing', 'reviewing']
    reasons: list[ProviderState]
    excluded_providers: list[Literal['codex', 'openai', 'claude_code']] = Field(default_factory=list)
    budget_exhausted: bool = False


class RoutingBlocked(ProviderFailure):
    def __init__(self, stage, reasons, *, exclude=(), budget_exhausted=False):
        self.blocker = ProviderBlocker(stage=stage, reasons=reasons,
            excluded_providers=sorted(exclude), budget_exhausted=budget_exhausted).model_dump()
        super().__init__(reasons[0].provider_id if reasons else 'codex', 'provider_unavailable')


@dataclass(frozen=True)
class Registration:
    adapter: object
    provider_type: str
    configured: bool
    enabled: bool = True
    capabilities: tuple = ('implementing', 'reviewing')
    task_types: tuple = ('code', 'documentation')


class EngineeringRegistry:
    # Configuration and authentication probes never create positive usability.
    # Negative observations expire too, permitting a later bounded retry.
    ttl_seconds = 300

    def __init__(self, database, registrations, *, clock=timestamp):
        self.database, self.clock = database, clock
        self.registrations = {}
        for registration in registrations:
            name = registration.adapter.name
            ProviderState(provider_id=name, provider_type=registration.provider_type,
                capabilities=list(registration.capabilities), task_types=list(registration.task_types),
                enabled=registration.enabled, configured=registration.configured)
            if name in self.registrations or not callable(getattr(registration.adapter, 'complete', None)):
                raise ValueError('Invalid or duplicate engineering registration')
            self.registrations[name] = registration
        with database.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS engineering_provider_state (provider TEXT PRIMARY KEY, record TEXT NOT NULL)')

    @classmethod
    def defaults(cls, database, transport):
        from dotenv import dotenv_values
        from pathlib import Path
        from .candidate_claude import ClaudeCodeProvider, claude_executable
        # Match the existing SDK's dev .env fallback without projecting values.
        configured = bool(os.getenv('OPENAI_API_KEY') or
            (os.getenv('JARVIS_MODE', 'development') != 'production' and
             dotenv_values(Path(__file__).resolve().parents[1] / '.env').get('OPENAI_API_KEY')))
        return cls(database, [Registration(ProposalProvider('codex', transport), 'api', configured),
            Registration(ClaudeCodeProvider(transport), 'cli', bool(claude_executable())),
            Registration(ProposalProvider('openai', transport), 'api', configured)])

    def observe(self, provider, state, source, scope, code=None, *, stage=None):
        if provider not in self.registrations:
            raise ValueError('Unregistered engineering provider')
        observation = Observation(state=state, observed_at=self.clock(), source=source,
                                  scope=scope, code=code, stage=stage).model_dump()
        with self.database.transaction() as db:
            row = db.execute('SELECT record FROM engineering_provider_state WHERE provider=?', (provider,)).fetchone()
            value = json.loads(row[0]) if row else {}
            value['observation'] = observation
            if source == 'provider_response' and state == 'available':
                value['last_success'] = observation
            if state not in {'available', 'unknown'}:
                value['last_failure'] = observation
                if state in {'unavailable', 'not_authenticated', 'quota_blocked'}:
                    value['unavailability'] = observation
            db.execute('INSERT OR REPLACE INTO engineering_provider_state VALUES (?,?)', (provider, json.dumps(value)))

    def state(self, name):
        r = self.registrations[name]
        with self.database.transaction() as db:
            row = db.execute('SELECT record FROM engineering_provider_state WHERE provider=?', (name,)).fetchone()
        value = json.loads(row[0]) if row else {}
        result = ProviderState(provider_id=name, provider_type=r.provider_type, enabled=r.enabled,
            configured=r.configured, capabilities=list(r.capabilities), task_types=list(r.task_types), **value)
        state, usability = 'unknown', 'unknown'
        if result.observation:
            state = result.observation.state
            age = (utc_time(self.clock()) - utc_time(result.observation.observed_at)).total_seconds()
            if 0 <= age < self.ttl_seconds:
                usability = state
        if not r.enabled or not r.configured:
            state = usability = 'unavailable'
            result = result.model_copy(update=dict(observation=Observation(state=state,
                observed_at=self.clock(), source='configuration', scope='local_configuration',
                code='provider_unavailable')))
        return result.model_copy(update=dict(state=state, current_usability=usability))

    def refresh_claude(self, timeout=5, *, force=True):
        r = self.registrations.get('claude_code')
        if r and r.enabled and r.configured:
            current = self.state('claude_code')
            if not force and current.observation:
                age = (utc_time(self.clock()) - utc_time(current.observation.observed_at)).total_seconds()
                if 0 <= age < self.ttl_seconds:
                    return
            state = r.adapter.probe(timeout)
            # A login says nothing about remote quota/service availability.
            # Preserve a recent execution failure/success until it expires.
            if state != 'unknown' or current.current_usability == 'unknown' or current.state == 'not_authenticated':
                self.observe('claude_code', state, 'claude_auth_status', 'local_authentication',
                    'provider_authentication' if state == 'not_authenticated' else
                    'provider_unavailable' if state == 'unavailable' else None)

    def complete(self, payload, instructions, timeout, *, stage, task_type='code', exclude=(), validate=lambda x: x):
        import time
        deadline = time.monotonic() + timeout
        order = ('codex', 'claude_code') if stage == 'implementing' else ('openai', 'claude_code', 'codex')
        reasons = []
        for name in order:
            if name in exclude or name not in self.registrations:
                continue
            r = self.registrations[name]
            if stage not in r.capabilities or task_type not in r.task_types:
                continue
            state = self.state(name)
            if state.current_usability not in {'available', 'unknown'}:
                reasons.append(state)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RoutingBlocked(stage, reasons + [state], exclude=exclude, budget_exhausted=True)
            try:
                result = r.adapter.complete(payload, instructions, remaining)
                try:
                    validate(result)
                except ValueError:
                    raise ProviderFailure(name, 'provider_invalid_response') from None
            except ProviderFailure as exc:
                code = exc.code if exc.code.startswith('provider_') else 'provider_invocation_failed'
                self.observe(name, FAILURE_STATES.get(exc.code, 'invocation_failed'),
                             'provider_failure', 'engineering_request', code, stage=stage)
                reasons.append(self.state(name))
                continue
            except Exception:
                # Never reflect arbitrary adapter exception text.
                self.observe(name, 'invocation_failed', 'provider_failure',
                             'engineering_request', 'provider_invocation_failed', stage=stage)
                reasons.append(self.state(name))
                continue
            self.observe(name, 'available', 'provider_response', 'engineering_request', stage=stage)
            return result, name
        raise RoutingBlocked(stage, reasons, exclude=exclude)

    def snapshot(self):
        return project_registry(dict(providers=[self.state(name).model_dump() for name in self.registrations]))


def project_registry(value):
    if value is None:
        return None
    states = [ProviderState.model_validate(v).model_dump() for v in value['providers'][:16]]
    # Policy is host-owned, never provider-supplied prose.
    last = max(((s['last_success']['observed_at'], s['provider_id'], s['last_success']['stage'])
                for s in states if s['last_success']), default=None)
    return dict(providers=states,
        last_successful_route=dict(provider=last[1], observed_at=last[0], stage=last[2]) if last else None,
        routing=dict(implementation=['codex', 'claude_code'], review=['openai', 'claude_code', 'codex'],
                     exclude_authors_from_review=True, unknown='bounded_attempt', evidence_ttl_seconds=300))
