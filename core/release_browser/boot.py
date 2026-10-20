"""Throwaway app entrypoint for the release browser check (never production).

Runs the release image's own server in production mode. Only the OpenAI client
is replaced by a local stub; the container also has no network at all.
The owner login hash is made by the image's own owner_login hashing from
throwaway credentials that exist only for this run.
"""
import json
import os
import re
import sys
import time


STUB_MARK = 'jarvis-browser-check-stub'
HELLO_REPLY = 'Hello! Jarvis browser check reply: I am here and ready to help.'


def seed_reply(number):
    return (f'## Seed section {number}\n\n'
            f'Here is **bold point {number}** with some `inline code`.\n\n'
            '- first bullet item\n- second bullet item\n- third bullet item\n\n'
            '1. numbered one\n2. numbered two\n\n'
            '```python\n'
            f'def seed_{number}():\n    return {number}\n'
            '```\n\n'
            f'### Notes {number}\n\nA closing line so the history is long enough to scroll.')


def choose_reply(arguments):
    text = json.dumps(arguments.get('input', ''))
    seeds = [(m.start(), int(m.group(1))) for m in re.finditer(r'BCSEED-(\d+)', text)]
    hello = text.lower().rfind('hello')
    if seeds and seeds[-1][0] > hello:
        return seed_reply(seeds[-1][1]), 0.0
    return HELLO_REPLY, 1.5  # Long enough for the page to show Thinking...


class _Item:
    def __init__(self, text):
        self.text = text

    def model_dump(self, **kwargs):
        return {'type': 'message', 'id': 'msg_' + STUB_MARK, 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': self.text, 'annotations': []}]}


class _Response:
    def __init__(self, text):
        self.output_text, self.status, self.output = text, 'completed', [_Item(text)]


class _Responses:
    def create(self, **arguments):
        text, delay = choose_reply(arguments)
        time.sleep(delay)
        return _Response(text)


class StubClient:
    responses = _Responses()

    def with_options(self, **kwargs):
        return self


def install():
    import models.openai_client as client
    client.get_client = lambda: StubClient()


def configure():
    from core import owner_login
    username = os.environ.pop('BCHECK_USERNAME')
    password = os.environ.pop('BCHECK_PASSWORD')
    os.environ[owner_login.ENV_KEY] = owner_login.make_hash(username, password)
    os.environ['OPENAI_BASE_URL'] = 'http://127.0.0.1:9/'  # Discard port: never a real endpoint.


if __name__ == '__main__':
    sys.path.insert(0, '/app')
    os.chdir('/app')
    configure()
    install()
    from core.deployment_server import main
    main()
