"""Owner candidate operations while the engineering lock is held by another process."""
from contextlib import contextmanager
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core import self_edit
from core.candidate_bridge import project
from core.candidate_broker import Broker
from core.candidate_tools import CandidateTools
from core.engineering_jobs import EngineeringJobs, project_deferred_operations
from core.engineering_worker import EngineeringWorker
from tools.policy import ToolDenied
from test_self_edit import setup
from test_engineering import model, run_checks
from test_engineering_jobs import enqueue
from test_candidate_bridge import request

HOLDER = '''import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('locked', flush=True)
sys.stdin.read() if sys.argv[2] == 'stdin' else time.sleep(float(sys.argv[2]))
'''


@contextmanager
def held_lock(database, release_after='stdin'):
    """A separate process holds the engineering flock, as the worker does."""
    holder = subprocess.Popen([sys.executable, '-c', HOLDER, database.path + '.engineering.lock', str(release_after)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == 'locked\n'
        yield holder
    finally:
        release(holder)


def release(holder):
    if holder.returncode is None:
        holder.stdin.close()
        holder.wait(timeout=10)


def linked_candidate(setup, author=model):
    source, db, factory = setup
    job = enqueue(db)
    result = EngineeringWorker(factory(model=author, tester=run_checks), source=source).once()
    assert result['id'] == job['id'] and result['candidate_id']
    return job, result['candidate_id']


def worker(setup):
    source, _, factory = setup
    return EngineeringWorker(factory(model=model, tester=run_checks), source=source)


def manual_tests(service, candidate_id):
    return len(service.store.get(candidate_id, internal=True).get('manual_operations', []))


def test_tests_request_is_queued_while_lock_is_held_and_the_worker_runs_it_once(setup, monkeypatch):
    _, db, factory = setup
    job, candidate_id = linked_candidate(setup)
    monkeypatch.setattr(self_edit, 'ENGINEERING_LOCK_WAIT_SECONDS', .3)
    service = factory(model=model, tester=run_checks)
    jobs = EngineeringJobs(db)
    before = service.store.get(candidate_id, internal=True)
    with held_lock(db):
        result = service.tests(candidate_id)
        [operation] = jobs.deferred(candidate_id)
        assert operation['status'] == 'queued' and operation['action'] == 'tests'
        assert result['deferred_operations'][-1]['id'] == operation['id']
        # Nothing ran: same evidence, candidate not claimed.
        current = service.store.get(candidate_id, internal=True)
        assert current['test_results'] == before['test_results'] and not current['busy']
        assert current.get('manual_operations', []) == before.get('manual_operations', [])
        projected = project(result)
        assert projected['deferred_operations'] == [dict(id=operation['id'], action='tests', status='queued', reason=None)]
        owner = CandidateTools('Rerun the candidate tests', db)
        owner.records[candidate_id] = projected
        text = owner.final_response('ignored')
        assert 'Your test rerun request is queued' in text and 'run automatically' in text
        # A repeated request joins the queued one.
        assert service.tests(candidate_id)['deferred_operations'][-1]['id'] == operation['id']
        assert len(jobs.deferred(candidate_id)) == 1
        # The worker cannot run (or drain) while the other process holds the lock.
        assert worker(setup).once() is None
        assert jobs.deferred(candidate_id)[0]['status'] == 'queued'
    worker(setup).once()
    [operation] = jobs.deferred(candidate_id)
    assert operation['status'] == 'completed' and operation['reason'] is None and operation['finished_at']
    assert manual_tests(service, candidate_id) == len(before.get('manual_operations', [])) + 1
    assert EngineeringJobs(db).get(job['id'])['manual_operations'] == \
        service.store.get(candidate_id, internal=True)['manual_operations']
    assert not service.store.get(candidate_id, internal=True)['busy']
    inspected = CandidateTools('Show candidate status', db, lambda database: factory())
    inspected.action('inspect', candidate_id)
    assert 'ran once the engineering worker was free' in inspected.final_response('ignored')
    worker(setup).once()
    assert manual_tests(service, candidate_id) == len(before.get('manual_operations', [])) + 1


def test_bounded_wait_covers_a_short_lock_hold_without_queueing(setup):
    _, db, factory = setup
    _, candidate_id = linked_candidate(setup)
    service = factory(model=model, tester=run_checks)
    count = manual_tests(service, candidate_id)
    with held_lock(db, release_after=1.0):
        started = time.monotonic()
        result = service.tests(candidate_id)
    assert time.monotonic() - started >= .5
    assert result['deferred_operations'] == [] and EngineeringJobs(db).deferred(candidate_id) == []
    assert manual_tests(service, candidate_id) == count + 1


def test_lock_freed_after_queueing_runs_the_request_inline(setup, monkeypatch):
    _, db, factory = setup
    _, candidate_id = linked_candidate(setup)
    monkeypatch.setattr(self_edit, 'ENGINEERING_LOCK_WAIT_SECONDS', .3)
    service = factory(model=model, tester=run_checks)
    count = manual_tests(service, candidate_id)
    original = EngineeringJobs.defer
    with held_lock(db) as holder:
        def defer_then_release(self, *args):
            operation = original(self, *args)
            release(holder)
            return operation
        monkeypatch.setattr(EngineeringJobs, 'defer', defer_then_release)
        result = service.tests(candidate_id)
    assert [(o['action'], o['status']) for o in result['deferred_operations']] == [('tests', 'completed')]
    assert manual_tests(service, candidate_id) == count + 1


def test_pause_is_rechecked_when_the_queued_request_runs_and_discard_still_queues(setup, monkeypatch):
    _, db, factory = setup
    def unavailable(*args):
        raise RuntimeError('provider down')
    job, candidate_id = linked_candidate(setup, author=unavailable)
    jobs = EngineeringJobs(db)
    assert jobs.get(job['id'])['status'] == 'blocked'
    monkeypatch.setattr(self_edit, 'ENGINEERING_LOCK_WAIT_SECONDS', .3)
    service = factory(model=model, tester=run_checks)
    count = manual_tests(service, candidate_id)
    with held_lock(db):
        assert service.tests(candidate_id)['deferred_operations'][-1]['status'] == 'queued'
    # Owner pauses the job once the lock is free; the queued run must honour it.
    jobs.control(job['id'], 'pause')
    worker(setup).once()
    [operation] = jobs.deferred(candidate_id)
    assert (operation['status'], operation['reason']) == ('not_run', 'job_controlled')
    assert manual_tests(service, candidate_id) == count
    owner = CandidateTools('Show candidate status', db)
    owner.records[candidate_id] = project(jobs.with_deferred(service.store.get(candidate_id)))
    assert 'did not run: its engineering job is paused or cancelled' in owner.final_response('ignored')
    worktree = Path(service.store.get(candidate_id, internal=True)['worktree'])
    assert worktree.is_dir()
    with held_lock(db):
        # Paused while held: control cannot change until the lock is free, so refuse now.
        with pytest.raises(ToolDenied, match='paused or cancelled'):
            service.tests(candidate_id)
        assert len(jobs.deferred(candidate_id)) == 1
        # Discard is allowed on a paused job (as before) and queues.
        assert service.discard(candidate_id)['deferred_operations'][-1]['action'] == 'discard'
        assert worktree.is_dir()
    worker(setup).once()
    assert [(o['action'], o['status']) for o in jobs.deferred(candidate_id)] == [
        ('tests', 'not_run'), ('discard', 'completed')]
    assert service.store.get(candidate_id)['state'] == 'discarded' and not worktree.exists()


def test_running_request_left_by_a_dead_holder_is_never_rerun(setup):
    _, db, factory = setup
    job, candidate_id = linked_candidate(setup)
    jobs = EngineeringJobs(db)
    service = factory(model=model, tester=run_checks)
    count = manual_tests(service, candidate_id)
    operation = jobs.defer(candidate_id, job['id'], 'tests')
    assert jobs.start_deferred(operation['id']) and not jobs.start_deferred(operation['id'])
    worker(setup).once()
    [row] = jobs.deferred(candidate_id)
    assert (row['status'], row['reason']) == ('not_run', 'interrupted')
    assert manual_tests(service, candidate_id) == count


def test_broker_request_is_queued_and_audited_while_lock_is_held(setup, monkeypatch):
    source, db, factory = setup
    _, candidate_id = linked_candidate(setup)
    monkeypatch.setattr(self_edit, 'ENGINEERING_LOCK_WAIT_SECONDS', .3)
    broker = Broker(factory(model=model, tester=run_checks), source=source)
    raw = request('tests', 'Rerun the candidate tests', dict(candidate_id=candidate_id))
    with held_lock(db):
        result = broker.execute(raw)
        assert [(o['action'], o['status']) for o in result['deferred_operations']] == [('tests', 'queued')]
        # A relayed duplicate of the same request returns without a second queue entry.
        broker.execute(raw)
        assert len(EngineeringJobs(db).deferred(candidate_id)) == 1
    record = broker.service.store.get(candidate_id, internal=True)
    assert [o['request_id'] for o in record['chat_operations']] == [raw['request_id']]
    worker(setup).once()
    inspected = broker.execute(request('inspect', 'Show candidate status', dict(candidate_id=candidate_id)))
    assert [(o['action'], o['status']) for o in inspected['deferred_operations']] == [('tests', 'completed')]


def test_deferred_projection_is_closed_and_queue_is_bounded(setup, monkeypatch):
    _, db, _ = setup
    valid = dict(id=str(uuid4()), action='tests', status='queued', reason=None, candidate_id='x')
    assert project_deferred_operations([valid]) == [dict(id=valid['id'], action='tests', status='queued', reason=None)]
    for change in (dict(action='apply'), dict(status='done'), dict(reason='free text'), dict(id='../x')):
        with pytest.raises(ToolDenied):
            project_deferred_operations([valid | change])
    jobs = EngineeringJobs(db)
    monkeypatch.setattr('core.engineering_jobs.DEFERRED_LIMIT', 2)
    jobs.defer(str(uuid4()), str(uuid4()), 'tests')
    jobs.defer(str(uuid4()), str(uuid4()), 'review')
    with pytest.raises(ToolDenied, match='queue is full'):
        jobs.defer(str(uuid4()), str(uuid4()), 'tests')
    with pytest.raises(ValueError):
        jobs.defer(str(uuid4()), str(uuid4()), 'apply')
