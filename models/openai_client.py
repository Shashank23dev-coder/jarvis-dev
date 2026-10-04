import os
from functools import lru_cache
from .provider import ProviderTurn as AgentTurn

MODEL = "gpt-5.6"


def strict_schema(value):
    """Normalize wire schemas without altering host defaults or validation."""
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: strict_schema(item) for key, item in value.items()}
    if result.get('type') == 'object' and result.get('properties'):
        result.update(required=list(result.get('properties', {})), additionalProperties=False)
    return result


@lru_cache(maxsize=1)
def get_client():
    """Create the client only when needed, so imports/tests need no credentials."""
    if os.getenv("JARVIS_MODE", "development") != "production":
        from dotenv import load_dotenv
        load_dotenv()
    from openai import OpenAI
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def request_agent_turn(*, input: list[dict], tools: list[dict],
                       instructions: str, allow_tools: bool, model: str = MODEL,
                       timeout: float = 30.0, max_output_tokens: int = 4096) -> AgentTurn:
    """One bounded Responses request; execution is always handled locally."""
    response = get_client().with_options(timeout=timeout, max_retries=0).responses.create(
        model=model, input=input, instructions=instructions,
        tools=[dict(tool, parameters=strict_schema(tool['parameters'])) for tool in tools],
        tool_choice="auto" if allow_tools else "none", parallel_tool_calls=False,
        max_output_tokens=max_output_tokens, store=False, include=["reasoning.encrypted_content"],
    )
    output = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
    text = response.output_text
    if not text:
        text = "\n".join(part["refusal"] for item in output if item.get("type") == "message"
                         for part in item.get("content", []) if part.get("type") == "refusal")
    return AgentTurn(output=output, text=text, status=response.status)


def ask_model(prompt: str) -> str:
    response = get_client().responses.create(
        model=MODEL,
        input=prompt,
    )

    return response.output_text
