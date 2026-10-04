"""Durable queue, execution fencing, provider path and approval invariants."""
import fcntl
import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core.candidate_action import EngineeringAction, goal_digest
from core.database import Database
from core.engineering_jobs import EngineeringJobs, project_job
from core.engineering_worker import EngineeringWorker
from tools.policy import ToolDenied
from test_self_edit import setup, tree_bytes
from test_engineering import ARGS, model, run_checks


def enqueue(database, goal='Fix the value', **changes):
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    action = EngineeringAction(provider='codex', scope='code', goal_digest=goal_digest(goal), **context)
    return EngineeringJobs(database).create(goal, action.model_dump(), ARGS | changes, context)


def test_persistence_idempotency_and_read_only_history(setup):
    _, db, _ = setup
    job = enqueue(db)
    db.close()
    jobs = EngineeringJobs(Database(db.path))
    assert jobs.get(job['id']) == job
    assert jobs.create(job['goal'], job['action'], job['arguments'], job['source']) == job
    assert jobs.recent() == [job]
    with pytest.raises(ToolDenied):
        jobs.create('other', job['action'], job['arguments'], job['source'])
    assert jobs.get(job['id'])['history'] == job['history']


def test_worker_candidate_tests_review_and_no_apply_deploy(setup, monkeypatch):
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    before = tree_bytes(source)
    job = enqueue(db)
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    assert result['id'] == job['id']
    assert result['status'] == 'waiting_for_approval', result
    assert result['provider'] == 'codex' and result['attempts'] == 1
    candidate = factory().store.get(result['candidate_id'])
    assert Path(candidate['worktree']).is_dir()
    assert candidate['improvement']['implementation_provider'] == 'codex'
    assert result['test_result'] and result['review_result']['verdict'] == 'passed'
    assert result['improvement']['baseline_evidence']['result'] == 'improved'
    assert {h['status'] for h in result['history']} >= {
        'queued', 'planning', 'implementing', 'testing', 'reviewing', 'waiting_for_approval'}
    assert not candidate.get('apply') and not candidate.get('deployment')
    assert tree_bytes(source) == before
    assert EngineeringWorker(factory(), source=source).once() is None
    db.close()
    assert EngineeringJobs(Database(db.path)).get(job['id'])['status'] == 'waiting_for_approval'


def test_lease_recovery_and_stale_owner_fenced(setup):
    _, db, _ = setup
    job = enqueue(db)
    jobs = EngineeringJobs(db)
    assert jobs.claim('old', now=0)['id'] == job['id']
    assert jobs.claim('duplicate', now=1) is None
    db.close()
    jobs = EngineeringJobs(Database(db.path))
    recovered = jobs.claim('new', now=931)
    assert recovered['attempts'] == 2
    with pytest.raises(ToolDenied):
        jobs.finish(job['id'], 'old')
    assert jobs.finish(job['id'], 'new')['status'] == 'blocked'
    assert 'lease_recovered' in [h['event'] for h in jobs.get(job['id'])['history']]


def test_flock_prevents_duplicate_even_with_expired_lease(setup, monkeypatch):
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    job = enqueue(db)
    EngineeringJobs(db).claim('old', now=0)
    with open(db.path + '.engineering.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert EngineeringWorker(factory(), source=source).once() is None
        assert EngineeringJobs(db).get(job['id'])['attempts'] == 1
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    assert result['status'] == 'waiting_for_approval'
    assert result['attempts'] == 2


def test_failure_retry_bounds_and_pause_cancel(setup, monkeypatch):
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    job = enqueue(db)
    jobs = EngineeringJobs(db)
    jobs.control(job['id'], 'pause')
    assert EngineeringWorker(factory(), source=source).once() is None
    jobs.control(job['id'], 'resume')
    def unavailable(*args):
        raise RuntimeError('PRIVATE_ERROR')
    worker = EngineeringWorker(factory(model=unavailable, tester=run_checks), source=source)
    for attempt in range(1, 4):
        result = worker.once()
        assert result['attempts'] == attempt
        assert result['status'] == ('failed' if attempt == 3 else 'blocked')
        if attempt < 3:
            jobs.control(job['id'], 'resume')
    assert len(result['candidate_ids']) == 3
    with pytest.raises(ToolDenied):
        jobs.control(job['id'], 'resume')
    other = enqueue(db)
    jobs.control(other['id'], 'cancel')
    with pytest.raises(ToolDenied):
        jobs.control(other['id'], 'resume')


@pytest.mark.parametrize('mode', ['production', ''])
def test_production_execution_denied(setup, monkeypatch, mode):
    source, db, factory = setup
    enqueue(db)
    monkeypatch.setenv('JARVIS_MODE', mode)
    with pytest.raises(ToolDenied):
        EngineeringWorker(factory(), source=source).once()
    assert EngineeringJobs(db).recent()[0]['attempts'] == 0


@pytest.mark.parametrize('path', ['.env', 'core/engineering_jobs.py', 'core/engineering_worker.py',
                                 'core/engineering_tools.py', 'core/candidate_store.py',
                                 'core/engineering_providers.py', 'core/candidate_claude.py'])
def test_protected_paths_fail_before_queue(setup, path):
    _, db, _ = setup
    with pytest.raises(ToolDenied):
        enqueue(db, inspect_paths=[path])
    assert EngineeringJobs(db).recent() == []


def test_active_control_and_foreign_checkout_denied(setup, monkeypatch, tmp_path):
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    job = enqueue(db)
    jobs = EngineeringJobs(db)
    jobs.claim('owner')
    for operation in ('pause', 'cancel', 'resume'):
        with pytest.raises(ToolDenied):
            jobs.control(job['id'], operation)
    with pytest.raises(ToolDenied):
        EngineeringWorker(factory(), source=tmp_path).once()
    assert 'goal' not in project_job(job) and 'arguments' not in project_job(job)


def test_broker_queue_and_conversation_scope(setup, monkeypatch):
    from core.candidate_broker import Broker
    from test_candidate_bridge import request
    from test_engineering_action import bound
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    broker = Broker(factory(), source=source)
    raw = request('job_create', 'Fix the value', ARGS)
    raw['engineering_action'] = bound(raw).model_dump()
    job = broker.execute(raw)
    assert job['status'] == 'queued'
    assert broker.execute(raw) == job
    with db.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0
    read = request('job_inspect', 'Show status', {'job_id': job['id']})
    assert broker.execute(read) == job
    read['conversation_id'] = raw['conversation_id']
    assert broker.execute(read) == job


@pytest.mark.parametrize('status', ['completed', 'blocked', 'failed', 'waiting_for_approval'])
def test_production_read_only_inspection_states(setup, monkeypatch, status):
    from core.candidate_broker import Broker
    from core.candidate_bridge import CandidateClient
    from core.candidate_tools import CandidateTools
    from tools import ToolRegistry
    from test_candidate_bridge import request
    source, db, factory = setup
    monkeypatch.setenv('JARVIS_MODE', 'development')
    jobs = EngineeringJobs(db)
    job = enqueue(db)
    with db.transaction() as connection:
        record = jobs._get(connection, job['id'])
        record.update(status=status, current_stage=status, attempts=1,
            candidate_id=str(uuid4()), error='PRIVATE /host/secret', block_reason='PRIVATE',
            test_result=[dict(phase='full_python', returncode=0, stdout='PRIVATE')],
            review_result=dict(verdict='passed', summary='PRIVATE'))
        jobs._save(connection, record, 'execution_finished')
    before = jobs.get(job['id'])
    broker = Broker(factory(), source=source)
    def transport(client, action, arguments):
        with monkeypatch.context() as host:
            host.setenv('JARVIS_MODE', 'development')
            return project_job(broker.execute(request(action, client.goal, arguments) |
                client.context))
    monkeypatch.setattr(CandidateClient, 'invoke', transport)
    monkeypatch.setenv('JARVIS_MODE', 'production')
    tools = CandidateTools('What is the current status?', db, source_context=job['source'])
    registry = tools.bind(ToolRegistry())
    assert {'list_engineering_jobs', 'inspect_engineering_job'} <= registry.schemas().keys()
    assert {'pause_engineering_job', 'cancel_engineering_job', 'resume_engineering_job'} <= registry.schemas().keys()
    assert not {'approve_and_apply_candidate', 'approve_and_deploy_commit'} & registry.schemas().keys()
    result = registry._definitions['inspect_engineering_job'].handler(job_id=job['id']).jobs[0]
    assert result['status'] == status and result['attempts'] == 1
    assert result['candidate_id'] == before['candidate_id']
    assert result['test_result'][0]['returncode'] == 0
    assert result['review_result']['verdict'] == 'passed'
    assert result['lease'] == dict(active=False, expired=False)
    assert result['recovery']['count'] == 0
    assert 'PRIVATE' not in __import__('json').dumps(result)
    assert project_job(result) == result
    assert jobs.get(job['id']) == before
    tools.source_context = dict(job['source'], conversation_id=str(uuid4()))
    assert registry._definitions['inspect_engineering_job'].handler(job_id=job['id']).jobs[0] == result
    with pytest.raises(ToolDenied):
        registry._definitions['inspect_engineering_job'].handler(job_id=str(uuid4()))


@pytest.mark.parametrize('field,value', [('current_stage', '/private'), ('candidate_id', '/secret'),
    ('attempts', 'secret'), ('control', 'secret'), ('created_at', 'secret'),
    ('test_result', [dict(phase='secret', returncode=0)]),
    ('review_result', dict(verdict='secret'))])
def test_job_projection_invalid_metadata_fails_closed(setup, field, value):
    job = enqueue(setup[1])
    with pytest.raises(ToolDenied):
        project_job(job | {field: value})
