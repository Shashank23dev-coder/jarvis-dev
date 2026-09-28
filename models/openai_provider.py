"""OpenAI adapter reusing the established bounded Responses transport."""
from . import openai_client
from .provider import ProviderRequest, ProviderTurn


class OpenAIProvider:
    name = 'openai'

    def complete(self, request: ProviderRequest) -> ProviderTurn:
        return openai_client.request_agent_turn(
            input=request.input, tools=request.tools,
            instructions=request.instructions, allow_tools=request.allow_tools)
