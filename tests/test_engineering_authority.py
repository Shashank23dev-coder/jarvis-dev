"""Authenticated owner capabilities are independent of wording and conversation."""
import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.candidate_broker import Broker
from core.candidate_bridge import project
from core.candidate_tools import CandidateTools
from core.engineering_authority import OwnerDevAuthority, current_owner
from core.engineering_jobs import EngineeringJobs, project_job
from core.candidate_provider import failure_evidence, project_failure, provider_error_code
from core.engineering_tools import runtime
from tools import ToolRegistry, ToolDenied
from tools.candidate_edit import inspection_text, safe_text, CandidatePolicy
from test_self_edit import setup, git
from test_engineering_jobs import enqueue
from test_candidate_bridge import request

pytestmark = pytest.mark.usefixtures('owner')


def test_owner_resume_renews_only_one_attempt_without_resetting_job(setup):
    source, db, factory = setup
    jobs = EngineeringJobs(db)
    job = enqueue(db)
    for attempt in range(3):
        jobs.claim('worker')
        jobs.finish(job['id'], 'worker')
        if attempt < 2:
            jobs.control(job['id'], 'resume')
    before = jobs.get(job['id'])
    for fake in (None, OwnerDevAuthority().model_dump()):
        with pytest.raises(ToolDenied):
            jobs.control(job['id'], 'resume', authority=fake)
    assert jobs.get(job['id']) == before
    assert project_job(before)['recovery']['retry_available'] is True
    resumed = Broker(factory(), source=source).execute(
        request('job_resume', 'Continue this objective', dict(job_id=job['id'])))
    assert resumed['status'] == 'queued' and resumed['attempts'] == 3
    saved = jobs.get(job['id'])
    assert saved['attempt_limit'] == 4
    assert saved['goal'] == job['goal'] and saved['source'] == job['source']
    assert any(e['event'] == 'owner_retry_authorized' for e in saved['history'])
    claimed = jobs.claim('fourth', now=0)
    assert claimed['attempts'] == 4
    assert project_job(project_job(jobs.get(job['id'])))['attempts'] == 4
    # A crashed fourth attempt cannot authorize a fifth attempt for itself.
    assert jobs.claim('crash-recovery', now=931) is None
    assert jobs.get(job['id'])['status'] == 'failed'
    assert jobs.get(job['id'])['attempts'] == 4


def test_successful_job_preserves_revised_candidate_failure_history(setup):
    from core.engineering_worker import EngineeringWorker
    from core.self_edit import REVIEW
    from test_engineering import run_checks
    source, db, factory = setup
    enqueue(db)
    turns = []
    def author(payload, instructions, timeout):
        if instructions == REVIEW:
            return dict(verdict='passed', summary='Independent review passed.')
        turns.append(payload)
        return dict(edits=[dict(path='src/app.py', content='value = 3\n' if len(turns) == 1 else 'value = 2\n')])
    result = EngineeringWorker(factory(model=author, tester=run_checks), source=source).once()
    assert result['status'] == 'completed' and result['failure'] is None
    assert result['failures'][0]['code'] == 'tests_failed'
    assert project_job(result)['failures'][0]['stage'] == 'testing'


def test_failure_projection_is_independent_of_retry_budget_and_reports_truncation(setup):
    job = enqueue(setup[1])
    job['attempts'] = 20
    job['failures'] = [failure_evidence('codex', 'tests_failed', 'testing', i) for i in range(1, 21)]
    projected = project_job(job)
    assert projected['failure_count'] == 20 and projected['failures_truncated']
    assert len(projected['failures']) == 16
    assert projected['failures'][0]['attempt'] == 5 and projected['failures'][-1]['attempt'] == 20
    assert project_job(projected) == projected
    job['failures'] = job['failures'][:4]
    projected = project_job(job)
    assert projected['failure_count'] == 4 and not projected['failures_truncated']
    assert len(projected['failures']) == 4


@pytest.mark.parametrize('legacy', [False, True])
def test_owner_recovers_exhausted_job_with_expired_candidate_lease(setup, legacy):
    import time
    from core.candidate_store import CandidateStore
    _, db, factory = setup
    jobs = EngineeringJobs(db)
    job = enqueue(db)
    for _ in range(3):
        claimed = jobs.claim('worker', now=0)
        if claimed['attempts'] < 3:
            jobs.finish(job['id'], 'worker')
            jobs.control(job['id'], 'resume')
    candidates = CandidateStore(db)
    candidate = candidates.create(job['goal'], [], [], source_context=dict(
        engineering_job_id=job['id'], lease_owner='worker', attempt=3))
    candidates.claim(candidate['id'])
    record = candidates.get(candidate['id'], internal=True)
    record['lease_until'] = time.time() - 1
    record['stage'] = 'testing'
    record['failure'] = failure_evidence('codex', 'tests_failed', 'testing', 3)
    candidates.save(record)
    if legacy:
        with db.transaction() as connection:
            connection.execute("UPDATE candidates SET record=json_remove(record,'$.failures') WHERE id=?", (candidate['id'],))
    assert jobs.claim('recovery', now=931) is None
    recovered = jobs.get(job['id'])
    assert recovered['status'] == recovered['current_stage'] == 'failed'
    assert recovered['failure']['code'] == 'worker_failed'
    assert any(f['code'] == 'tests_failed' and f['attempt'] == 3 for f in recovered['failures'])
    candidate = candidates.get(candidate['id'])
    assert candidate['failure']['code'] == 'worker_failed' and candidate['failure']['attempt'] == 3
    assert any(e['event'] == 'candidate_interrupted' for e in candidate['events'])
    assert jobs.control(job['id'], 'resume', authority=OwnerDevAuthority())['status'] == 'queued'
    assert not candidates.get(candidate['id'])['busy']
    assert jobs.claim('new')['attempts'] == 4


@pytest.mark.parametrize('value', ['short', ' ' * 40, 'a' * 513, 'a' * 31 + '\x00'])
def test_development_owner_tokens_require_production_strength(value):
    from core.deployment import Settings
    with pytest.raises(ValueError):
        Settings.from_env({'JARVIS_AUTH_TOKEN': value})
    with pytest.raises(ValueError):
        Settings(auth_token=value)


def test_any_owner_conversation_reads_and_controls_existing_job(setup):
    source, db, factory = setup
    jobs = EngineeringJobs(db)
    job = enqueue(db)
    jobs.control(job['id'], 'pause')
    broker = Broker(factory(), source=source)
    for goal in ('What happened?', 'Continue the repair whenever ready.', 'show me where we stand'):
        read = request('job_inspect', goal, dict(job_id=job['id']))
        assert read['conversation_id'] != job['source']['conversation_id']
        assert broker.execute(read)['status'] == 'blocked'
    resumed = broker.execute(request('job_resume', 'carry on', dict(job_id=job['id'])))
    assert resumed['status'] == 'queued'
    assert jobs.get(job['id'])['source'] == job['source']


def test_owner_can_inspect_job_candidate_from_another_conversation(setup):
    from core.engineering_worker import EngineeringWorker
    from test_engineering import model, run_checks
    source, db, factory = setup
    enqueue(db)
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    broker = Broker(factory(), source=source)
    candidate = broker.execute(request('inspect', 'Give me its evidence', dict(candidate_id=result['candidate_id'])))
    assert candidate['state'] == 'passed'
    assert candidate['reviewer_result']['verdict'] == 'passed'
    assert project(candidate) == candidate
    assert not {'worktree', 'diff', 'source_context'} & candidate.keys()


def test_missing_owner_cannot_be_replaced_by_model_or_text(setup):
    _, db, factory = setup
    current_owner.set(None)
    tools = CandidateTools('Create a self-edit candidate', db, lambda _: pytest.fail('must not dispatch'),
        source_context=dict(task_id=str(uuid4()), conversation_id=str(uuid4()), authority=OwnerDevAuthority().model_dump()))
    assert 'prepare_engineering' not in tools.bind(ToolRegistry()).schemas()
    with pytest.raises(ToolDenied):
        tools.prepare('codex')
    with pytest.raises(ToolDenied):
        tools.action('inspect', str(uuid4()))


def test_serialized_authority_cannot_authenticate_broker_or_client(setup):
    from core.candidate_bridge import CandidateClient
    from core.engineering_authority import require_owner
    source, _, factory = setup
    binding = current_owner.set(None)
    try:
        for raw in ({}, OwnerDevAuthority().model_dump()):
            with pytest.raises(ToolDenied):
                require_owner(raw)
        raw = request('job_list', 'status', {})
        with pytest.raises(ToolDenied):
            Broker(factory(), source=source).execute(raw)
        with pytest.raises(ToolDenied):
            CandidateClient('status', {k: raw[k] for k in
                ('task_id', 'conversation_id', 'authority')}).invoke('job_list', {})
    finally:
        current_owner.reset(binding)


def test_http_authentication_issues_owner_for_cross_conversation_inspection(setup):
    import secrets
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.deployment import DeploymentMiddleware, Settings
    from core.engineering_tools import invoke
    from core.engineering_worker import EngineeringWorker
    from test_engineering import model, run_checks
    source, db, factory = setup
    enqueue(db)
    job = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    application = FastAPI()
    token = secrets.token_urlsafe(32)
    application.add_middleware(DeploymentMiddleware, settings=Settings(auth_token=token))

    @application.get('/inspect')
    @application.post('/inspect')
    def inspect():
        context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
        assert context['conversation_id'] != job['source']['conversation_id']
        tools = CandidateTools('Where are we?', db, lambda _: factory(), source_context=context)
        return dict(job=invoke(tools, 'inspect', job['id']).model_dump(),
                    candidate=tools.action('inspect', job['candidate_id']).model_dump())

    with TestClient(application) as client:
        assert client.get('/inspect').status_code == 401
        assert client.get('/inspect', headers={'Authorization': 'Bearer invalid'}).status_code == 401
        response = client.get('/inspect', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200
        assert response.json()['job']['jobs'][0]['status'] == 'completed'
        assert response.json()['candidate']['state'] == 'passed'
        headers = {'Authorization': 'Bearer ' + token, 'Origin': 'http://testserver'}
        assert client.post('/inspect', headers=headers).status_code == 200
        headers['Origin'] = 'https://untrusted.example'
        assert client.post('/inspect', headers=headers).status_code == 403


def test_legacy_candidate_http_routes_do_not_bypass_owner_or_production_boundary(tmp_path, setup, monkeypatch):
    import secrets
    import main
    from fastapi.testclient import TestClient
    from core.deployment import Settings
    from core import candidate_broker
    source, _, factory = setup
    broker_class = candidate_broker.Broker
    monkeypatch.setattr(main, 'SelfEditService', lambda database: factory())
    monkeypatch.setattr(candidate_broker, 'Broker', lambda service: broker_class(service, source=source))
    token = secrets.token_urlsafe(32)
    for mode, authenticated, expected in [('development', False, 403),
            ('development', True, 404), ('production', True, 403)]:
        database = tmp_path / (mode + str(authenticated) + '.sqlite3')
        settings = Settings(mode=mode, database_path=database, state_dir=tmp_path,
                            auth_token=token if authenticated else '')
        with TestClient(main.create_app(settings=settings)) as client:
            response = client.get('/candidates/' + str(uuid4()), headers=(
                {'Authorization': 'Bearer ' + token} if authenticated else {}))
            assert response.status_code == expected


def test_inspection_redacts_fixture_literals_but_edit_secret_guard_stays():
    source = "settings = dict(OPENAI_API_KEY='test-provider-key')\n"
    assert 'test-provider-key' not in inspection_text(source)
    assert 'OMITTED' in inspection_text(source)
    with pytest.raises(ToolDenied):
        safe_text(source)
    for path in ('.env', '.ssh/id_rsa', '.aws/config', 'security/trading.py'):
        with pytest.raises(ToolDenied):
            CandidatePolicy().edit_path(path)


def test_actual_inspection_fixture_reaches_provider_without_disclosing_it(setup):
    source, db, factory = setup
    path = source / 'tests/test_fixture.py'
    path.write_text("value = dict(OPENAI_API_KEY='test-provider-key')\n")
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Credential-like fixture')
    def author(payload, instructions, timeout):
        assert 'test-provider-key' not in json.dumps(payload)
        if 'original_files' in payload:
            return dict(verdict='passed', summary='Verified')
        assert 'OMITTED' in payload['files']['tests/test_fixture.py']
        return dict(edits=[dict(path='src/app.py', content='value = 2\n')])
    record = factory(model=author).start('Fix value', ['tests/test_fixture.py', 'src/app.py'], ['tests/test_app.py'])
    assert record['state'] == 'passed', record


@pytest.mark.parametrize('status,code,expected,retryable', [
    (429, 'insufficient_quota', 'provider_quota', False),
    (429, None, 'provider_rate_limit', True), (401, None, 'provider_authentication', False),
    (500, None, 'provider_unavailable', True), (400, None, 'provider_request_rejected', False)])
def test_provider_failures_are_closed_and_actionable(status, code, expected, retryable):
    exc = RuntimeError('PRIVATE provider text /host/path')
    exc.status_code, exc.code = status, code
    value = failure_evidence('codex', provider_error_code(exc), 'implementing', 2)
    projected = project_failure(value | {'raw': str(exc)})
    assert projected['code'] == expected and projected['retryable'] is retryable
    assert projected['stage'] == 'implementing' and projected['attempt'] == 2
    assert 'PRIVATE' not in json.dumps(projected)
    assert project_failure(projected) == projected


def test_job_retains_failure_history_after_successful_retry(setup):
    _, db, _ = setup
    job = enqueue(db)
    jobs = EngineeringJobs(db)
    jobs.claim('first')
    failure = failure_evidence('codex', 'provider_rate_limit', 'implementing', 1)
    result = jobs.finish(job['id'], 'first', failure=failure)
    assert project_job(result)['blocker'] == 'provider_rate_limit'
    jobs.control(job['id'], 'resume')
    assert project_job(jobs.get(job['id']))['failures'][0]['code'] == 'provider_rate_limit'


def test_owner_can_repair_inputs_without_replacing_objective(setup):
    _, db, _ = setup
    job = enqueue(db)
    jobs = EngineeringJobs(db)
    authority = OwnerDevAuthority()
    with pytest.raises(ToolDenied):
        jobs.revise_inputs(job['id'], job['arguments'], authority=authority)
    jobs.control(job['id'], 'pause')
    args = job['arguments'] | {'inspect_paths': ['src/other.py']}
    updated = jobs.revise_inputs(job['id'], args, authority=authority)
    assert updated['arguments'] == args
    assert updated['input_revisions'][0]['arguments'] == job['arguments']
    assert all(updated[k] == job[k] for k in ('id', 'goal', 'action', 'source'))
    for invalid in (None, authority.model_dump()):
        with pytest.raises(ToolDenied):
            jobs.revise_inputs(job['id'], args, authority=invalid)
    with pytest.raises(ToolDenied):
        jobs.revise_inputs(job['id'], args | {'inspect_paths': ['.env']}, authority=authority)


def test_owner_listing_includes_jobs_older_than_recent_runtime_window(setup):
    from core.engineering_tools import invoke
    _, db, factory = setup
    ids = {enqueue(db)['id'] for _ in range(12)}
    tools = CandidateTools('status', db, lambda _: factory(), source_context=dict(
        task_id=str(uuid4()), conversation_id=str(uuid4())))
    found, before = [], None
    while True:
        page = invoke(tools, 'list', before=before)
        assert len(page.jobs) <= 5
        found.extend(j['id'] for j in page.jobs)
        before = page.next_before
        if before is None:
            break
    assert set(found) == ids and len(found) == len(ids)


def test_controls_share_candidate_management_fence(setup):
    from core.engineering_jobs import management_lock
    _, db, _ = setup
    jobs = EngineeringJobs(db)
    job = enqueue(db)
    with management_lock(db):
        with pytest.raises(ToolDenied):
            jobs.control(job['id'], 'pause')
    assert jobs.get(job['id'])['status'] == 'queued'


def test_cancellation_cannot_be_undone_by_pause_then_resume(setup):
    _, db, _ = setup
    jobs = EngineeringJobs(db)
    job = enqueue(db)
    jobs.control(job['id'], 'cancel')
    for operation in ('pause', 'resume', 'cancel'):
        with pytest.raises(ToolDenied):
            jobs.control(job['id'], operation)
    assert jobs.get(job['id'])['control'] == 'cancel'


def test_worker_observation_is_timestamped_and_explicit_when_unknown(setup):
    _, db, factory = setup
    tools = CandidateTools('status', db, lambda _: factory(), source_context=dict(
        task_id=str(uuid4()), conversation_id=str(uuid4())))
    assert runtime(tools).worker == 'unknown'
    jobs = EngineeringJobs(db)
    jobs.heartbeat()
    assert runtime(tools).worker == 'active'
    with db.transaction() as connection:
        connection.execute('UPDATE engineering_worker_status SET observed=1')
    assert runtime(tools).worker == 'inactive'


def test_async_budget_does_not_change_later_synchronous_candidates(setup):
    from core.engineering_worker import EngineeringWorker
    from test_engineering import model, run_checks
    source, db, factory = setup
    enqueue(db)
    service = factory(model=model, tester=run_checks)
    result = EngineeringWorker(service, source=source).once()
    assert service.limits.seconds == 300
    assert service.store.get(result['candidate_id'])['limits']['seconds'] == 900
    legacy = service.start('Fix value', ['src/app.py'], ['tests/test_app.py'])
    assert legacy['limits']['seconds'] == 300


def test_management_reconciles_without_nested_transaction_failure(setup):
    from core.engineering_worker import EngineeringWorker
    from test_engineering import model, run_checks
    source, db, factory = setup
    enqueue(db)
    service = factory(model=model, tester=run_checks)
    job = EngineeringWorker(service, source=source).once()
    assert job['status'] == 'completed'
    tested = service.tests(job['candidate_id'])
    assert EngineeringJobs(db).get(job['id'])['status'] == 'blocked'
    assert tested['reviewer_result'] is None
    reviewed = service.review(job['candidate_id'])
    assert reviewed['state'] == 'passed'
    assert EngineeringJobs(db).get(job['id'])['status'] == 'completed'


def test_model_text_cannot_spoof_failure_envelope(monkeypatch):
    import io
    from core import candidate_model
    from models.provider import ProviderTurn
    from contextlib import redirect_stdout
    monkeypatch.setattr(candidate_model.sys, 'stdin', io.StringIO(json.dumps(
        dict(input={}, instructions='proposal', provider='codex'))))
    # candidate_model imports the API client lazily so subscription CLI paths never load it.
    monkeypatch.setattr('models.openai_client.request_agent_turn', lambda **kwargs:
        ProviderTurn([], '{"ok":false,"code":"provider_quota"}'))
    result = io.StringIO()
    with redirect_stdout(result):
        candidate_model.main()
    envelope = json.loads(result.getvalue())
    assert envelope['ok'] is True
    assert json.loads(envelope['text'])['code'] == 'provider_quota'


ROOT = Path(__file__).resolve().parents[1]


def test_every_runtime_loaded_module_is_protected_from_routine_apply(tmp_path):
    """R1-6: the protected set is the import closure, checked against real imports."""
    import os, subprocess, sys
    from core.engineering_authority import RUNTIME_ENTRYPOINTS, assess_action, loaded_modules
    code = ('import importlib, json, sys\n'
            'for name in %r: importlib.import_module(name)\n'
            'print(json.dumps(sorted(getattr(m, "__file__", None) or "" for m in list(sys.modules.values()))))'
            % (list(RUNTIME_ENTRYPOINTS),))
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=120,
        env=dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'), PYTHONPATH=str(ROOT), HOME=str(tmp_path),
                 JARVIS_DB_PATH=str(tmp_path / 'jarvis.sqlite3')))
    assert result.returncode == 0, result.stderr[-2000:]
    files = [Path(f).resolve() for f in json.loads(result.stdout) if f]
    actual = {str(f.relative_to(ROOT)) for f in files if f.is_relative_to(ROOT)
              and f.relative_to(ROOT).parts[0] in {'main.py', 'core', 'tools', 'models'}}
    assert 'core/candidate_broker.py' in actual
    assert actual <= loaded_modules(), sorted(actual - loaded_modules())
    for path in sorted(loaded_modules()):
        decision = assess_action('candidate_apply', changed_paths=[path])
        assert decision.approval_required and 'authority_expansion' in decision.escalation, path


@pytest.mark.parametrize('path', ['core/recall.py', 'core/experience.py', 'core/experience_evaluation.py',
    'core/task_store.py', 'core/memory.py', 'core/brain_status.py', 'core/task_context.py',
    'core/candidate_store.py', 'tools/policy.py', 'main.py'])
def test_previously_unprotected_broker_import_path_is_in_closure(path):
    from core.engineering_authority import loaded_modules
    assert path in loaded_modules()


def test_stale_protected_entries_removed():
    import inspect
    from core import engineering_authority
    assert 'core/agent_runner.py' not in inspect.getsource(engineering_authority)
    assert not (ROOT / 'core/agent_runner.py').exists()


@pytest.mark.parametrize('path', ['tests/conftest.py', 'tests/test_tools.py', 'tests/test_authority_model.py',
    'tests/test_authority_acceptance.py', 'tests/test_candidate_apply.py', 'tests/test_candidate_tools.py',
    'tests/test_engineering_authority.py', 'tests/test_owner_readonly_flow.py', 'tests/test_deployment.py',
    'tests/test_deployment_status.py', 'tests/test_sandbox_docker.py', 'tests/test_sandbox_imports.py'])
def test_security_tests_escalate_on_apply_but_remain_editable_in_isolation(path):
    from core.engineering_authority import assess_action
    CandidatePolicy().edit_path(path)  # Preparation in an isolated candidate stays allowed.
    assert assess_action('candidate_apply', phase='prepare', changed_paths=[path]).allowed
    decision = assess_action('candidate_apply', changed_paths=[path])
    assert decision.approval_required and decision.escalation == ['authority_expansion']


@pytest.mark.parametrize('path', ['src/app.py', 'docs/readme.md', 'tests/test_app.py', 'static/app.js',
    'docs/deployment.md', 'tests/test_recall.py'])
def test_unloaded_routine_paths_stay_autonomous(path):
    from core.engineering_authority import assess_action
    assert assess_action('candidate_apply', changed_paths=[path]).allowed


# Restored coverage for protection cases removed by 9b872a1..084807b, adapted to
# the new design: preparation is allowed in isolation; applying escalates;
# secrets and path escapes stay hard denials.
@pytest.mark.parametrize('path', ['security_rules.py', 'access_policy.py', 'tools/policy.py',
    'tools/repository.py', 'core/candidate_tools.py', 'core/self_edit.py', 'core/candidate_store.py',
    'core/engineering_jobs.py', 'core/engineering_worker.py', 'core/engineering_tools.py',
    'core/engineering_providers.py', 'core/candidate_claude.py'])
def test_restored_protected_paths_escalate_on_apply(path):
    from core.engineering_authority import assess_action
    try:
        CandidatePolicy().edit_path(path)  # Isolated preparation may proceed...
    except ToolDenied:
        pass  # ...or the candidate policy may still hard-deny it (stricter).
    decision = assess_action('candidate_apply', changed_paths=[path])
    assert decision.approval_required and 'authority_expansion' in decision.escalation


@pytest.mark.parametrize('path', ['.env', '.env.local', '../outside.py', '/etc/passwd', '.git/config',
    'docs/credentials.md', 'private.key', 'docs/../docs/persistence.md'])
def test_restored_secret_and_escape_paths_still_denied(path):
    with pytest.raises(ToolDenied):
        CandidatePolicy().edit_path(path)


@pytest.mark.parametrize('path', ['core/engineering_jobs.py', 'core/engineering_worker.py',
    'core/engineering_tools.py', 'core/candidate_store.py', 'core/engineering_providers.py', 'core/candidate_claude.py'])
def test_restored_protected_job_paths_queue_but_completion_waits_for_approval(setup, path):
    db = setup[1]
    job = enqueue(db, inspect_paths=[path])
    assert job['status'] == 'queued'
    jobs = EngineeringJobs(db)
    jobs.claim('worker')
    candidate = dict(id=str(uuid4()), state='passed', busy=False, goal=job['goal'], files_changed=[path],
        tested_fingerprint='f', reviewed_fingerprint='f', reviewer_result={'verdict': 'passed'},
        test_results=[], failures=[])
    finished = jobs.finish(job['id'], 'worker', candidate)
    assert finished['status'] == 'waiting_for_approval'
    assert finished['authority']['escalation'] == ['authority_expansion']
