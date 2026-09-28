"""Jarvis-owned turn contract; adapters translate it to their provider's API.

The transcript uses role/content messages, function_call and function_call_output
items. Tool definitions use the existing registry schema. Only the host runner
executes calls. Opaque reasoning items are transient continuation data, never audit
data; adapters must not expose private reasoning as answer text.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol


PROVIDER_NAMES = frozenset({'openai', 'claude', 'codex', 'gemini', 'local'})


@dataclass(frozen=True)
class ProviderRequest:
    input: list[dict]
    tools: list[dict]
    instructions: str
    allow_tools: bool


@dataclass(frozen=True)
class ProviderTurn:
    output: list[dict]
    text: str
    status: str = 'completed'


class Provider(Protocol):
    name: str

    def complete(self, request: ProviderRequest) -> ProviderTurn:
        """Return one bounded turn; never execute tools or persist transcripts."""
        ...


class ProviderRegistry:
    """Explicit trusted instances only: no plugin imports or request-selected URLs."""
    def __init__(self, providers):
        registered = {}
        for provider in providers:
            if (provider.name not in PROVIDER_NAMES or provider.name in registered
                    or not callable(getattr(provider, 'complete', None))):
                raise ValueError('Invalid provider registration')
            registered[provider.name] = provider
        self.providers = MappingProxyType(registered)

    def get(self, name):
        return self.providers.get(name)
