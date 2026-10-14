"""Operator acceptance uses real Git/SQLite/worker processes and scripted providers.

No paid calls, live services, promotion or deployment are used by these tests.
"""
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from core import engineering_operator as operator
from core.candidate_provider import ProposalProvider, ProviderFailure
from core.database import Database
from core.engineering_jobs import EngineeringJobs, project_job
from core.engineering_providers import EngineeringRegistry, Registration
from core.engineering_worker import EngineeringWorker
from core.self_edit import SelfEditService, model_request
from tools import CommandResult, ToolDenied
from test_self_edit import setup, git, tree_bytes
from test_engineering import ARGS

prepare_release_subprocess = operator.prepare_release

pytestmark = pytest.mark.usefixtures('owner')


def read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def release_result(record, *, passed=True):
    from core.deployment_release import now
    sha = record['operator']['commit_sha']
    return dict(commit=sha, prepared=passed, controller_validated=False, required_confirmation=None,
                evidence=dict(commit=sha, at=now(), passed=passed, tests=[
                    dict(shard=shard, phase='ui' if shard == 'ui' else 'python',
                         passed=passed, returncode=0 if passed else 1)
                    for shard in ('python-1', 'python-2', 'python-3', 'ui')]))


@pytest.fixture
def harness(setup, monkeypatch, tmp_path):
    source, db, _ = setup
    log = tmp_path / 'operator-events.jsonl'
    behavior = {}

    def emit(value):
        with log.open('a') as stream:
            stream.write(json.dumps(value) + '\n')

    def transport(payload, instructions, timeout, *, provider):
        role = 'review' if 'candidate' in payload else 'implement'
        calls = [e for e in read_events(log) if e.get('role') == role]
        emit(dict(role=role, provider=provider, payload=payload))
        if role == 'review':
            if behavior.get('quota') and not calls:
                raise ProviderFailure(provider, 'provider_quota')
            if behavior.get('interrupt') and not calls:
                os._exit(7)
            if behavior.get('revision') and not calls:
                return dict(verdict='needs_revision', summary='PRIVATE_REVIEW_COMMENTARY', findings=[dict(
                    path='src/app.py', problem='The constant needs its rationale documented.',
                    requested_change='Add a short comment explaining the corrected value.')])
            if behavior.get('bad_findings'):
                return dict(verdict='needs_revision', summary='Vague commentary only.')
            return dict(verdict='passed', summary='The change and focused evidence satisfy the goal.')
        content = 'value = 2\n' + ('# Expected value for this example.\n' if calls else '')
        return dict(edits=[dict(path='src/app.py', content=content)])

    def registry(database, ignored):
        return EngineeringRegistry(database, [Registration(ProposalProvider(name, transport),
            'cli' if name == 'claude_code' else 'api', True) for name in ('codex', 'claude_code', 'openai')])

    monkeypatch.setattr(EngineeringRegistry, 'defaults', staticmethod(registry))
    monkeypatch.setattr(EngineeringRegistry, 'refresh_claude', lambda *a, **kw: None)

    def tester(repository, phase, paths):
        previous_targeted = sum(e.get('test') == 'targeted' for e in read_events(log))
        emit(dict(test=phase, worktree=str(repository.root)))
        if phase == 'targeted' and behavior.get('targeted_denial') and not previous_targeted:
            if behavior.get('targeted_safety_cause'):
                try:
                    raise ToolDenied(behavior['targeted_safety_cause'])
                except ToolDenied as cause:
                    raise ToolDenied(behavior['targeted_denial']) from cause
            raise ToolDenied(behavior['targeted_denial'])
        content = repository.read_text('src/app.py')
        if phase == 'targeted' and behavior.get('targeted_tamper'):
            Path(repository.root, 'src/app.py').write_text('value = 999\n')
        return CommandResult(('pytest',), 0 if content.splitlines()[0] == 'value = 2' else 1, 'Assertion result')

    def prepare(service, record):
        emit(dict(release=record['operator']['commit_sha'], worktree=record['worktree']))
        assert git(Path(record['worktree']), 'rev-parse', 'HEAD').strip() == record['operator']['commit_sha']
        return release_result(record, passed=not behavior.get('release_failure'))

    monkeypatch.setattr(operator, 'prepare_release', prepare)
    from core.deployment_release import ReleaseService
    monkeypatch.setattr(ReleaseService, 'deploy', lambda *a, **kw: pytest.fail('Operator must never deploy'))

    def worker(database=db):
        service = SelfEditService(database, source_root=source, candidate_root=tmp_path / 'candidates',
                                  model=model_request, tester=tester)
        return EngineeringWorker(service, source=source)

    def enqueue(mode='CODEX_BUILD_CLAUDE_REVIEW', **changes):
        return EngineeringJobs(db).create('Fix the value in Jarvis', None,
            ARGS | dict(project_id='jarvis-core', execution_mode=mode) | changes,
            dict(task_id=str(uuid4()), conversation_id=str(uuid4())))

    return dict(source=source, db=db, log=log, behavior=behavior, worker=worker, enqueue=enqueue,
                tester=tester, emit=emit, registry=registry)


@pytest.mark.parametrize('mode,builder,reviewer', [
    ('CODEX_ONLY', 'codex', 'openai'), ('CLAUDE_ONLY', 'claude_code', 'openai'),
    ('CODEX_BUILD_CLAUDE_REVIEW', 'codex', 'claude_code')])
def test_modes_commit_private_readiness_and_no_promotion(harness, mode, builder, reviewer):
    h = harness
    before = tree_bytes(h['source'])
    queued = h['enqueue'](mode)
    result = h['worker']().once()
    assert result['id'] == queued['id']
    assert result['status'] == 'waiting_for_approval', result
    assert result['operator']['ready'] and result['operator']['phase'] == 'done'
    assert result['operator']['controller_validated'] is False
    assert result['operator']['review_verdict'] == 'passed'
    assert result['operator']['release']['prepared']
    assert result['provider'] == builder
    calls = [e['provider'] for e in read_events(h['log']) if 'role' in e]
    assert calls == [builder, reviewer]
    candidate = h['worker']().service.store.get(result['candidate_id'])
    assert candidate['branch'] == result['branch'] and candidate['worktree'] == result['worktree']
    assert git(Path(result['worktree']), 'rev-parse', 'HEAD').strip() == result['commit_sha']
    assert git(Path(result['worktree']), 'status', '--porcelain') == ''
    assert [t['phase'] for t in candidate['test_results']] == ['targeted', 'full_python', 'full_ui']
    assert {t['release_commit'] for t in candidate['test_results'][1:]} == {result['commit_sha']}
    steps = result['operator']['steps']
    assert all(e['started_at'] and e['ended_at'] and e['status'] == 'completed' for e in steps)
    assert [e['provider'] for e in steps if e['provider']] == [builder, reviewer]
    assert not candidate.get('apply') and not candidate.get('deployment')
    assert tree_bytes(h['source']) == before
    assert h['worker']().once() is None


def test_needs_revision_sends_only_concrete_findings_then_pass(harness):
    h = harness
    h['behavior']['revision'] = True
    h['enqueue']()
    result = h['worker']().once()
    assert result['operator']['ready'], result
    events = read_events(h['log'])
    calls = [e for e in events if 'role' in e]
    assert [e['provider'] for e in calls] == ['codex', 'claude_code', 'codex', 'claude_code']
    assert calls[2]['payload']['findings'][0]['path'] == 'src/app.py'
    assert 'PRIVATE_REVIEW_COMMENTARY' not in json.dumps(calls[2]['payload'])
    assert not {'review', 'messages', 'history'} & calls[2]['payload'].keys()
    assert [e['test'] for e in events if 'test' in e] == ['baseline', 'targeted', 'targeted']
    assert len([e for e in events if 'release' in e]) == 1
    assert result['operator']['revision'] == 2


@pytest.mark.parametrize('failure', ['quota', 'interrupt'])
def test_resume_preserves_implementation_tests_and_worktree_across_restart(harness, monkeypatch, failure):
    h = harness
    h['behavior'][failure] = True
    job = h['enqueue']()
    first = h['worker']().once()
    assert first['status'] == 'blocked', first
    assert first['operator']['phase'] == 'review'
    assert first['failure']['code'] == ('provider_quota' if failure == 'quota' else 'worker_failed')
    candidate = h['worker']().service.store.get(first['candidate_id'])
    assert candidate['tested_fingerprint'] == candidate['fingerprint']
    original_worktree = tree_bytes(Path(candidate['worktree']))
    h['db'].close()
    reopened = Database(h['db'].path)
    jobs = EngineeringJobs(reopened)
    assert jobs.get(job['id'])['operator']['phase'] == 'review'
    jobs.control(job['id'], 'resume')
    monkeypatch.setattr(EngineeringRegistry, 'ttl_seconds', 0)
    second = h['worker'](reopened).once()
    assert second['operator']['ready'], second
    assert second['candidate_ids'] == first['candidate_ids']
    assert second['worktree'] == first['worktree']
    assert tree_bytes(Path(second['worktree'])) == original_worktree
    events = read_events(h['log'])
    assert len([e for e in events if e.get('role') == 'implement']) == 1
    assert [e['test'] for e in events if 'test' in e] == ['baseline', 'targeted']
    assert len(second['failures']) >= 1


@pytest.mark.parametrize('denial,code', [
    ('No usable sandbox: Bubblewrap and Docker unavailable; host execution disabled', 'sandbox_unavailable'),
    ('Candidate deadline exceeded', 'candidate_deadline'),
])
def test_targeted_operational_denial_resumes_same_candidate_without_reimplementation(harness, denial, code):
    h = harness
    h['behavior']['targeted_denial'] = denial
    job = h['enqueue']()
    first = h['worker']().once()
    assert first['status'] == 'blocked' and first['operator']['phase'] == 'validate', first
    assert first['failure']['code'] == code and first['failure']['retryable']
    candidate = h['worker']().service.store.get(first['candidate_id'], internal=True)
    assert candidate['state'] == 'needs_revision'
    assert candidate['operator']['revision'] == 1
    assert candidate['improvement']['baseline_comparison']['baseline']
    original_worktree = tree_bytes(Path(first['worktree']))
    EngineeringJobs(h['db']).control(job['id'], 'resume')
    second = h['worker']().once()
    assert second['status'] == 'waiting_for_approval' and second['operator']['ready'], second
    assert second['candidate_ids'] == first['candidate_ids']
    assert second['worktree'] == first['worktree']
    assert tree_bytes(Path(second['worktree'])) == original_worktree
    events = read_events(h['log'])
    assert len([e for e in events if e.get('role') == 'implement']) == 1
    assert [e['test'] for e in events if 'test' in e] == ['baseline', 'targeted', 'targeted']


def test_targeted_worktree_tamper_remains_terminal(harness):
    h = harness
    h['behavior']['targeted_tamper'] = True
    job = h['enqueue']()
    first = h['worker']().once()
    assert first['status'] == 'blocked' and first['operator']['phase'] == 'validate', first
    candidate = h['worker']().service.store.get(first['candidate_id'], internal=True)
    assert candidate['state'] == 'rejected'
    EngineeringJobs(h['db']).control(job['id'], 'resume')
    second = h['worker']().once()
    assert second['status'] == 'blocked' and second['candidate_ids'] == first['candidate_ids']
    assert len([e for e in read_events(h['log']) if e.get('role') == 'implement']) == 1


@pytest.mark.parametrize('denial', [
    'Operator worktree changed outside its checkpoint',
    'Operator candidate identity mismatch',
    'Candidate commit identity changed',
    'Baseline changed during tests',
    'Docker runtime must have no implicit volumes',
])
def test_integrity_and_sandbox_safety_denials_reject_candidate(harness, denial):
    h = harness
    h['behavior']['targeted_denial'] = denial
    h['enqueue']()
    result = h['worker']().once()
    candidate = h['worker']().service.store.get(result['candidate_id'], internal=True)
    assert result['status'] == 'blocked'
    assert candidate['state'] == 'rejected'
    assert not candidate['operator']['ready']
    assert candidate['failure']['retryable'] is False


def test_sandbox_unavailable_wrapper_does_not_hide_safety_denial(harness):
    h = harness
    h['behavior']['targeted_denial'] = 'No usable sandbox: Bubblewrap and Docker unavailable; host execution disabled'
    h['behavior']['targeted_safety_cause'] = 'Docker runtime must have no implicit volumes'
    h['enqueue']()
    result = h['worker']().once()
    candidate = h['worker']().service.store.get(result['candidate_id'], internal=True)
    assert candidate['state'] == 'rejected'
    assert candidate['failure']['code'] == 'sandbox_integrity_failed'
    assert candidate['failure']['retryable'] is False


def test_release_failure_keeps_pass_and_commit_for_retry(harness, tmp_path):
    from core.candidate_apply import CandidateApply
    from test_candidate_apply import integration_target
    h = harness
    integration_target((h['source'], h['db'], None), tmp_path)
    h['behavior']['release_failure'] = True
    job = h['enqueue']()
    first = h['worker']().once()
    assert first['status'] == 'blocked' and first['operator']['phase'] == 'readiness', first
    assert first['review_result']['verdict'] == 'passed'
    assert first['commit_sha']
    assert not CandidateApply(h['worker']().service).preview(first['candidate_id'])['eligible']
    h['behavior']['release_failure'] = False
    EngineeringJobs(h['db']).control(job['id'], 'resume')
    second = h['worker']().once()
    assert second['operator']['ready'], second
    assert second['commit_sha'] == first['commit_sha']
    assert len([e for e in read_events(h['log']) if 'role' in e]) == 2
    assert CandidateApply(h['worker']().service).preview(second['candidate_id'])['eligible']


def test_provider_modes_do_not_silently_fall_back(harness):
    h = harness
    h['behavior']['quota'] = True
    h['enqueue']()
    result = h['worker']().once()
    assert result['status'] == 'blocked'
    assert [e['provider'] for e in read_events(h['log']) if 'role' in e] == ['codex', 'claude_code']
    assert result['provider_blocker']['reasons'][0]['current_usability'] == 'quota_blocked'


def test_missing_concrete_findings_cannot_become_revision_instructions(harness):
    h = harness
    h['behavior']['bad_findings'] = True
    h['enqueue']()
    result = h['worker']().once()
    assert not result['operator']['ready']
    assert len([e for e in read_events(h['log']) if e.get('role') == 'implement']) == 1


def test_worktrees_are_distinct_and_inherited_fence_blocks_second_worker(harness):
    h = harness
    first = h['enqueue']('CODEX_ONLY')
    second = h['enqueue']('CLAUDE_ONLY')
    fd = os.open(h['db'].path + '.engineering.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, pass_fds=(fd,))
    os.close(fd)
    try:
        assert h['worker']().once() is None
        assert EngineeringJobs(h['db']).get(first['id'])['attempts'] == 0
        assert not h['log'].exists()
    finally:
        child.communicate(b'', timeout=10)
    a, b = h['worker']().once(), h['worker']().once()
    assert a['id'] == first['id'] and b['id'] == second['id']
    assert a['worktree'] != b['worktree'] and a['branch'] != b['branch']
    assert a['operator']['ready'] and b['operator']['ready']


def test_owner_projection_is_concise_closed_and_durable(harness):
    h = harness
    queued = h['enqueue']()
    assert 'CODEX_BUILD_CLAUDE_REVIEW' in operator.owner_status(project_job(queued))
    result = h['worker']().once()
    projected = project_job(result)
    assert project_job(projected) == projected
    assert 'worktree' not in json.dumps(projected) and 'PRIVATE_REVIEW_COMMENTARY' not in json.dumps(projected)
    text = operator.owner_status(projected)
    assert 'review PASS' in text and result['commit_sha'] in text
    assert 'production controller validation is still required' in text
    assert len(text) < 400 and 'python ' not in text and 'docker ' not in text
    for control, word in [('pause', 'paused'), ('cancel', 'cancelled')]:
        message = operator.owner_status(projected | dict(control=control, status='blocked'))
        assert word in message and 'review PASS' not in message
    h['db'].close()
    assert project_job(EngineeringJobs(Database(h['db'].path)).get(result['id'])) == projected


def test_unknown_project_and_invalid_mode_rejected_without_execution(harness):
    h = harness
    with pytest.raises(ToolDenied):
        h['enqueue'](project_id='unknown-project')
    with pytest.raises(ValueError):
        h['enqueue']('CODEX_AND_DEPLOY')
    assert not h['log'].exists()
    assert EngineeringJobs(h['db']).recent() == []


def test_private_release_entry_reuses_fresh_exact_evidence(tmp_path, monkeypatch, capsys, setup):
    from core.deployment_release import ReleaseService, now
    source = setup[0]
    sha = git(source, 'rev-parse', 'HEAD').strip()
    config = dict(source=str(source), journal=str(tmp_path / 'private-preparation'), commit=sha)
    calls = []
    def validate(service, commit):
        calls.append(commit)
        evidence = dict(commit=commit, at=now(), passed=True, tests=[])
        with service.lock():
            service.save(dict(records=[], evidence={commit: evidence}))
        return evidence
    monkeypatch.setattr(ReleaseService, 'validate', validate)
    # Even inherited controller configuration cannot change the journal/authority.
    monkeypatch.setenv('JARVIS_DEPLOY_CONFIG', '/must-not-read-controller-config')
    for _ in range(2):
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(config)))
        operator.main()
        result = json.loads(capsys.readouterr().out)
        assert result['prepared'] and result['commit'] == sha
        assert not result['controller_validated'] and result['required_confirmation'] is None
    assert calls == [sha]


def test_readiness_refuses_incomplete_or_wrong_commit_evidence():
    record = dict(operator=dict(commit_sha='a' * 40), reviewed_fingerprint='b' * 64, test_results=[])
    valid = release_result(record)
    for corrupt in [valid | {'commit': 'c' * 40}, valid | {'controller_validated': True},
                    valid | {'required_confirmation': 'deploy'},
                    valid | {'evidence': valid['evidence'] | {'tests': valid['evidence']['tests'][:2]}}]:
        with pytest.raises(ValueError):
            operator.record_release_tests(record, corrupt)
    assert record['test_results'] == []


def test_injected_adapter_is_honestly_operator_assisted(harness):
    h = harness
    job = h['enqueue']('CODEX_ONLY')
    worker = h['worker']()
    def injected(payload, instructions, timeout):
        if 'candidate' in payload:
            return dict(verdict='passed', summary='Fixture review')
        return dict(edits=[dict(path='src/app.py', content='value = 2\n')])
    worker.service.model = injected
    result = worker.once()
    assert result['id'] == job['id'] and result['operator']['ready'], result
    assert result['attribution']['outcome'] == 'operator_assisted'


def test_worktree_change_during_readiness_preserves_invalidation(harness, monkeypatch):
    h = harness
    def tamper(service, record):
        Path(record['worktree'], 'src/app.py').write_text('value = 999\n')
        return release_result(record)
    monkeypatch.setattr(operator, 'prepare_release', tamper)
    h['enqueue']()
    result = h['worker']().once()
    assert result['status'] == 'blocked' and not result['operator']['ready']
    candidate = h['worker']().service.store.get(result['candidate_id'], internal=True)
    assert candidate['manifest'] and candidate['state'] == 'needs_revision'
    assert candidate['tested_fingerprint'] is None and candidate['reviewer_result'] is None
    assert candidate['execution_origin'] == 'operator_assisted'
    assert 'review PASS' not in operator.owner_status(project_job(result))


@pytest.mark.parametrize('crash_at', ['apply', 'commit'])
def test_crash_after_mutation_resumes_saved_proposal_or_commit(harness, monkeypatch, tmp_path, crash_at):
    from tools.candidate_edit import CandidateEditor
    h = harness
    marker = tmp_path / 'crashed'
    if crash_at == 'apply':
        original = CandidateEditor.apply
        def apply(*args, **kwargs):
            result = original(*args, **kwargs)
            if not marker.exists():
                marker.touch()
                os._exit(7)
            return result
        monkeypatch.setattr(CandidateEditor, 'apply', apply)
    else:
        original = SelfEditService._workgit
        def workgit(self, *args):
            result = original(self, *args)
            if 'commit' in args and not marker.exists():
                marker.touch()
                os._exit(7)
            return result
        monkeypatch.setattr(SelfEditService, '_workgit', workgit)
    h['enqueue']()
    first = h['worker']().once()
    assert first['status'] == 'blocked' and first['operator']['phase'] == crash_at
    EngineeringJobs(h['db']).control(first['id'], 'resume')
    second = h['worker']().once()
    assert second['operator']['ready'], second
    assert second['worktree'] == first['worktree']
    assert len([e for e in read_events(h['log']) if e.get('role') == 'implement']) == 1
    assert len([e for e in read_events(h['log']) if e.get('role') == 'review']) == 1
    assert git(Path(second['worktree']), 'rev-list', '--count', 'HEAD').strip() == '2'


def test_supervisor_process_restart_recovers_completed_work_without_reexecution(harness):
    import multiprocessing
    h = harness
    job = h['enqueue']()
    def supervise():
        # A new connection, as used by the real persistent worker.
        database = Database(h['db'].path)
        worker = h['worker'](database)
        worker.jobs.finish = lambda *args, **kwargs: os._exit(7)
        worker.once()
    child = multiprocessing.get_context('fork').Process(target=supervise)
    child.start()
    child.join(40)
    try:
        assert child.exitcode == 7
        jobs = EngineeringJobs(h['db'])
        first = jobs.get(job['id'])
        assert first['lease_owner'] and first['operator']['ready']
        events = read_events(h['log'])
        # Simulate the existing 930/2460 second lease elapsing after process death.
        with h['db'].transaction() as db:
            record = jobs._get(db, job['id'])
            record['lease_until'] = 0
            jobs._save(db, record, 'fixture_lease_elapsed')
        h['db'].close()
        recovered = h['worker'](Database(h['db'].path)).once()
        assert recovered['operator']['ready'] and recovered['attempts'] == 2
        assert recovered['candidate_ids'] == first['candidate_ids']
        assert recovered['commit_sha'] == first['commit_sha']
        assert read_events(h['log']) == events
        assert 'lease_recovered' in [e['event'] for e in recovered['history']]
    finally:
        if child.is_alive():
            child.kill()
            child.join(10)


def test_public_entry_defaults_and_owner_boundary_use_existing_broker(harness, monkeypatch):
    from core.candidate_broker import Broker
    from core.candidate_bridge import CandidateClient
    from core.candidate_tools import CandidateTools
    from core.engineering_authority import current_owner
    from test_candidate_bridge import request
    h = harness
    broker = Broker(h['worker']().service, source=h['source'])
    requests = []
    def invoke(client, action, arguments):
        raw = request(action, client.goal, arguments)
        raw.update(client.context)
        raw['engineering_action'] = client.engineering_action.model_dump()
        requests.append(raw)
        with monkeypatch.context() as host:
            host.setenv('JARVIS_MODE', 'development')
            result = broker.execute(raw)
            assert broker.execute(raw) == result
            return result
    monkeypatch.setattr(CandidateClient, 'invoke', invoke)
    monkeypatch.setenv('JARVIS_MODE', 'production')
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    tools = CandidateTools('Fix the value in Jarvis', h['db'], source_context=context)
    result = tools.start_operator(**ARGS, project_id='jarvis-core')
    assert result.jobs[0]['operator']['mode'] == 'CODEX_BUILD_CLAUDE_REVIEW'
    assert result.jobs[0]['status'] == 'queued' and len(requests) == 1
    assert not h['log'].exists()  # Submission returns before implementation.
    with pytest.raises(ToolDenied):
        tools.start_operator(**ARGS, project_id='jarvis-core')
    token = current_owner.set(None)
    try:
        unauthenticated = CandidateTools('Fix Jarvis', h['db'], source_context=context)
        with pytest.raises(ToolDenied):
            unauthenticated.start_operator(**ARGS, project_id='jarvis-core')
    finally:
        current_owner.reset(token)
    assert len(requests) == 1


def test_operator_runtime_and_approval_regressions_remain_authority_protected():
    from core.engineering_authority import loaded_modules, protected_apply_path
    assert 'core/engineering_operator.py' in loaded_modules()
    assert protected_apply_path('tests/test_engineering_operator.py')


def test_registered_external_project_uses_existing_full_candidate_tests(harness):
    from core.engineering_portfolio import Portfolio
    h = harness
    portfolio = Portfolio(h['db'])
    jarvis = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    portfolio.register_project(jarvis | dict(repository=None), replace=True)
    portfolio.register_project(jarvis | dict(project_id='fixture', name='Fixture', read_only=False))
    EngineeringJobs(h['db']).create('Fix the value in Fixture', None,
        ARGS | dict(project_id='fixture', execution_mode='CODEX_ONLY'),
        dict(task_id=str(uuid4()), conversation_id=str(uuid4())))
    result = h['worker']().once()
    assert result['operator']['ready'] and result['status'] == 'completed', result
    assert result['operator']['release']['scope'] == 'candidate_tests'
    assert 'production controller' not in operator.owner_status(project_job(result))
    assert [e['test'] for e in read_events(h['log']) if 'test' in e] == ['baseline', 'targeted', 'full_python']
    assert not any('release' in e for e in read_events(h['log']))
    assert result['operator']['observations'][0]['project']['project_id'] == 'fixture'


def test_actual_release_subprocess_reuses_private_exact_commit_evidence(harness, monkeypatch):
    from core.deployment_release import ReleaseConfig, ReleaseService
    h = harness
    h['enqueue']()
    result = h['worker']().once()
    service = h['worker']().service
    record = service.store.get(result['candidate_id'], internal=True)
    journal = service._paths(record)[0] / 'release-preparation'
    private = ReleaseService(ReleaseConfig(record['worktree'], journal, journal / 'unused.env'))
    with private.lock():
        private.save(dict(records=[], evidence={result['commit_sha']: release_result(record)['evidence']}))
    monkeypatch.setenv('JARVIS_DEPLOY_CONFIG', '/must-not-read-controller-config')
    fd = os.open(h['db'].path + '.engineering.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        service.engineering_lock_fd = fd
        prepared = prepare_release_subprocess(service, record)
    finally:
        os.close(fd)
    assert prepared['prepared'] and prepared['commit'] == result['commit_sha']
    assert prepared['controller_validated'] is False and prepared['required_confirmation'] is None
    assert private.load()['records'] == []
