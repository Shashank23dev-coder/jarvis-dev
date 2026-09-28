"""Trusted engineering adapters. Providers return data, never execute edits.

Keep this module under the candidate protected-path boundary. A future Claude
Code adapter must implement this contract without acquiring filesystem authority.
"""
from dataclasses import dataclass
from typing import Protocol


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
