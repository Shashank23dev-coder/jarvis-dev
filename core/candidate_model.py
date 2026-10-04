"""Fixed host model worker. No generated Python, tools, or candidate imports."""

import json
import sys
from models.openai_client import request_agent_turn
from .candidate_provider import ProviderFailure, provider_error_code


def main():
    request = json.loads(sys.stdin.read(256001))
    provider = request.get('provider', 'openai')
    if provider not in {'codex', 'openai', 'claude_code'}:
        raise ValueError('Unavailable engineering provider')
    options = {'model': 'gpt-5.3-codex'} if provider == 'codex' else {}
    try:
        if provider == 'claude_code':
            from .candidate_claude import claude_request
            print(json.dumps({'ok': True, 'text': claude_request(request['input'],
                request['instructions'], request.get('timeout', 120))}))
            return
        turn = request_agent_turn(input=[{'role': 'user', 'content': json.dumps(request['input'])}],
            tools=[], instructions=request['instructions'], allow_tools=False,
            timeout=request.get('timeout', 120), max_output_tokens=24000, **options)
        if turn.status != 'completed':
            raise ProviderFailure(provider, 'provider_output_limit' if turn.status == 'incomplete' else 'provider_unavailable')
        if any(item.get('type') == 'function_call' for item in turn.output):
            raise ProviderFailure(provider, 'provider_invalid_response')
        if len(turn.text.encode('utf-8')) > 256000:
            raise ProviderFailure(provider, 'provider_output_limit')
        # The envelope is emitted by trusted adapter code; provider text is
        # always nested, so it cannot impersonate adapter failure metadata.
        print(json.dumps({'ok': True, 'text': turn.text}))
    except Exception as exc:
        print(json.dumps({'ok': False, 'code': exc.code if isinstance(exc, ProviderFailure) else provider_error_code(exc)}))


if __name__ == '__main__':
    main()
