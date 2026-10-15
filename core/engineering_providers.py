"""Small trusted registry: timestamped evidence and bounded role routing."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from typing import Literal

from pydantic import Field, field_validator
from tools.schemas import StrictModel
from .candidate_provider import ProviderFailure, ProposalProvider, ProviderId
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
    excluded_providers: list[ProviderId] = Field(default_factory=list)
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
        from .candidate_claude import ClaudeCodeProvider, ClaudeCLIProvider, claude_executable
        from .candidate_codex_cli import CodexCLIProvider, codex_executable
        # Match the existing SDK's dev .env fallback without projecting values.
        configured = bool(os.getenv('OPENAI_API_KEY') or
            (os.getenv('JARVIS_MODE', 'development') != 'production' and
             dotenv_values(Path(__file__).resolve().parents[1] / '.env').get('OPENAI_API_KEY')))
        return cls(database, [Registration(CodexCLIProvider(transport), 'cli', bool(codex_executable())),
            Registration(ProposalProvider('codex_api', transport), 'api', configured,
                         capabilities=('implementing', 'reviewing')),
            Registration(ClaudeCLIProvider(transport), 'cli', bool(claude_executable()),
                         capabilities=('reviewing', 'implementing')),
            Registration(ProposalProvider('codex', transport), 'api', configured),
            Registration(ClaudeCodeProvider(transport), 'cli', bool(claude_executable())),
            Registration(ProposalProvider('openai', transport), 'api', configured, capabilities=('reviewing',))])

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
        name = 'claude_cli' if 'claude_cli' in self.registrations else 'claude_code'
        r = self.registrations.get(name)
        if r and r.enabled and r.configured:
            current = self.state(name)
            if not force and current.observation:
                age = (utc_time(self.clock()) - utc_time(current.observation.observed_at)).total_seconds()
                if 0 <= age < self.ttl_seconds:
                    return
            state = r.adapter.probe(timeout)
            # A login says nothing about remote quota/service availability.
            # Preserve a recent execution failure/success until it expires.
            if state != 'unknown' or current.current_usability == 'unknown' or current.state == 'not_authenticated':
                self.observe(name, state, 'claude_auth_status', 'local_authentication',
                    'provider_authentication' if state == 'not_authenticated' else
                    'provider_unavailable' if state == 'unavailable' else None)

    def complete(self, payload, instructions, timeout, *, stage, task_type='code', exclude=(), validate=lambda x: x, preferred=None, allowed=None, api_fallback_authorized=False):
        import time
        deadline = time.monotonic() + timeout
        modern = 'codex_cli' in self.registrations
        if modern:
            preferred = {'codex': 'codex_cli', 'openai': 'claude_cli',
                         'claude_code': 'claude_cli'}.get(preferred, preferred)
        order = (('codex_cli', 'claude_cli', 'codex_api') if stage == 'implementing' else
                 ('claude_cli', 'codex_cli')) if modern else (
                 ('codex', 'claude_code') if stage == 'implementing' else
                 ('openai', 'claude_code', 'codex'))
        order = list(dict.fromkeys(([preferred] if preferred in self.registrations else []) +
            list(order) + list(self.registrations)))
        requested = preferred or order[0]
        requested_type = self.registrations[requested].provider_type if requested in self.registrations else None
        self.last_route = dict(provider_requested=requested, provider_used=None,
            adapter_type=requested_type,
            billing_class='subscription_cli' if requested_type == 'cli' else
                          'api_billed' if requested_type == 'api' else None,
            fallback_reason=None)
        reasons = []
        for name in order:
            if allowed is not None and name not in allowed:
                if name == requested:
                    self.last_route['fallback_reason'] = 'route_not_allowed'
                continue
            if name in exclude:
                if name == requested:
                    self.last_route['fallback_reason'] = (
                        'reviewer_independence' if stage == 'reviewing' else 'provider_excluded')
                continue
            if name not in self.registrations:
                continue
            r = self.registrations[name]
            if stage not in r.capabilities or task_type not in r.task_types:
                if name == requested:
                    self.last_route['fallback_reason'] = 'capability_mismatch'
                continue
            if name == 'codex_api' and name != preferred:
                # A CLI failure cannot create a new API charge. Unknown cost is
                # rejected by the existing financial policy until authorized.
                from .engineering_authority import assess_action
                if not api_fallback_authorized or not assess_action('spend', amount=None).allowed:
                    self.last_route['fallback_reason'] = 'financial_authorization_required'
                    continue
            if modern and name in {'codex', 'openai', 'claude_code'}:
                if name == requested:
                    self.last_route['fallback_reason'] = 'route_not_allowed'
                continue
            state = self.state(name)
            if state.current_usability not in {'available', 'unknown'}:
                reasons.append(state)
                if modern and name == requested and state.current_usability in {'quota_blocked', 'unavailable', 'not_authenticated'}:
                    break
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
                if modern and name == requested and exc.code in {'provider_quota', 'provider_rate_limit',
                                                                  'provider_unavailable', 'provider_authentication'}:
                    break
                continue
            except Exception:
                # Never reflect arbitrary adapter exception text.
                self.observe(name, 'invocation_failed', 'provider_failure',
                             'engineering_request', 'provider_invocation_failed', stage=stage)
                reasons.append(self.state(name))
                continue
            self.observe(name, 'available', 'provider_response', 'engineering_request', stage=stage)
            self.last_route.update(provider_used=name, adapter_type=r.provider_type,
                billing_class='subscription_cli' if r.provider_type == 'cli' else 'api_billed',
                fallback_reason=None if name == self.last_route['provider_requested'] else
                (self.last_route['fallback_reason'] or
                 (reasons[0].observation.code if reasons and reasons[0].observation else None)))
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
        routing=dict(implementation=['codex_cli', 'claude_cli', 'codex_api'],
                     review=['claude_cli', 'codex_cli'],
                     api_fallback_requires_authorization=True,
                     exclude_authors_from_review=True, unknown='bounded_attempt', evidence_ttl_seconds=300))
