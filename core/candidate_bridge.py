"""Bounded production mailbox. No host paths, credentials or commands cross it."""
import json
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

from pydantic import Field
from tools.schemas import StrictModel
from tools.policy import ToolDenied
from .candidate_tools import CreateArguments, CandidateArguments, EngineeringArguments
from .database import valid_id

ROOT = Path('/tmp/jarvis-candidate-mailbox')
MAX_BYTES = 48000


class Request(StrictModel):
    request_id: str
    task_id: str
    conversation_id: str
    goal: str = Field(min_length=1, max_length=32000)
    action: str
    arguments: dict
    created: float

    def checked(self):
        if not all(valid_id(v) for v in (self.request_id, self.task_id, self.conversation_id)):
            raise ToolDenied('Invalid request identity')
        if not 0 <= time.time() - self.created <= 330:
            raise ToolDenied('Expired request')
        schema = EngineeringArguments if self.action == 'engineer' else CreateArguments if self.action == 'create' else CandidateArguments
        if self.action not in {'create', 'engineer', 'inspect', 'tests', 'review', 'discard'}:
            raise ToolDenied('Unsupported candidate operation')
        schema.model_validate(self.arguments)
        return self


def root():
    ROOT.mkdir(mode=0o700, exist_ok=True)
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or ROOT.resolve() != ROOT or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ToolDenied('Unsafe mailbox')
    return ROOT


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ToolDenied('Unsafe mailbox entry')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ToolDenied('Mailbox size exceeded')
    return json.loads(data)


def write(path, data):
    encoded = json.dumps(data, allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        raise ToolDenied('Mailbox size exceeded')
    temp = path.parent / (str(uuid4()) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class CandidateClient:
    def __init__(self, goal, context):
        self.goal, self.context = goal, context

    def invoke(self, action, arguments):
        if not self.context:
            raise ToolDenied('Source task metadata required')
        request = Request(request_id=str(uuid4()), goal=self.goal, action=action,
                          arguments=arguments, created=time.time(), **self.context).checked()
        directory = root()
        # Bound concurrent waiting callers and refuse rather than evicting work.
        if len(list(directory.iterdir())) >= 16:
            raise ToolDenied('Candidate mailbox is full')
        incoming = directory / (request.request_id + '.request')
        outgoing = directory / (request.request_id + '.response')
        write(incoming, request.model_dump())
        try:
            deadline = time.monotonic() + 310
            while time.monotonic() < deadline:
                if outgoing.exists():
                    result = read(outgoing)
                    if result.get('request_id') != request.request_id or not result.get('ok'):
                        raise ToolDenied('Candidate broker denied the request')
                    return project(result['record'])
                time.sleep(.2)
            raise ToolDenied('Candidate broker unavailable or timed out; inspect before retrying')
        finally:
            incoming.unlink(missing_ok=True)
            outgoing.unlink(missing_ok=True)

    def start(self, goal, inspect_paths, targeted_tests):
        if goal != self.goal:
            raise ToolDenied('Goal mismatch')
        return self.invoke('create', dict(inspect_paths=inspect_paths, targeted_tests=targeted_tests))

    def inspect(self, candidate_id):
        return self.invoke('inspect', dict(candidate_id=candidate_id))

    def engineer(self, goal, inspect_paths, targeted_tests, problem_detected, hypothesis):
        if goal != self.goal:
            raise ToolDenied('Goal mismatch')
        return self.invoke('engineer', dict(inspect_paths=inspect_paths, targeted_tests=targeted_tests,
                                          problem_detected=problem_detected, hypothesis=hypothesis))

    def tests(self, candidate_id):
        return self.invoke('tests', dict(candidate_id=candidate_id))

    def review(self, candidate_id):
        return self.invoke('review', dict(candidate_id=candidate_id))

    def discard(self, candidate_id):
        return self.invoke('discard', dict(candidate_id=candidate_id))


def project(record):
    """Only structured status reaches production: no diff, logs, paths or prose."""
    if not valid_id(record['id']) or record['state'] not in {'created', 'passed', 'needs_revision', 'rejected', 'discarded'}:
        raise ToolDenied('Invalid candidate result')
    tests = []
    for item in record.get('test_results', [])[-3:]:
        if item.get('phase') not in {'targeted', 'full_python', 'full_ui'} or type(item.get('returncode')) is not int:
            raise ToolDenied('Invalid test result')
        tests.append(dict(phase=item['phase'], returncode=item['returncode'],
                          timed_out=bool(item.get('timed_out')), truncated=bool(item.get('truncated'))))
    verdict = (record.get('reviewer_result') or {}).get('verdict')
    if verdict not in {None, 'passed', 'needs_revision', 'rejected'}:
        raise ToolDenied('Invalid review result')
    summary = record.get('diff_summary') or {}
    summary = {k: v for k, v in summary.items() if k in {'files', 'added', 'deleted'} and type(v) is int and 0 <= v <= 400}
    projected = dict(id=record['id'], state=record['state'], busy=bool(record.get('busy')),
                diff_summary=summary, files_changed=[], test_results=tests,
                reviewer_result=dict(verdict=verdict, summary='Host review completed; detailed evidence remains in dev.') if verdict else None,
                error='Candidate operation did not complete successfully.' if record.get('error') else None)
    if record.get('improvement'):
        improvement = record['improvement']
        comparison = improvement.get('baseline_comparison', {}).get('result')
        if comparison not in {'not_run', 'not_compared', 'inconclusive', 'regressed', 'improved', 'unchanged_pass', 'unchanged_failure', 'changed_test_suite'}:
            raise ToolDenied('Invalid baseline comparison')
        projected['improvement'] = dict(
            problem_detected='Detailed problem evidence remains in dev.',
            hypothesis='Detailed hypothesis remains in dev.',
            files_changed=summary.get('files', 0), tests_run=tests,
            failures_revisions=[{'attempt': item['attempt'], 'state': item['state']}
                for item in improvement.get('failures_revisions', [])[:2]
                if type(item.get('attempt')) is int and item['attempt'] in (1, 2)
                and item.get('state') in {'passed', 'needs_revision', 'rejected'}],
            baseline_comparison={'result': comparison}, review_result=verdict,
            confidence='supported_by_tests_and_review' if record['state'] == 'passed' and not record.get('busy') else 'insufficient',
            implementation_provider='codex', review_provider='openai')
    return projected


def transfer(operation, payload=None):
    """Trusted broker transport, run by docker exec; never reads application DB."""
    directory = root()
    if operation == 'receive':
        for path in sorted(directory.glob('*.request'))[:16]:
            try:
                request = Request.model_validate(read(path)).checked()
                if path.name != request.request_id + '.request':
                    continue
                return request.model_dump()
            except Exception:
                path.unlink(missing_ok=True)
        return None
    if operation == 'respond':
        identifier = payload['request_id']
        if not valid_id(identifier):
            raise ToolDenied('Invalid response identity')
        incoming = directory / (identifier + '.request')
        if incoming.exists():
            write(directory / (identifier + '.response'), payload)
            incoming.unlink(missing_ok=True)
        return None
    raise ToolDenied('Invalid transport operation')


if __name__ == '__main__':
    import sys
    data = json.loads(sys.stdin.buffer.read(MAX_BYTES + 1))
    print(json.dumps(transfer(**data)))
