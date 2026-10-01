"""Trusted engineering adapters. Providers return data, never execute edits.

Keep this module under the candidate protected-path boundary. A future Claude
Code adapter must implement this contract without acquiring filesystem authority.
"""
from dataclasses import dataclass
from typing import Protocol
from typing import Literal
from pydantic import Field
from tools.schemas import StrictModel

PROVIDERS = {'codex', 'openai', 'claude_code'}
FAILURES = {
    'provider_authentication': (False, 'Configure the development provider credential.'),
    'provider_quota': (False, 'Restore provider quota, then resume the job.'),
    'provider_rate_limit': (True, 'Wait for the rate limit to clear, then resume.'),
    'provider_unavailable': (True, 'Check provider availability, then resume.'),
    'provider_timeout': (True, 'Resume with a fresh bounded provider budget.'),
    'provider_invalid_response': (True, 'Retry the structured proposal or review.'),
    'provider_no_changes': (True, 'The proposal made no changes; request a concrete implementation.'),
    'provider_output_limit': (True, 'Reduce response size or increase the bounded output budget.'),
    'provider_request_rejected': (False, 'Check the configured model and request parameters.'),
    'source_not_clean': (True, 'Commit development changes before resuming.'),
    'credential_content_blocked': (False, 'Remove credential-like content from the proposed edits.'),
    'sandbox_unavailable': (True, 'Restore the configured Docker test sandbox.'),
    'candidate_deadline': (True, 'Resume with a fresh candidate budget.'),
    'candidate_policy_denied': (False, 'Keep edits within approved development paths and limits.'),
    'tests_failed': (True, 'Revise the candidate using test evidence.'),
    'review_failed': (True, 'Revise the candidate using independent review evidence.'),
    'worker_failed': (True, 'Inspect the development worker and resume.'),
}


class FailureEvidence(StrictModel):
    provider: Literal['codex', 'openai', 'claude_code']
    code: str
    retryable: bool
    stage: Literal['planning', 'implementing', 'testing', 'reviewing']
    attempt: int = Field(ge=1)
    reason: str


def failure_evidence(provider, code, stage, attempt):
    if code not in FAILURES:
        code = 'worker_failed'
    return FailureEvidence(provider=provider, code=code, retryable=FAILURES[code][0],
                           stage=stage, attempt=attempt, reason=FAILURES[code][1]).model_dump()


def project_failure(value):
    if value is None:
        return None
    fields = {k: value[k] for k in FailureEvidence.model_fields if k != 'reason'}
    # Backward-compatible projection; never reflect arbitrary exception prose.
    fields['reason'] = FAILURES.get(fields['code'], FAILURES['worker_failed'])[1]
    result = FailureEvidence.model_validate(fields).model_dump()
    if result['code'] not in FAILURES or result['retryable'] != FAILURES[result['code']][0]:
        raise ValueError('Invalid failure evidence')
    return result | {'action': FAILURES[result['code']][1]}


class ProviderFailure(RuntimeError):
    def __init__(self, provider, code):
        self.provider = provider
        self.code = code if code in FAILURES else 'provider_unavailable'
        super().__init__(self.code)


class ProviderResult(StrictModel):
    provider: Literal['codex', 'openai', 'claude_code']
    stage: Literal['implementing', 'reviewing']
    attempt: int = Field(ge=1)
    iteration: int = Field(ge=1, le=4)
    status: Literal['received', 'no_changes', 'applied']
    response_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def project_provider_results(values):
    return [ProviderResult.model_validate({k: item[k] for k in ProviderResult.model_fields}).model_dump()
            for item in values[-4:]]


def provider_error_code(exc):
    """Only SDK types/status/closed codes; never persist exception prose/body."""
    code = getattr(exc, 'code', None)
    status = getattr(exc, 'status_code', None)
    if code == 'insufficient_quota':
        return 'provider_quota'
    if status in (401, 403):
        return 'provider_authentication'
    if status == 429:
        return 'provider_rate_limit'
    if status in (400, 404, 422):
        return 'provider_request_rejected'
    if isinstance(exc, TimeoutError) or type(exc).__name__ == 'APITimeoutError':
        return 'provider_timeout'
    return 'provider_unavailable'


def execution_error_code(exc):
    from tools.policy import ToolDenied
    if isinstance(exc, ToolDenied):
        value = str(exc)
        if value == 'Proposal made no changes':
            return 'provider_no_changes'
        if value == 'Source must be clean before starting a candidate':
            return 'source_not_clean'
        if value.startswith('Credential-like'):
            return 'credential_content_blocked'
        if 'sandbox' in value.lower() or 'Docker' in value:
            return 'sandbox_unavailable'
        if 'deadline' in value:
            return 'candidate_deadline'
        return 'candidate_policy_denied'
    return 'worker_failed'


class EngineeringProvider(Protocol):
    name: str

    def complete(self, payload, instructions, timeout): ...


@dataclass(frozen=True)
class ProposalProvider:
    name: str
    transport: object

    def complete(self, payload, instructions, timeout):
        return self.transport(payload, instructions, timeout, provider=self.name)


class EngineeringProviders:
    """Separate trusted author/reviewer instances; no model-selected routing."""
    def __init__(self, implementer, reviewer):
        if implementer is reviewer or implementer.name == reviewer.name:
            raise ValueError('Engineering review requires a separate provider')
        for provider in (implementer, reviewer):
            if not callable(getattr(provider, 'complete', None)):
                raise ValueError('Invalid engineering provider')
        self.implementer, self.reviewer = implementer, reviewer

    @classmethod
    def defaults(cls, transport):
        return cls(ProposalProvider('codex', transport), ProposalProvider('openai', transport))

    def select(self, name):
        if name != self.implementer.name:
            raise ValueError('Engineering provider unavailable')
        return self.implementer
