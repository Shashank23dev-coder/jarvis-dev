"""Checkpointed coordination over the existing candidate worker and providers.

No apply, promotion, deployment, shell-tool or registration interface. Private
release preparation deliberately cannot produce controller-owned eligibility.
"""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Literal

from pydantic import Field, field_validator
from tools.schemas import StrictModel
from tools.policy import ToolDenied
from tools.candidate_edit import digest, inspection_text, safe_text, scan
from .database import timestamp

ExecutionMode = Literal['CODEX_ONLY', 'CLAUDE_ONLY', 'CODEX_BUILD_CLAUDE_REVIEW']
Phase = Literal['inspect', 'baseline', 'implement', 'apply', 'validate', 'review',
                'commit', 'readiness', 'done']


def routes(mode):
    return {'CODEX_ONLY': ('codex_cli', 'claude_cli'),
            'CLAUDE_ONLY': ('claude_cli', 'codex_cli'),
            'CODEX_BUILD_CLAUDE_REVIEW': ('codex_cli', 'claude_cli')}[mode]


class OperatorEvidence(StrictModel):
    mode: ExecutionMode
    phase: Phase
    revision: int = Field(ge=0, le=2)
    commit_sha: str | None = Field(default=None, pattern=r'^[0-9a-f]{40}$')
    ready: bool = False
    controller_validated: Literal[False] = False
    review_verdict: Literal['passed', 'needs_revision', 'rejected'] | None = None
    checkpoint_at: str | None = None

    @field_validator('checkpoint_at')
    @classmethod
    def timestamp_value(cls, value):
        if value is not None:
            from datetime import datetime
            datetime.fromisoformat(value)
        return value


def project_operator(value):
    if value is None:
        return None
    return OperatorEvidence.model_validate({k: value[k] for k in OperatorEvidence.model_fields
                                           if k in value}).model_dump()


def owner_status(job):
    op = project_operator(job.get('operator'))
    label = f"Engineering job {job['id']}"
    from .engineering_jobs import project_provider_route
    route = project_provider_route(job.get('provider_route'), job.get('provider'))
    provider_status = (f" Provider requested: {route['provider_requested']}; used: "
        f"{route['provider_used'] or 'none'}; adapter: {route['adapter_type'] or 'unreported'}; "
        f"billing: {route['billing_class'] or 'unreported'}; fallback: {route['fallback_state']}"
        + (f" ({route['fallback_reason']})." if route['fallback_reason'] else '.'))
    if not op:
        return f"{label}: {job['status']}.{provider_status}"
    if job.get('control'):
        state = 'cancelled' if job['control'] == 'cancel' else 'paused'
        return f'{label}: {state}. Completed work is retained.{provider_status}'
    if job['status'] in {'blocked', 'failed'}:
        from .candidate_provider import FAILURES
        code = (job.get('failure') or {}).get('code', 'worker_failed')
        return f"{label}: blocked at {op['phase']}. {FAILURES.get(code, FAILURES['worker_failed'])[1]} Completed work is retained.{provider_status}"
    if (op['ready'] and job['status'] in {'completed', 'waiting_for_approval'}
            and (job.get('review_result') or {}).get('verdict') == 'passed'):
        next_step = ('Ready for the existing promotion/approval path; production controller validation is still required.'
                     if job.get('project_id') == 'jarvis-core' else 'Ready for the existing candidate apply path.')
        return (f"{label}: review PASS; candidate {op['commit_sha']} is prepared. "
                + next_step + provider_status)
    return f"{label}: {op['mode']}, {op['phase']}. Review: {op['review_verdict'] or 'pending'}.{provider_status}"


def checked_findings(record, review):
    from .candidate_provider import ProviderFailure
    from tools.candidate_edit import CandidatePolicy
    if review.verdict == 'needs_revision' and not review.findings:
        raise ProviderFailure(routes(record['operator']['mode'])[1], 'provider_invalid_response')
    for finding in review.findings:
        CandidatePolicy().edit_path(finding.path)
        if finding.path not in set(record['inspect_paths']) | set(record['files_changed']):
            raise ProviderFailure(routes(record['operator']['mode'])[1], 'provider_invalid_response')
        safe_text(finding.problem)
        safe_text(finding.requested_change)


def checkpoint(service, phase, **values):
    op = service.record['operator']
    op.update(values, phase=phase, checkpoint_at=timestamp())
    service.store.save(service.record, 'operator_' + phase)


def step(service, phase, operation, *, provider=None):
    """A durable start and result; interrupted calls never count as success."""
    op = service.record['operator']
    model = None
    if provider == 'codex_api':
        model = 'gpt-5.3-codex'
    elif provider == 'openai':
        from models.openai_client import MODEL
        model = MODEL
    entry = dict(phase=phase, revision=op['revision'], provider=provider, model=model,
                 model_source='configured' if model else 'unreported' if provider else None,
                 started_at=timestamp(), ended_at=None, status='running',
                 attempt=service.record['source_context']['attempt'])
    op.setdefault('steps', []).append(entry)
    service.store.save(service.record, 'operator_step_started')
    try:
        result = operation()
    except BaseException as exc:
        entry.update(status='failed' if isinstance(exc, Exception) else 'interrupted', ended_at=timestamp())
        service.store.save(service.record, 'operator_step_interrupted')
        raise
    entry.update(status='completed', ended_at=timestamp(),
                 fingerprint=service.record.get('fingerprint'))
    # Provider output itself is held in the validated proposal/review checkpoint.
    if provider and service.record.get('provider_results'):
        entry['response_sha256'] = service.record['provider_results'][-1]['response_sha256']
        entry['route'] = service.record.get('provider_route')
    service.store.save(service.record, 'operator_step_completed')
    return result


def check_pending_edits(service):
    """Recover only known old/new bytes after a crash between atomic edits."""
    r, op = service.record, service.record['operator']
    current, _ = service._editor().validate()
    before = op['proposal_manifest']
    replacements = {e['path']: digest(e['content'].encode()) for e in op['proposal']['edits']}
    for path in set(current) | set(before):
        observed, original = current.get(path), before.get(path)
        if observed == original:
            continue
        mode = r['manifest'].get(path, [None, 0o644])[1]
        if observed != [replacements.get(path), mode] or path not in replacements:
            r['execution_origin'] = 'operator_assisted'
            raise ToolDenied('Operator worktree changed outside its pending edits')


def coordinate(s):
    """Runs inside the existing fenced, killable candidate subprocess."""
    from .self_edit import CandidateRepository, Proposal, PROPOSE
    from .candidate_provider import ProviderFailure
    r, op = s.record, s.record['operator']
    builder, reviewer = routes(op['mode'])
    r['improvement'].update(implementation_provider=builder, review_provider=reviewer)
    if op['phase'] == 'inspect' and not r.get('manifest'):
        # _prepare enforces a clean registered baseline and makes a private bare
        # clone/worktree, with no shared refs, objects, hooks or live checkout.
        step(s, 'inspect', lambda: s._prepare(workspace_only=True))
        checkpoint(s, 'baseline')
    elif r.get('manifest'):
        expected = r.get('fingerprint') or digest(json.dumps(r['manifest'], sort_keys=True).encode())
        actual = s._diff()
        if actual != expected and op['phase'] != 'apply':
            r.update(execution_origin='operator_assisted', tested_fingerprint=None, reviewer_result=None)
            raise ToolDenied('Operator worktree changed outside its checkpoint')
        if r.get('reviewed_fingerprint') == actual and (r.get('reviewer_result') or {}).get('verdict') == 'passed':
            r['state'] = 'passed'
        if op['phase'] == 'inspect':
            checkpoint(s, 'baseline')
    _, _, worktree = s._paths(r)
    if op['phase'] == 'baseline':
        def baseline():
            s._tick('baseline_tests')
            repo = CandidateRepository(worktree, source_root=s.source, check=s._check)
            result = s.tester(repo, 'baseline', r['targeted_tests']) if s.tester else repo.run_pytest(r['targeted_tests'])
            if scan(worktree, s._check) != r['manifest']:
                raise ToolDenied('Baseline changed during tests')
            if result.returncode not in (0, 1) or result.timed_out or result.truncated:
                raise ProviderFailure(builder, 'tests_failed')
            r['improvement']['baseline_comparison'] = dict(result='not_compared', baseline=dict(
                commit=r['base_commit'], paths=r['targeted_tests'], returncode=result.returncode,
                timed_out=False, truncated=False, sandbox=result.sandbox))
        if not r['improvement']['baseline_comparison'].get('baseline'):
            step(s, 'baseline', baseline)
        r['state'] = 'needs_revision'
        checkpoint(s, 'implement')
    while op['phase'] in {'implement', 'apply', 'validate', 'review'}:
        if op['phase'] == 'implement':
            if op['revision'] >= 2:
                raise ProviderFailure(builder, 'review_failed')
            context = {p: inspection_text(s._editor().reader.read_text(p))
                       for p in set(r['inspect_paths']) | set(r['files_changed'])}
            payload = dict(goal=r['goal'], files=context,
                           baseline=r['improvement']['baseline_comparison']['baseline'])
            if op.get('findings'):
                # Deliberately exclude reviewer summary and all dialogue/history.
                payload['findings'] = op['findings']
            if op.get('test_failure'):
                payload['tests'] = r['test_results'][-3:]
            def implement():
                proposal = s._ask(payload, PROPOSE, Proposal)
                s._validate_proposal_scope(proposal, context)
                for edit in proposal.edits:
                    safe_text(edit.content)
                current, _ = s._editor().validate()
                checkpoint(s, 'apply', proposal=proposal.model_dump(), revision=op['revision'] + 1,
                           proposal_manifest=current, proposal_base_fingerprint=digest(json.dumps(current, sort_keys=True).encode()))
            step(s, 'implement', implement, provider=builder)
        if op['phase'] == 'apply':
            proposal = Proposal.model_validate(op['proposal'])
            check_pending_edits(s)
            before = op['proposal_base_fingerprint']
            r.update(state='needs_revision', tested_fingerprint=None, reviewer_result=None, error=None, failure=None)
            s._tick('apply_edits')
            s._editor().apply([e.model_dump() for e in proposal.edits], s._original)
            s._diff()
            if not r['files_changed'] or before == r['fingerprint']:
                raise ProviderFailure(builder, 'provider_no_changes')
            r['provider_results'][-1]['status'] = 'applied'
            op.pop('proposal', None)
            op.pop('proposal_manifest', None)
            op.pop('proposal_base_fingerprint', None)
            checkpoint(s, 'validate', test_failure=False)
        if op['phase'] == 'validate':
            step(s, 'validate', lambda: s._tests(resume=True,
                 focused=r['source_context']['project_id'] == 'jarvis-core'))
            if r['tested_fingerprint'] != r['fingerprint']:
                if op['revision'] < 2:
                    checkpoint(s, 'implement', test_failure=True)
                    continue
                return
            checkpoint(s, 'review')
        if op['phase'] == 'review':
            # A crash after _review committed its result does not buy a re-review.
            if r.get('reviewed_fingerprint') != r['fingerprint'] or not r.get('reviewer_result'):
                step(s, 'review', s._review, provider=reviewer)
            review = r.get('reviewer_result')
            if not review:
                return
            op['review_verdict'] = review['verdict']
            if review['verdict'] == 'needs_revision':
                checkpoint(s, 'implement', findings=review['findings'])
                continue
            if review['verdict'] != 'passed':
                return
            checkpoint(s, 'commit')
    if op['phase'] == 'commit':
        def commit():
            if s._diff() != r['reviewed_fingerprint'] or r['tested_fingerprint'] != r['reviewed_fingerprint']:
                raise ToolDenied('Commit requires unchanged tested and reviewed bytes')
            head = s._workgit('rev-parse', 'HEAD').strip()
            if head == r['base_commit']:
                s._workgit('--literal-pathspecs', 'add', '--', *r['files_changed'])
                s._workgit('-c', 'user.name=Jarvis Engineering', '-c', 'user.email=jarvis@localhost',
                           '-c', 'commit.gpgsign=false', 'commit', '-m', 'Engineering candidate ' + r['id'])
                head = s._workgit('rev-parse', 'HEAD').strip()
            if (s._workgit('rev-parse', 'HEAD^').strip() != r['base_commit'] or
                    s._workgit('status', '--porcelain=v1', '--untracked-files=all') or
                    s._workgit('symbolic-ref', '--short', 'HEAD').strip() != r['branch']):
                raise ToolDenied('Candidate commit identity changed')
            return head
        sha = step(s, 'commit', commit)
        checkpoint(s, 'readiness', commit_sha=sha)


def run_job(service, jobs, job, owner):
    """Called only by EngineeringWorker while holding its inherited flock."""
    from .self_edit import Limits, SelfEditService, model_request
    from .engineering_portfolio import Portfolio
    from .engineering_providers import EngineeringRegistry
    if not hasattr(service, 'engineering_lock_fd'):
        raise ToolDenied('Operator requires the worker fence')
    project = Portfolio(jobs.database).inspect_project(job['project_id'])
    if not project:
        raise ToolDenied('Registered project unavailable')
    limits = service.limits if service.explicit_limits else Limits(seconds=900)
    r = service.store.get(job.get('candidate_id'), internal=True, recover=False)
    if r is None:
        args = job['arguments']
        context = dict(job['source'], source='engineering_job', engineering_job_id=job['id'],
                       lease_owner=owner, attempt=job['attempts'], project_id=job['project_id'])
        r = service.store.create(job['goal'], args['inspect_paths'], args['targeted_tests'], source_context=context)
    if not r.get('operator'):
        if r['state'] != 'created' or r.get('manifest') or r['source_context']['engineering_job_id'] != job['id']:
            raise ToolDenied('Incomplete operator candidate identity mismatch')
        args = job['arguments']
        r.update(source_root=str(service.source), limits=asdict(limits),
            execution_origin='provider' if service.model is model_request else 'operator_assisted',
            operator=dict(job['operator'], revision=0, ready=False, steps=[]),
            improvement=dict(problem_detected=args['problem_detected'], hypothesis=args['hypothesis'],
                scope=job['action']['scope'], implementation_provider=routes(job['operator']['mode'])[0],
                review_provider=routes(job['operator']['mode'])[1], failures_revisions=[],
                baseline_comparison={'result': 'not_run'}))
    elif r['source_root'] != str(service.source) or r['source_context']['engineering_job_id'] != job['id']:
        raise ToolDenied('Operator candidate identity mismatch')
    if r['state'] in {'discarded', 'rejected'}:
        return r
    r['source_context'].update(lease_owner=owner, attempt=job['attempts'])
    deployment = dict(available=False, reason='controller_observation_unavailable')
    if job['project_id'] == 'jarvis-core':
        from .deployment_status import configured_inspector
        try:
            deployment = dict(available=True, observation=configured_inspector().status())
        except Exception:
            pass
    r['operator'].setdefault('observations', []).append(dict(at=timestamp(), project=project,
        providers=EngineeringRegistry.defaults(jobs.database, model_request).snapshot(),
        runtime=dict(worker='active', observed_at=timestamp(), project_runtime='unproven',
                     notes=project.get('runtime_notes', []), deployment=deployment)))
    for entry in r['operator'].get('steps', []):
        if entry['status'] == 'running':
            entry.update(status='interrupted', ended_at=timestamp())
    # A bounded queue retry gets a fresh operation budget, retaining lifetime
    # evidence and completed checkpoints. Automatic retries still obey claim().
    r['operator']['elapsed_seconds'] = r['operator'].get('elapsed_seconds', 0) + r['elapsed_seconds']
    r.update(elapsed_seconds=0, iterations=0, tool_calls=0)
    service.store.save(r, 'operator_inspected')
    runner = SelfEditService(jobs.database, source_root=service.source, candidate_root=service.root,
                            limits=limits, model=service.model, tester=service.tester)
    runner.engineering_lock_fd = service.engineering_lock_fd
    runner._operate(r['id'], 'operator')
    r = runner.store.get(r['id'], internal=True, recover=False)
    if r['operator']['phase'] != 'readiness' or r['state'] != 'passed':
        return r
    # Inspect again under the same fence before trusting existing review/commit.
    if runner.inspect(r['id'])['state'] != 'passed':
        return runner.store.get(r['id'])
    r = runner.store.get(r['id'], internal=True, recover=False)
    runner.record = r
    from .candidate_provider import failure_evidence
    try:
        if job['project_id'] == 'jarvis-core':
            # Existing full release validation can take four 600 s batches. It
            # gets its own fixed subprocess deadline, never a candidate budget.
            with jobs.database.transaction() as db:
                current = jobs._get(db, job['id'])
                current.update(status='testing', current_stage='testing', lease_until=time.time() + 2460)
                jobs._save(db, current, 'operator_release_preparation')
            def prepare():
                result = prepare_release(runner, r)
                record_release_tests(r, result)
                return result
            result = step(runner, 'readiness', prepare)
        else:
            result = dict(scope='candidate_tests', prepared=True,
                          controller_validated=False, required_confirmation=None)
        inspected = runner.inspect(r['id'])
        # inspect() may invalidate evidence in storage. Never overwrite that
        # decision with the pre-inspection record held by this supervisor.
        r = runner.store.get(r['id'], internal=True, recover=False)
        runner.record = r
        if inspected['state'] != 'passed':
            r['operator']['ready'] = False
            r['operator']['review_verdict'] = None
            raise ValueError('Candidate changed during readiness')
        r['operator'].update(phase='done', ready=True, release=result, checkpoint_at=timestamp())
        r.update(failure=None, error=None)
    except Exception:
        r.update(failure=failure_evidence(job['provider'], 'tests_failed', 'testing', job['attempts']),
                 error='Release preparation requires attention.')
    runner._improvement_evidence()
    runner.store.save(r, 'operator_readiness_finished')
    return runner.store.get(r['id'])


def record_release_tests(record, result):
    """Full-test aggregates derive from the exact commit's existing validator."""
    sha = record['operator']['commit_sha']
    evidence = result['evidence']
    tests = evidence['tests']
    from datetime import datetime, timezone
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(evidence['at'])).total_seconds()
    if (result['commit'] != sha or evidence['commit'] != sha or result['prepared'] is not True
            or result['controller_validated'] is not False or result['required_confirmation'] is not None
            or not 0 <= age < 86400 or evidence['passed'] is not True
            or [t['shard'] for t in tests] != ['python-1', 'python-2', 'python-3', 'ui']
            or [t['phase'] for t in tests] != ['python', 'python', 'python', 'ui']
            or any(t['passed'] is not True or t['returncode'] != 0
                   or (t.get('sandbox') or {}).get('timed_out')
                   or (t.get('sandbox') or {}).get('truncated') for t in tests)):
        raise ValueError('Private preparation failed')
    for phase, paths, batches in [('full_python', ['tests'], tests[:3]),
                                 ('full_ui', ['tests/ui.test.cjs'], tests[3:])]:
        record['test_results'].append(dict(phase=phase, paths=paths, returncode=0, timed_out=False,
            truncated=False, stdout='', stderr='', fingerprint=record['reviewed_fingerprint'],
            sandbox=dict(backend='release_validator', commit=sha, at=evidence['at'],
                         batches=[t['shard'] for t in batches]), release_commit=sha))


def prepare_release(service, record):
    """No production configuration: validated private evidence survives crashes."""
    config = dict(source=record['worktree'], journal=str(service._paths(record)[0] / 'release-preparation'),
                  commit=record['operator']['commit_sha'])
    process = subprocess.Popen([sys.executable, '-B', '-m', 'core.engineering_operator'],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(service.engineering_lock_fd,))
    try:
        output, _ = process.communicate(json.dumps(config).encode(), timeout=2430)
        if process.returncode or len(output) > 64000:
            raise ValueError('Preparation worker failed')
        return json.loads(output)
    finally:
        if process.returncode is None:
            service._kill_worker(process)
        process.wait()
        process.stdin.close()
        process.stdout.close()


def main():
    from .deployment_release import ReleaseService, ReleaseConfig
    config = json.loads(sys.stdin.read(16000))
    journal = Path(config['journal'])
    service = ReleaseService(ReleaseConfig(config['source'], journal, journal / 'unused.env'))
    service.controller = False
    service.clean(config['commit'])
    print(json.dumps(service.prepare()))


if __name__ == '__main__':
    main()
