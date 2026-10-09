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


class ProviderPolicyRefusal(Exception):
    """A provider declined the request; no provider text is retained."""


def is_policy_rejection(exc):
    """Recognize structured 400 policy errors without inspecting exception text."""
    if getattr(exc, 'status_code', None) != 400:
        return False
    body = getattr(exc, 'body', None)
    error = body.get('error', body) if isinstance(body, dict) else None
    codes = [getattr(exc, 'code', None)]
    if isinstance(error, dict):
        codes.extend((error.get('code'), error.get('type')))
    return any(isinstance(code, str) and any(part in code.lower()
               for part in ('policy', 'safety', 'content_filter', 'moderation')) for code in codes)


def refusal_only(turn):
    """A completed turn containing only refusal content and optional reasoning."""
    if not isinstance(turn, ProviderTurn) or turn.status != 'completed' or not isinstance(turn.output, list):
        return False
    messages = [item for item in turn.output if isinstance(item, dict) and item.get('type') == 'message']
    return bool(messages) and all(
        isinstance(item, dict) and (item.get('type') == 'reasoning' or
        item.get('type') == 'message' and isinstance(item.get('content'), list) and
        bool(item['content']) and all(isinstance(part, dict) and part.get('type') == 'refusal'
                                     for part in item['content']))
        for item in turn.output)


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
