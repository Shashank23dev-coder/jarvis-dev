"""Verify the Responses wire contract without contacting OpenAI."""

from types import SimpleNamespace
from unittest.mock import Mock
import os
import subprocess
import sys

import pytest

from models import openai_client
from tools import ToolRegistry


@pytest.mark.parametrize("allow_tools,choice", [(True, "auto"), (False, "none")])
def test_agent_transport_preserves_model_and_bounds_requests(monkeypatch, allow_tools, choice):
    output_item = Mock()
    output_item.model_dump.return_value = {"type": "reasoning", "summary": [], "encrypted_content": "opaque"}
    response = SimpleNamespace(output=[output_item], output_text="Final answer", status="completed")
    client = Mock()
    client.with_options.return_value.responses.create.return_value = response
    monkeypatch.setattr(openai_client, "get_client", lambda: client)
    history = [{"role": "user", "content": "Inspect"}]
    definitions = ToolRegistry().model_tools()
    result = openai_client.request_agent_turn(input=history, tools=definitions,
                                             instructions="Read-only", allow_tools=allow_tools)
    client.with_options.assert_called_once_with(timeout=30.0, max_retries=0)
    kwargs = client.with_options.return_value.responses.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5.6" and kwargs["input"] == history
    assert kwargs["tools"] == definitions and kwargs["tool_choice"] == choice
    assert kwargs["parallel_tool_calls"] is False and kwargs["store"] is False
    assert kwargs["max_output_tokens"] == 4096
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert result.text == "Final answer" and result.output[0]["encrypted_content"] == "opaque"


def test_refusal_is_a_final_response(monkeypatch):
    item = Mock()
    item.model_dump.return_value = {"type": "message", "content": [{"type": "refusal", "refusal": "Cannot do that."}]}
    client = Mock()
    client.with_options.return_value.responses.create.return_value = SimpleNamespace(
        output=[item], output_text="", status="completed")
    monkeypatch.setattr(openai_client, "get_client", lambda: client)
    result = openai_client.request_agent_turn(input=[], tools=[], instructions="", allow_tools=False)
    assert result.text == "Cannot do that."


def test_legacy_planning_transport_unchanged(monkeypatch):
    client = Mock()
    client.responses.create.return_value.output_text = '{"steps": []}'
    monkeypatch.setattr(openai_client, "get_client", lambda: client)
    assert openai_client.ask_model("Plan this") == '{"steps": []}'
    client.responses.create.assert_called_once_with(model="gpt-5.6", input="Plan this")


def test_import_does_not_load_credentials_or_construct_client():
    # Patching openai.OpenAI imports the entire SDK before importing main, and
    # can itself exhaust the sandbox deadline. Reject those imports instead.
    script = '''import builtins, socket, subprocess, threading
from unittest.mock import patch
original_import = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split('.')[0] in {'openai', 'dotenv'}:
        raise AssertionError('Provider or credential module loaded at import')
    return original_import(name, *args, **kwargs)
with patch('builtins.__import__', side_effect=checked_import), \\
     patch.object(threading.Thread, 'start', side_effect=AssertionError('Worker started at import')), \\
     patch.object(subprocess, 'Popen', side_effect=AssertionError('Process started at import')), \\
     patch.object(socket.socket, 'connect', side_effect=AssertionError('Network access at import')), \\
     patch.object(socket, 'create_connection', side_effect=AssertionError('Network access at import')):
    import main
    assert main.task_store.database._connection is None
print('imported')
'''
    environment = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"
