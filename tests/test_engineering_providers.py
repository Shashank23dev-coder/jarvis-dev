"""Registry and CLI checks never require a real authenticated provider."""
import json
from types import SimpleNamespace as NS

import pytest
from core.database import Database
from core.candidate_provider import ProviderFailure
from core.engineering_providers import EngineeringRegistry, Registration, RoutingBlocked
from core import candidate_claude as claude
from test_self_edit import setup

pytestmark = pytest.mark.usefixtures('owner')


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


def test_registered_additional_provider_routes_and_projects_job(setup):
    from core.candidate_action import EngineeringAction, goal_digest
    from core.engineering_jobs import EngineeringJobs, project_job
    from test_engineering import ARGS
    from uuid import uuid4
    database = setup[1]
    providers = EngineeringRegistry(database, [Registration(adapter('grok'), 'api', True)])
    assert providers.complete({}, '', 1, stage='implementing')[1] == 'grok'
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    goal = 'Fix a small bug in Jarvis'
    action = EngineeringAction(provider='grok', scope='code', goal_digest=goal_digest(goal), **context)
    job = EngineeringJobs(database).create(goal, action.model_dump(), ARGS, context)
    assert project_job(job)['provider'] == 'grok'
    with pytest.raises(RoutingBlocked):
        providers.complete({}, '', 1, stage='reviewing', exclude=['grok'])


@pytest.mark.parametrize('code', ['provider_unavailable', 'provider_quota', 'provider_authentication'])
def test_codex_failure_falls_back_and_observations_survive_restart(tmp_path, code):
    r = registry(tmp_path, codex=adapter('codex', code))
    result, name = r.complete({}, '', 1, stage='implementing')
    assert name == 'claude_code' and result == {'edits': []}
    assert r.last_route['fallback_reason'] == code
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


def test_claude_cli_requires_subscription_authentication(monkeypatch):
    monkeypatch.setattr(claude, 'claude_executable', lambda: '/user/claude')
    monkeypatch.setattr(claude, 'run_claude', lambda *a, **kw:
        (0, b'{"loggedIn":true,"authMethod":"api_key"}'))
    assert claude.probe_claude(subscription_only=True) == 'not_authenticated'
    with pytest.raises(ProviderFailure) as error:
        claude.claude_request({}, '', 2, subscription_only=True)
    assert error.value.code == 'provider_authentication'


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
    assert args[args.index('--permission-mode')+1] == 'plan'
    assert '--restricted' in args and '--safe-mode' in args
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


@pytest.mark.parametrize('mode,provider,outcome', [
    ('codex', 'codex', 'codex_autonomous'),
    ('fallback', 'claude_code', 'claude_autonomous'),
    ('mixed', 'claude_code', 'multi_provider'),
    ('blocked', 'codex', 'unknown')])
def test_engineering_job_routes_and_durable_attribution(setup, monkeypatch, mode, provider, outcome):
    from core import self_edit
    from core.engineering_worker import EngineeringWorker
    from core.engineering_jobs import project_job
    from core.candidate_bridge import project
    from test_engineering_jobs import enqueue
    from test_engineering import run_checks, model
    source, database, factory = setup
    calls = []
    def transport(payload, instructions, timeout, *, provider='openai'):
        calls.append(provider)
        if provider == 'codex' and (mode in {'fallback', 'blocked'} or mode == 'mixed' and calls.count('codex') > 1):
            raise ProviderFailure(provider, 'provider_quota')
        if provider == 'claude_code' and mode == 'blocked':
            raise ProviderFailure(provider, 'provider_authentication')
        if mode == 'mixed' and provider == 'codex':
            return {'edits': [{'path': 'src/app.py', 'content': 'value = 3\n'}]}
        return model(payload, instructions, timeout)
    monkeypatch.setattr(self_edit, 'model_request', transport)
    # Historical provider IDs remain readable and routable in the legacy-only
    # registry; production defaults use the explicit CLI/API registrations.
    def legacy_defaults(database, model_request):
        def historical(name):
            return NS(name=name, complete=lambda payload, instructions, timeout:
                model_request(payload, instructions, timeout, provider=name),
                probe=lambda timeout: 'unknown')
        return EngineeringRegistry(database, [Registration(historical('codex'), 'api', True),
            Registration(historical('claude_code'), 'cli', True),
            Registration(historical('openai'), 'api', True, capabilities=('reviewing',))])
    monkeypatch.setattr(EngineeringRegistry, 'defaults', staticmethod(legacy_defaults))
    monkeypatch.setenv('OPENAI_API_KEY', 'configured-test-key')
    monkeypatch.setattr(claude, 'claude_executable', lambda: '/user/claude')
    monkeypatch.setattr(claude, 'probe_claude', lambda _: 'unknown')
    job = enqueue(database)
    service = factory(model=transport, tester=run_checks)
    result = EngineeringWorker(service, source=source).once()
    candidate = service.store.get(result['candidate_id'])
    assert result['id'] == job['id'] and result['provider'] == provider
    assert result['attribution']['outcome'] == outcome
    assert project(candidate)['attribution'] == result['attribution']
    assert project_job(project_job(result)) == project_job(result)
    if mode == 'blocked':
        assert result['status'] == 'blocked'
        assert result['provider_blocker']['stage'] == 'implementing'
        assert [r['state'] for r in result['provider_blocker']['reasons']] == ['quota_blocked', 'not_authenticated']
    else:
        assert result['status'] == 'completed', candidate
        assert candidate['improvement']['implementation_provider'] == provider
        assert result['attribution']['review_providers'] == ['openai']
        assert all(r['execution_origin'] == 'provider' for r in candidate['provider_results'])
    database.close()
    from core.engineering_jobs import EngineeringJobs
    assert EngineeringJobs(Database(database.path)).get(job['id'])['attribution']['outcome'] == outcome


def test_injected_adapter_is_operator_assisted_not_provider_autonomous(setup, monkeypatch):
    from core.engineering_worker import EngineeringWorker
    from test_engineering_jobs import enqueue
    from test_engineering import run_checks, model
    source, database, factory = setup
    enqueue(database)
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    assert result['status'] == 'completed'
    assert result['attribution'] == dict(outcome='operator_assisted', operator_assisted=True,
                                      implementation_providers=[], review_providers=[])


@pytest.mark.parametrize('operation', ['inspect', 'tests', 'review'])
def test_external_worktree_change_marks_operator_assisted(setup, monkeypatch, operation):
    from core import self_edit
    from test_engineering import ARGS, model, run_checks
    from pathlib import Path
    _, _, factory = setup
    def transport(payload, instructions, timeout, **kwargs):
        return model(payload, instructions, timeout)
    monkeypatch.setattr(self_edit, 'model_request', transport)
    monkeypatch.setenv('OPENAI_API_KEY', 'configured-test-key')
    service = factory(model=transport, tester=run_checks)
    candidate = service.engineer('Fix value', **ARGS)
    assert candidate['attribution']['outcome'] == 'multi_provider'
    Path(candidate['worktree'], 'src/app.py').write_text('value = 2\n# Operator adjustment\n')
    result = getattr(service, operation)(candidate['id'])
    assert result['attribution']['outcome'] == 'operator_assisted'


def test_legacy_response_is_not_claimed_autonomous():
    from core.candidate_provider import work_attribution
    response = dict(provider='codex', stage='implementing', attempt=1, iteration=1,
                    status='applied', response_sha256='a'*64)
    assert work_attribution({'provider_results': [response]})['outcome'] == 'unknown'


def test_review_fallback_never_uses_candidate_author(tmp_path):
    r = registry(tmp_path)
    from core.engineering_providers import Registration
    r = EngineeringRegistry(r.database, [Registration(adapter('codex'), 'api', True),
        Registration(adapter('claude_code'), 'cli', True),
        Registration(adapter('openai', 'provider_unavailable'), 'api', True)])
    _, name = r.complete({}, '', 1, stage='reviewing', exclude=['codex'])
    assert name == 'claude_code'
    with pytest.raises(RoutingBlocked):
        r.complete({}, '', 1, stage='reviewing', exclude=['codex', 'claude_code'])


def test_malformed_response_falls_back_without_success_evidence(tmp_path):
    r = registry(tmp_path)
    def validate(value):
        raise ValueError('SECRET in invalid model output')
    with pytest.raises(RoutingBlocked) as error:
        r.complete({}, '', 1, stage='implementing', validate=validate)
    assert r.snapshot()['last_successful_route'] is None
    assert 'SECRET' not in json.dumps(error.value.blocker)
    assert all(v['state'] == 'invocation_failed' for v in error.value.blocker['reasons'])


def test_job_attribution_keeps_provider_history_and_operator_assistance_across_retries():
    from core.candidate_provider import merge_attribution
    codex = dict(outcome='codex_autonomous', implementation_providers=['codex'],
                 review_providers=['openai'], operator_assisted=False)
    claude_work = dict(codex, outcome='claude_autonomous', implementation_providers=['claude_code'])
    merged = merge_attribution(codex, claude_work)
    assert merged['outcome'] == 'multi_provider'
    assert merged['implementation_providers'] == ['claude_code', 'codex']
    assert merge_attribution(merged, dict(claude_work, operator_assisted=True))['outcome'] == 'operator_assisted'


@pytest.mark.parametrize('script,expected', [
    ('import time; time.sleep(5)', 'provider_timeout'),
    ('import sys; sys.stdout.write("x" * 256001)', 'provider_output_limit')])
def test_bounded_cli_subprocess_failure(monkeypatch, script, expected):
    import sys
    monkeypatch.setattr(claude, 'claude_executable', lambda: sys.executable)
    with pytest.raises(ProviderFailure) as error:
        claude.run_claude(['-c', script], timeout=0.1 if expected == 'provider_timeout' else 2)
    assert error.value.code == expected


def test_cli_subprocess_uses_isolated_cwd_and_discards_stderr(monkeypatch):
    import sys
    from pathlib import Path
    monkeypatch.setattr(claude, 'claude_executable', lambda: sys.executable)
    code, output = claude.run_claude(['-c',
        'import os,sys; sys.stderr.write("SECRET"); print(os.getcwd())'], timeout=2)
    assert code == 0 and 'SECRET' not in output.decode()
    directory = Path(output.decode().strip())
    assert directory.name.startswith('jarvis-claude-') and not directory.exists()


def test_expired_route_budget_does_not_claim_uninvoked_provider_failure(tmp_path):
    import time
    def slow(*args):
        time.sleep(0.01)
        raise ProviderFailure('codex', 'provider_timeout')
    r = registry(tmp_path, codex=NS(name='codex', complete=slow))
    with pytest.raises(RoutingBlocked) as error:
        r.complete({}, '', 0.001, stage='implementing')
    assert error.value.blocker['budget_exhausted']
    assert r.state('claude_code').state == 'unknown'
    assert r.state('claude_code').last_failure is None


def test_user_nvm_install_detection_and_trusted_override(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(claude.shutil, 'which', lambda _: None)
    monkeypatch.delenv('JARVIS_CLAUDE_EXECUTABLE', raising=False)
    binary = tmp_path / '.nvm/versions/node/v22/bin/claude'
    binary.parent.mkdir(parents=True)
    binary.write_text('fixture')
    binary.chmod(0o700)
    # Docker scratch is deliberately noexec. Model executable permissions for
    # this discovery-only fixture; retain the real access check elsewhere.
    real_access = claude.os.access
    def fixture_access(path, mode):
        if Path(path).is_relative_to(tmp_path):
            return mode == os.X_OK and Path(path).is_file() and bool(Path(path).stat().st_mode & 0o111)
        return real_access(path, mode)
    monkeypatch.setattr(claude.os, 'access', fixture_access)
    assert claude.claude_executable() == str(binary)
    monkeypatch.setenv('JARVIS_CLAUDE_EXECUTABLE', 'relative/claude')
    assert claude.claude_executable() is None
    monkeypatch.setenv('JARVIS_CLAUDE_EXECUTABLE', str(binary))
    assert claude.claude_executable() == str(binary)
    binary.chmod(0o600)
    assert claude.claude_executable() is None
    monkeypatch.setenv('JARVIS_CLAUDE_EXECUTABLE', str(binary.parent))
    assert claude.claude_executable() is None
    binary.chmod(0o700)
    second = tmp_path / '.nvm/versions/node/v24/bin/claude'
    second.parent.mkdir(parents=True)
    second.write_text('fixture')
    second.chmod(0o700)
    monkeypatch.delenv('JARVIS_CLAUDE_EXECUTABLE')
    assert claude.claude_executable() is None
    monkeypatch.setenv('JARVIS_CLAUDE_EXECUTABLE', str(binary))
    assert claude.claude_executable() == str(binary)


def test_evidence_with_timezone_offset_uses_utc_age(tmp_path):
    now = ['2026-10-04T02:00:00+02:00']
    r = registry(tmp_path, clock=lambda: now[0])
    r.observe('codex', 'available', 'provider_response', 'engineering_request', stage='implementing')
    now[0] = '2026-10-04T00:01:00+00:00'
    assert r.state('codex').current_usability == 'available'
    assert r.snapshot()['last_successful_route']['stage'] == 'implementing'


def test_completed_transport_is_reaped_without_signalling_exited_pid(monkeypatch):
    from core import self_edit
    import sys
    original = self_edit.subprocess.Popen
    def transport_process(arguments, **kwargs):
        return original([sys.executable, '-c',
            'import json; print(json.dumps({"ok": True, "text": "{}"}))'], **kwargs)
    monkeypatch.setattr(self_edit.subprocess, 'Popen', transport_process)
    monkeypatch.setattr(self_edit.SelfEditService, '_kill_worker',
                       lambda _: pytest.fail('Completed transport must not receive process signals'))
    assert self_edit.model_request({}, '', 2, provider='claude_code') == {}
