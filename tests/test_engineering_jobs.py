"""Durable queue, execution fencing, provider path and approval invariants."""
import fcntl
import os
from pathlib import Path
from uuid import uuid4

import pytest

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
    recovered = jobs.claim('new', now=331)
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
                                 'core/engineering_tools.py', 'core/candidate_store.py'])
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
    with pytest.raises(ToolDenied):
        broker.execute(read)
    read['conversation_id'] = raw['conversation_id']
    assert broker.execute(read) == job
