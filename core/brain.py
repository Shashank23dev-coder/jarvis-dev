"""Jarvis decision layer; the existing runner retains all execution authority."""
from copy import deepcopy
from dataclasses import dataclass
import os

from models.openai_provider import OpenAIProvider
from models.provider import ProviderRegistry, ProviderRequest, ProviderTurn
from .task_context import TaskContext, assemble_context


@dataclass(frozen=True)
class BrainSettings:
    enabled: bool = False
    provider: str = 'openai'

    @classmethod
    def from_env(cls):
        # Only an explicit 1 opts in. Neither user text nor tools can enable it.
        return cls(os.environ.get('JARVIS_BRAIN_ENABLED', '0') == '1',
                   os.environ.get('JARVIS_BRAIN_PROVIDER', 'openai'))


class JarvisBrain:
    def __init__(self, runner, *, settings=None, providers=None):
        self.runner = runner
        self.settings = settings or BrainSettings.from_env()
        self.providers = providers if providers is not None else ProviderRegistry([OpenAIProvider()])

    def route(self, context: TaskContext):
        # Phase 1 has one configured route, pinned for the task. Context and model
        # content cannot select providers, executable code, endpoints or authority.
        return self.providers.get(self.settings.provider)

    def assemble_context(self, context, store, **limits):
        return assemble_context(context, store, **limits)

    def run(self, goal, *, task_id, task_store):
        task = task_store.get_task(task_id)
        if task is None:
            raise ValueError('Brain execution requires an existing task')
        context = TaskContext.from_task(goal, task)
        provider = self.route(context) if self.settings.enabled else None
        task_store.add_brain_runtime(task_id, enabled=self.settings.enabled,
                                    selected_provider=(provider.name if provider else None)
                                    if self.settings.enabled else 'legacy')
        session = _ProviderSession(provider, self.runner.model, task_store, task_id, enabled=self.settings.enabled)
        if not self.settings.enabled:
            return self.runner.run(goal, task_id=task_id, task_store=task_store, model=session.turn)

        return self.runner.run(goal, task_id=task_id, task_store=task_store,
                               model=session.turn, context_builder=self.assemble_context, engineering=True)


class _ProviderSession:
    """Task-local routing/fallback state. Never restart or replay the agent loop."""
    def __init__(self, provider, legacy, store, task_id, *, enabled=True):
        self.provider, self.legacy = provider, legacy
        self.enabled = enabled
        self.store, self.task_id = store, task_id
        self.iteration = 0
        self.fallback_reason = ('brain_disabled' if not enabled else
                                'provider_unavailable' if provider is None else None)
        self.foreign_prefix_length = None

    def record(self, provider, reason, status):
        self.store.add_routing_activity(self.task_id, provider=provider, reason=reason,
                                        iteration=self.iteration, status=status)

    def turn(self, **kwargs):
        self.iteration += 1
        if self.fallback_reason is None:
            name = self.provider.name
            self.record(name, 'configured_provider', 'started')
            try:
                # A failed adapter cannot corrupt the transcript or tool schemas
                # subsequently passed to the legacy transport.
                result = self.provider.complete(ProviderRequest(**deepcopy(kwargs)))
                if not isinstance(result, ProviderTurn):
                    raise ValueError('Invalid provider result')
            except Exception:
                # Exception messages may contain credentials; persist only a code.
                self.record(name, 'provider_error', 'failed')
                self.fallback_reason = 'provider_error'
            else:
                if result.status == 'completed':
                    self.record(name, 'configured_provider', 'completed')
                    return result
                self.record(name, 'provider_incomplete', 'incomplete')
                self.fallback_reason = 'provider_incomplete'
        reason = self.fallback_reason
        self.record('legacy', reason, 'started')
        # Opaque continuation data from a different provider must not cross the
        # provider boundary. Canonical tool calls/results remain for continuity.
        legacy_kwargs = deepcopy(kwargs)
        if self.provider is not None and self.provider.name != 'openai':
            if self.foreign_prefix_length is None:
                self.foreign_prefix_length = len(legacy_kwargs['input'])
            legacy_kwargs['input'] = [item for index, item in enumerate(legacy_kwargs['input'])
                                      if index >= self.foreign_prefix_length or item.get('type') != 'reasoning']
        try:
            result = self.legacy(**legacy_kwargs)
        except Exception:
            self.record('legacy', reason, 'failed')
            raise
        self.record('legacy', reason, 'completed' if result.status == 'completed' else 'incomplete')
        self.fallback_reason = 'legacy_continuation' if self.enabled else 'brain_disabled'
        return result
