"""Registry and CLI checks never require a real authenticated provider."""
import json
from types import SimpleNamespace as NS

import pytest
from core.database import Database
from core.candidate_provider import ProviderFailure
from core.engineering_providers import EngineeringRegistry, Registration, RoutingBlocked
from core import candidate_claude as claude


def adapter(name, failure=None):
    def complete(*args):
        if failure:
            raise ProviderFailure(name, failure)
        return {'edits': []}
    return NS(name=name, complete=complete)


def registry(tmp_path, codex=None, claude_provider=None, **kwargs):
    return EngineeringRegistry(Database(tmp_path / 'providers.db'), [
        Registration(codex or adapter('codex'), 'api', True),
        Registration(claude_provider or adapter('claude_code'), 'cli', True)], **kwargs)


def test_registration_configuration_is_not_availability_and_duplicate_denied(tmp_path):
    r = registry(tmp_path)
    assert r.state('codex').current_usability == 'unknown'
    assert r.state('claude_code').capabilities == ['implementing', 'reviewing']
    with pytest.raises(ValueError):
        EngineeringRegistry(r.database, [Registration(adapter('codex'), 'api', True)] * 2)
    # Future trusted adapters need no new registry subclass or state table.
    future = EngineeringRegistry(r.database, [Registration(adapter('future_agent'), 'api', False)])
    assert future.state('future_agent').current_usability == 'unavailable'


@pytest.mark.parametrize('code', ['provider_unavailable', 'provider_quota', 'provider_authentication'])
def test_codex_failure_falls_back_and_observations_survive_restart(tmp_path, code):
    r = registry(tmp_path, codex=adapter('codex', code))
    result, name = r.complete({}, '', 1, stage='implementing')
    assert name == 'claude_code' and result == {'edits': []}
    assert r.state('codex').last_failure.code == code
    r.database.close()
    assert registry(tmp_path).snapshot()['last_successful_route']['provider'] == 'claude_code'
    assert registry(tmp_path).state('claude_code').current_usability == 'available'


def test_both_unavailable_structured_blocker_no_exception_prose(tmp_path):
    r = registry(tmp_path, codex=adapter('codex', 'provider_quota'),
                 claude_provider=adapter('claude_code', 'provider_authentication'))
    with pytest.raises(RoutingBlocked) as error:
        r.complete({}, '', 1, stage='implementing')
    blocker = error.value.blocker
    assert blocker['code'] == 'engineering_providers_blocked'
    assert [v['state'] for v in blocker['reasons']] == ['quota_blocked', 'not_authenticated']
    assert r.snapshot()['last_successful_route'] is None


def test_observations_expire_and_review_excludes_author(tmp_path):
    now = ['2026-10-04T00:00:00']
    r = registry(tmp_path, clock=lambda: now[0])
    r.observe('codex', 'quota_blocked', 'provider_failure', 'engineering_request', 'provider_quota')
    now[0] = '2026-10-04T00:06:00'
    assert r.state('codex').state == 'quota_blocked'
    assert r.state('codex').current_usability == 'unknown'
    _, name = r.complete({}, '', 1, stage='reviewing', exclude=['codex'])
    assert name == 'claude_code'


@pytest.mark.parametrize('installed,status,expected', [
    (False, None, 'unavailable'), (True, {'loggedIn': False, 'email': 'HIDDEN'}, 'not_authenticated'),
    (True, {'loggedIn': True, 'accessToken': 'HIDDEN'}, 'unknown'), (True, {}, 'unknown')])
def test_claude_authentication_detection(monkeypatch, installed, status, expected):
    monkeypatch.setattr(claude, 'claude_executable', lambda: '/user/claude' if installed else None)
    monkeypatch.setattr(claude, 'run_claude', lambda *a, **kw: (0, json.dumps(status).encode()))
    assert claude.probe_claude() == expected
    if expected in {'unavailable', 'not_authenticated'}:
        with pytest.raises(ProviderFailure) as error:
            claude.claude_request({}, '', 1)
        assert error.value.code == ('provider_authentication' if installed else 'provider_unavailable')
        assert 'HIDDEN' not in str(error.value)


def test_claude_noninteractive_tool_disabled_request(monkeypatch):
    monkeypatch.setattr(claude, 'probe_claude', lambda _: 'unknown')
    seen = []
    def run(arguments, **kwargs):
        seen.append((arguments, kwargs))
        return 0, json.dumps(dict(type='result', is_error=False, result='{"edits":[]}')).encode()
    monkeypatch.setattr(claude, 'run_claude', run)
    assert json.loads(claude.claude_request({'goal': 'fix'}, 'Return JSON', 2)) == {'edits': []}
    args, options = seen[0]
    assert '-p' in args and '--no-session-persistence' in args
    assert args[args.index('--tools')+1] == ''
    assert '--strict-mcp-config' in args and '--disable-slash-commands' in args
    assert args[args.index('--settings')+1] == '{"disableAllHooks":true}'
    assert args[args.index('--permission-mode')+1] == 'default'
    assert not any('skip-permissions' in value for value in args)
    assert json.loads(options['prompt']) == {'goal': 'fix'}


@pytest.mark.parametrize('message,expected', [('Not logged in HIDDEN', 'provider_authentication'),
    ('You have hit your limit HIDDEN', 'provider_quota'), ('Internal failure HIDDEN', 'provider_invocation_failed')])
def test_claude_failure_does_not_leak_secrets(monkeypatch, message, expected):
    monkeypatch.setattr(claude, 'probe_claude', lambda _: 'unknown')
    monkeypatch.setattr(claude, 'run_claude', lambda *a, **kw:
        (1, json.dumps(dict(type='result', is_error=True, result=message)).encode()))
    with pytest.raises(ProviderFailure) as error:
        claude.claude_request({}, '', 2)
    assert error.value.code == expected and 'HIDDEN' not in str(error.value)


def test_auth_probe_records_no_account_information(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, 'claude_executable', lambda: '/user/claude')
    monkeypatch.setattr(claude, 'run_claude', lambda *a, **kw:
        (1, b'{"loggedIn":false,"email":"HIDDEN","accessToken":"HIDDEN"}'))
    r = registry(tmp_path, claude_provider=claude.ClaudeCodeProvider(lambda *a, **kw: None))
    r.refresh_claude()
    assert r.state('claude_code').state == 'not_authenticated'
    assert 'HIDDEN' not in json.dumps(r.snapshot())
    with r.database.transaction() as db:
        assert 'HIDDEN' not in db.execute('SELECT record FROM engineering_provider_state').fetchone()[0]
