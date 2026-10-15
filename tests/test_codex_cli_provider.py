"""Codex subscription routing and data-only CLI invocation."""
import io
import json
from types import SimpleNamespace

import pytest

from core import candidate_codex_cli as cli
from core.candidate_provider import ProviderFailure
from core.database import Database
from core.engineering_providers import (EngineeringRegistry, Registration, RoutingBlocked,
                                        _successful_fallback_reason)


def fake(name, calls, failure=None):
    def complete(*args):
        calls.append(name)
        if failure:
            raise ProviderFailure(name, failure)
        return {'edits': [{'path': 'src/app.py', 'content': 'value = 2\n'}]}
    return SimpleNamespace(name=name, complete=complete)


def registry(tmp_path, calls, cli_failure=None, reviewer_failure=None):
    return EngineeringRegistry(Database(tmp_path / 'routes.db'), [
        Registration(fake('codex_cli', calls, cli_failure), 'cli', True,
                     capabilities=('implementing',)),
        Registration(fake('claude_cli', calls, reviewer_failure), 'cli', True,
                     capabilities=('reviewing',)),
        Registration(fake('codex_api', calls), 'api', True,
                     capabilities=('implementing', 'reviewing'))])


def test_cli_default_and_api_never_called_when_healthy(tmp_path):
    calls = []
    routes = registry(tmp_path, calls)
    _, used = routes.complete({}, '', 2, stage='implementing')
    assert used == 'codex_cli' and calls == ['codex_cli']
    assert routes.last_route == dict(provider_requested='codex_cli', provider_used='codex_cli',
        adapter_type='cli', billing_class='subscription_cli', fallback_reason=None)
    assert routes.complete({}, '', 2, stage='reviewing', exclude=('codex_cli',))[1] == 'claude_cli'
    assert calls == ['codex_cli', 'claude_cli']


@pytest.mark.parametrize('stage,preferred,allowed,exclude,capabilities,expected', [
    ('reviewing', 'codex_cli', None, ('codex_cli',), ('implementing', 'reviewing'),
     'reviewer_independence'),
    ('implementing', 'codex_cli', None, ('codex_cli',), ('implementing', 'reviewing'),
     'provider_excluded'),
    ('reviewing', 'codex_cli', None, (), ('implementing',), 'capability_mismatch'),
    ('implementing', 'codex_cli', ('claude_cli',), (), ('implementing', 'reviewing'),
     'route_not_allowed'),
])
def test_intentional_routing_skip_has_accurate_attribution(
        tmp_path, stage, preferred, allowed, exclude, capabilities, expected):
    from core.engineering_jobs import project_provider_route
    calls = []
    routes = EngineeringRegistry(Database(tmp_path / 'routes.db'), [
        Registration(fake('codex_cli', calls), 'cli', True, capabilities=capabilities),
        Registration(fake('claude_cli', calls), 'cli', True)])
    _, used = routes.complete({}, '', 2, stage=stage, preferred=preferred,
                              allowed=allowed, exclude=exclude)
    assert used == 'claude_cli' and calls == ['claude_cli']
    assert routes.last_route['fallback_reason'] == expected
    assert project_provider_route(routes.last_route, preferred)['fallback_state'] == 'used'
    assert routes.state(preferred).last_failure is None
    assert routes.state(preferred).current_usability == 'unknown'


@pytest.mark.parametrize('allowed,capabilities,expected', [
    (('claude_cli', 'later_cli'), ('implementing',), 'route_not_allowed'),
    (None, ('reviewing',), 'capability_mismatch'),
])
def test_requested_skip_survives_later_failure_and_success(
        tmp_path, allowed, capabilities, expected):
    calls = []
    routes = EngineeringRegistry(Database(tmp_path / 'routes.db'), [
        Registration(fake('codex_cli', calls), 'cli', True, capabilities=capabilities),
        Registration(fake('claude_cli', calls, 'provider_invalid_response'), 'cli', True),
        Registration(fake('later_cli', calls), 'cli', True)])
    _, used = routes.complete({}, '', 2, stage='implementing', preferred='codex_cli',
                              allowed=allowed)
    assert used == 'later_cli'
    assert calls == ['claude_cli', 'later_cli']
    assert routes.last_route['fallback_reason'] == expected
    assert routes.last_route['fallback_reason'] is not None
    assert routes.state('codex_cli').last_failure is None


def test_api_fallback_blocked_without_financial_authorization(tmp_path):
    calls = []
    routes = registry(tmp_path, calls, 'provider_invalid_response', 'provider_unavailable')
    with pytest.raises(RoutingBlocked):
        routes.complete({}, '', 2, stage='implementing', api_fallback_authorized=True)
    assert calls == ['codex_cli']
    assert routes.last_route['provider_used'] is None
    assert routes.last_route['fallback_reason'] == 'provider_invalid_response'
    # Explicit selection remains distinguishable from a surprise fallback.
    assert routes.complete({}, '', 2, stage='implementing', preferred='codex_api')[1] == 'codex_api'
    assert calls[-1] == 'codex_api'
    assert routes.last_route['billing_class'] == 'api_billed'


def test_requested_quota_reason_precedes_later_api_financial_skip():
    # Production routing stops on CLI quota. Exercise the attribution decision
    # for the stated later-provider sequence without changing that routing rule.
    assert _successful_fallback_reason('provider_quota', []) == 'provider_quota'
    assert _successful_fallback_reason('provider_quota', [SimpleNamespace(
        observation=SimpleNamespace(code='financial_authorization_required'))]) == 'provider_quota'


def test_financial_skip_attribution_ignores_later_provider_failure():
    later_failure = [SimpleNamespace(observation=SimpleNamespace(code='provider_quota'))]
    assert _successful_fallback_reason('financial_authorization_required', later_failure) == (
        'financial_authorization_required')


def test_successful_fallback_without_specific_requested_reason_uses_last_resort():
    later_failure = [SimpleNamespace(observation=SimpleNamespace(code='provider_quota'))]
    assert _successful_fallback_reason(None, later_failure) == 'provider_unavailable'


def test_requested_quota_failure_survives_later_success(tmp_path):
    calls = []
    routes = EngineeringRegistry(Database(tmp_path / 'routes.db'), [
        Registration(fake('codex', calls, 'provider_quota'), 'api', True),
        Registration(fake('claude_code', calls), 'cli', True)])
    _, used = routes.complete({}, '', 2, stage='implementing')
    assert used == 'claude_code' and calls == ['codex', 'claude_code']
    assert routes.last_route['fallback_reason'] == 'provider_quota'


def test_requested_failure_survives_api_skip_before_later_success(tmp_path):
    calls = []
    routes = EngineeringRegistry(Database(tmp_path / 'routes.db'), [
        Registration(fake('codex_cli', calls, 'provider_invalid_response'), 'cli', True),
        Registration(fake('claude_cli', calls, 'provider_invalid_response'), 'cli', True),
        Registration(fake('codex_api', calls), 'api', True),
        Registration(fake('later_cli', calls), 'cli', True)])
    _, used = routes.complete({}, '', 2, stage='implementing')
    assert used == 'later_cli'
    assert calls == ['codex_cli', 'claude_cli', 'later_cli']
    assert routes.last_route['fallback_reason'] == 'provider_invalid_response'


@pytest.mark.parametrize('unregistered,expected', [
    (True, 'provider_unavailable'),
    (False, 'provider_invocation_failed'),
])
def test_missing_or_unusable_requested_provider_has_truthful_reason(
        tmp_path, unregistered, expected):
    calls = []
    registrations = [Registration(fake('claude_cli', calls), 'cli', True)]
    if not unregistered:
        registrations.insert(0, Registration(fake('codex_cli', calls), 'cli', True))
    routes = EngineeringRegistry(Database(tmp_path / 'routes.db'), registrations)
    if not unregistered:
        routes.observe('codex_cli', 'invocation_failed', 'provider_failure',
                       'engineering_request', 'provider_invocation_failed')
    _, used = routes.complete({}, '', 2, stage='implementing', preferred='codex_cli')
    assert used == 'claude_cli' and calls == ['claude_cli']
    assert routes.last_route['provider_requested'] == 'codex_cli'
    assert routes.last_route['fallback_reason'] == expected


def test_configured_unavailable_requested_route_has_reason_even_when_blocked(tmp_path):
    routes = EngineeringRegistry(Database(tmp_path / 'routes.db'), [
        Registration(fake('codex_cli', []), 'cli', False),
        Registration(fake('claude_cli', []), 'cli', True)])
    with pytest.raises(RoutingBlocked):
        routes.complete({}, '', 2, stage='implementing')
    assert routes.last_route['fallback_reason'] == 'provider_unavailable'


def test_cli_invocation_is_subscription_only_and_cannot_edit_checkout(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, 'codex_executable', lambda: '/usr/local/bin/codex')
    monkeypatch.setenv('OPENAI_API_KEY', 'DO_NOT_PASS')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://api.invalid')
    monkeypatch.setattr(cli.subprocess, 'run', lambda argv, **kw: (
        seen.setdefault('login', (argv, kw)) or None) and
        SimpleNamespace(returncode=0, stdout=b'Logged in using ChatGPT\n'))

    class Process:
        pid = 99999999
        returncode = 0
        stdin = io.BytesIO()

        def __init__(self, argv, **kwargs):
            seen['exec'] = argv, kwargs
            kwargs['stdout'].write(json.dumps({'edits': [{'path': 'src/app.py',
                'content': 'value = 2\n'}]}).encode())

        def communicate(self, data, timeout):
            seen['prompt'] = data

        def wait(self):
            pass

        def poll(self):
            return self.returncode

    monkeypatch.setattr(cli.subprocess, 'Popen', Process)
    result = cli.codex_request({'goal': 'fix'}, 'Return proposal JSON', 2)
    argv, kwargs = seen['exec']
    assert argv[:2] == ['/usr/local/bin/codex', 'exec']
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    assert '--skip-git-repo-check' in argv and '--ephemeral' in argv
    assert 'OPENAI_API_KEY' not in kwargs['env'] and 'OPENAI_BASE_URL' not in kwargs['env']
    assert 'OPENAI_API_KEY' not in seen['login'][1]['env']
    assert kwargs['cwd'].startswith('/tmp/jarvis-codex-')
    assert b'src/app.py' not in seen['prompt']
    assert result['edits'][0]['path'] == 'src/app.py'


def test_non_chatgpt_login_stops_before_exec(monkeypatch):
    monkeypatch.setattr(cli, 'codex_executable', lambda: '/usr/local/bin/codex')
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw:
        SimpleNamespace(returncode=0, stdout=b'Logged in using API key'))
    monkeypatch.setattr(cli.subprocess, 'Popen', lambda *a, **kw:
        pytest.fail('API-authenticated CLI must not be invoked'))
    with pytest.raises(ProviderFailure) as error:
        cli.codex_request({}, 'Return JSON', 2)
    assert error.value.code == 'provider_authentication'


def test_quota_on_stderr_blocks_without_cli_or_api_fallback(tmp_path, monkeypatch):
    calls = []
    routes = registry(tmp_path, calls, 'provider_quota')
    with pytest.raises(RoutingBlocked):
        routes.complete({}, '', 2, stage='implementing')
    assert calls == ['codex_cli']
    assert routes.last_route['provider_requested'] == 'codex_cli'
    assert routes.last_route['provider_used'] is None
    assert routes.last_route['billing_class'] == 'subscription_cli'
    assert routes.last_route['fallback_reason'] == 'provider_quota'

    monkeypatch.setattr(cli, 'codex_executable', lambda: '/usr/local/bin/codex')
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw:
        SimpleNamespace(returncode=0, stdout=b'Logged in using ChatGPT'))

    class QuotaProcess:
        returncode = 1
        stdin = io.BytesIO()

        def __init__(self, argv, **kwargs):
            kwargs['stderr'].write(b'Usage limit reached')

        def communicate(self, data, timeout):
            pass

        def poll(self):
            return self.returncode

        def wait(self):
            pass

    monkeypatch.setattr(cli.subprocess, 'Popen', QuotaProcess)
    with pytest.raises(ProviderFailure) as error:
        cli.codex_request({}, 'Return JSON', 2)
    assert error.value.code == 'provider_quota'


def test_cli_unavailability_blocks_without_api_charge(tmp_path):
    calls = []
    routes = registry(tmp_path, calls, 'provider_unavailable')
    with pytest.raises(RoutingBlocked):
        routes.complete({}, '', 2, stage='implementing')
    assert calls == ['codex_cli']
    assert routes.last_route['provider_used'] is None
    assert routes.last_route['billing_class'] == 'subscription_cli'
