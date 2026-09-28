import os
from functools import lru_cache
from dotenv import load_dotenv
from openai import OpenAI
from .provider import ProviderTurn as AgentTurn

MODEL = "gpt-5.6"


@lru_cache(maxsize=1)
def get_client():
    """Create the client only when needed, so imports/tests need no credentials."""
    if os.getenv("JARVIS_MODE", "development") != "production":
        load_dotenv()
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def request_agent_turn(*, input: list[dict], tools: list[dict],
                       instructions: str, allow_tools: bool, model: str = MODEL) -> AgentTurn:
    """One bounded Responses request; execution is always handled locally."""
    response = get_client().with_options(timeout=30.0, max_retries=0).responses.create(
        model=model, input=input, instructions=instructions, tools=tools,
        tool_choice="auto" if allow_tools else "none", parallel_tool_calls=False,
        max_output_tokens=4096, store=False, include=["reasoning.encrypted_content"],
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
