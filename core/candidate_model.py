"""Fixed host model worker. No generated Python, tools, or candidate imports."""

import json
import sys
from models.openai_client import request_agent_turn


def main():
    request = json.loads(sys.stdin.read(256001))
    provider = request.get('provider', 'openai')
    if provider not in {'codex', 'openai'}:
        raise ValueError('Unavailable engineering provider')
    options = {'model': 'gpt-5.3-codex'} if provider == 'codex' else {}
    turn = request_agent_turn(input=[{'role': 'user', 'content': json.dumps(request['input'])}],
                              tools=[], instructions=request['instructions'], allow_tools=False, **options)
    if turn.status != 'completed' or any(item.get('type') == 'function_call' for item in turn.output):
        raise ValueError('Invalid model response')
    if len(turn.text.encode('utf-8')) > 256000:
        raise ValueError('Model output limit')
    print(turn.text)


if __name__ == '__main__':
    main()
