"""Direct development: one agent edits an isolated worktree, a different agent reviews.

Isolation reuses SelfEditService._prepare(workspace_only=True): a private bare clone
and worktree of a clean, registered project baseline. Tests reuse the candidate
sandbox (_tests). Agent state and execution go through EngineeringRegistry; this
module only adds the role choice, recorded with a reason for every decision.
The legacy candidate/engineering paths are untouched.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from tools.policy import ToolDenied
from .candidate_provider import ProviderFailure
from .database import timestamp
from .dev_agents import ADAPTERS, FailureLog
from .engineering_providers import EngineeringRegistry, Registration
from .engineering_repository import development_service
from .self_edit import Limits, SelfEditService

DEFAULT_ROLES = {'implementing': 'codex_cli', 'reviewing': 'claude_cli'}
MAX_ATTEMPTS = 3  # the first attempt plus up to 2 revisions by the same author
UNUSABLE = {'unavailable', 'not_authenticated', 'quota_blocked', 'invocation_failed'}


class NoAgentAvailable(ProviderFailure):
    def __init__(self, stage, decision):
        self.decision = decision
        super().__init__(decision['default'], 'provider_unavailable')

    def __str__(self):
        reasons = ', '.join(f"{c['agent']}={c['reason']}" for c in self.decision['candidates'])
        return f"no usable {self.decision['stage']} agent: {reasons}"


def default_state_dir():
    return Path(os.getenv('XDG_STATE_HOME') or Path.home() / '.local/state') / 'jarvis-direct-dev'


class Router:
    """Chooses an agent per stage from registry state plus a live login probe."""

    def __init__(self, registry, *, roles=None, api_authorized=False, log=None):
        self.registry, self.api_authorized = registry, api_authorized
        self.roles = dict(DEFAULT_ROLES, **(roles or {}))
        self.decisions = []
        self.log = log or (lambda decision: None)

    def choose(self, stage, *, exclude=()):
        default = self.roles[stage]
        order = list(dict.fromkeys([default, *self.registry.registrations]))
        candidates, chosen = [], None
        for name in order:
            reason = self._unusable(name, stage, exclude)
            candidates.append(dict(agent=name, usable=reason is None, reason=reason or 'usable'))
            if reason is None:
                chosen = name
                break
        default_reason = next(c['reason'] for c in candidates if c['agent'] == default)
        decision = dict(at=timestamp(), stage=stage, default=default, chosen=chosen,
                        excluded=sorted(exclude), candidates=candidates,
                        reason=('default_usable' if chosen == default else
                                f'default_{default_reason}' if chosen else 'no_usable_agent'))
        self.decisions.append(decision)
        self.log(decision)
        if chosen is None:
            raise NoAgentAvailable(stage, decision)
        return chosen, decision

    def _unusable(self, name, stage, exclude):
        if name in exclude:
            return 'reviewer_must_differ_from_author' if stage == 'reviewing' else 'excluded'
        registration = self.registry.registrations.get(name)
        if registration is None:
            return 'not_registered'
        if registration.provider_type != 'cli' and not self.api_authorized:
            return 'api_billing_not_authorized'
        if stage not in registration.capabilities:
            return 'capability_mismatch'
        state = self.registry.state(name)
        if not registration.configured:
            return 'cli_not_installed'
        if state.current_usability in UNUSABLE:
            return f'recent_{state.current_usability}'
        probe = getattr(registration.adapter, 'probe', None)
        if probe:
            try:
                live = probe(10)
            except Exception as exc:
                failures = getattr(registration.adapter, 'failures', None)
                if failures:
                    failures.record(registration.adapter.run_id, f'{stage}.route_probe', name, exc)
                live = 'invocation_failed'
            if live != 'available':
                self.registry.observe(name, live, 'provider_failure', 'engineering_request',
                    'provider_authentication' if live == 'not_authenticated' else
                    'provider_invocation_failed' if live == 'invocation_failed' else 'provider_unavailable',
                    stage=stage)
                return f'probe_{live}'
        return None


class DirectDevelopment:
    def __init__(self, database, *, candidate_root, state_dir=None, agents=None, roles=None,
                 api_authorized=False, limits=None, tester=None):
        self.database, self.tester = database, tester
        self.candidate_root = Path(candidate_root)
        self.state_dir = Path(state_dir or default_state_dir())
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.failures = FailureLog(self.state_dir / 'failures.jsonl')
        self.agents, self.roles, self.api_authorized = agents, roles, api_authorized
        self.limits = limits or Limits(seconds=900)

    def _registry(self, run_id):
        agents = self.agents or [cls(self.state_dir / 'agent-bin', self.failures, run_id) for cls in ADAPTERS.values()]
        registrations = []
        for agent in agents:
            agent.failures, agent.run_id = self.failures, run_id
            executable = getattr(agent, 'executable', lambda: True)
            registrations.append(Registration(agent, getattr(agent, 'provider_type', 'cli'), bool(executable())))
        return EngineeringRegistry(self.database, registrations)

    def _append(self, name, value):
        descriptor = os.open(self.state_dir / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'a') as handle:
            handle.write(json.dumps(value) + '\n')

    def run(self, project_id, task, inspect_paths, targeted_tests, *, timeout=900):
        run_id = str(uuid4())
        registry = self._registry(run_id)
        router = Router(registry, roles=self.roles, api_authorized=self.api_authorized,
                        log=lambda d: self._append('routing.jsonl', dict(run_id=run_id, **d)))
        report = dict(run_id=run_id, project_id=project_id, task=task, started_at=timestamp(),
                      status='running', stage='prepare', routing=router.decisions)
        deadline = time.monotonic() + min(timeout, self.limits.seconds)
        try:
            service = development_service(
                SelfEditService(self.database, candidate_root=self.candidate_root, limits=self.limits,
                                tester=self.tester), project_id)
            report['source_root'] = str(service.source)
            record = service.store.create(task, inspect_paths, targeted_tests,
                                          source_context=dict(project_id=project_id, direct_dev_run=run_id))
            record.update(source_root=str(service.source), limits=asdict(self.limits), execution_origin='provider')
            service.record, service.deadline, service.originals = record, deadline, {}
            service._prepare(workspace_only=True)
            _, bare, worktree = service._paths(record)
            report.update(candidate_id=record['id'], worktree=str(worktree), base_commit=record['base_commit'],
                          branch=record['branch'], repository=str(bare), attempts=[])
            report['stage'] = 'implement'
            author, _ = router.choose('implementing')
            report['author'], feedback = author, None
            for number in range(1, MAX_ATTEMPTS + 1):
                attempt = dict(attempt=number, author=author)
                report['attempts'].append(attempt)
                report['stage'] = 'implement'
                implementation, used = registry.complete(
                    dict(action='implement', worktree=str(worktree), task=_revision(task, feedback)), '',
                    service._check(), stage='implementing', preferred=author, allowed=(author,),
                    validate=_implementation)
                attempt['implementation'] = implementation
                report.update(implementation=implementation, route_implement=registry.last_route)
                record['improvement'] = dict(implementation_provider=used, scope='code')

                report['stage'] = 'test'
                service._tests()
                tests = [dict(phase=t['phase'], paths=t['paths'], returncode=t['returncode'],
                              timed_out=t['timed_out'], backend=(t['sandbox'] or {}).get('backend'),
                              stdout_tail=t['stdout'][-1500:])
                         for t in record['test_results'] if t['fingerprint'] == record['fingerprint']]
                passed = record.get('tested_fingerprint') == record['fingerprint']
                attempt.update(tests=tests, tests_passed=passed, files_changed=record['files_changed'])
                report.update(diff=record['diff'], diff_summary=record['diff_summary'],
                              files_changed=record['files_changed'], tests=tests, tests_passed=passed)
                if not passed:
                    # As in _review: only a change with passing sandboxed tests is reviewed.
                    attempt['outcome'] = report['status'] = 'tests_failed'
                    feedback = 'The sandboxed tests failed:\n' + '\n'.join(
                        f"[{t['phase']} rc={t['returncode']}]\n{t['stdout_tail']}" for t in tests if t['returncode'])
                    continue

                report['stage'] = 'commit'
                commit = _commit(service, used, number)
                attempt['commit'] = report['commit'] = commit

                report['stage'] = 'review'
                reviewer, _ = router.choose('reviewing', exclude={used})
                review, reviewed_by = registry.complete(
                    dict(action='review', task=task, commit=commit, diff=record['diff'], tests=tests), '',
                    service._check(), stage='reviewing', preferred=reviewer, allowed=(reviewer,),
                    exclude={used}, validate=_review)
                if reviewed_by == used:  # defence in depth; Router and registry both exclude the author
                    raise ToolDenied('Reviewer must differ from author')
                if service._workgit('rev-parse', 'HEAD').strip() != commit or \
                        service._diff() != record['tested_fingerprint']:
                    raise ToolDenied('Worktree changed during review')
                record['reviewer_result'] = dict(review, provider=reviewed_by, commit=commit,
                                                 fingerprint=record['fingerprint'])
                service.store.save(record, 'direct_review_finished')
                attempt.update(reviewer=reviewed_by, review=review, outcome=review['verdict'])
                report.update(reviewer=reviewed_by, review=review, reviewed_commit=commit,
                              route_review=registry.last_route, status=review['verdict'])
                if review['verdict'] != 'needs_revision':
                    break
                feedback = 'An independent reviewer asked for revision:\n' + review['summary']
            report['attempts_exhausted'] = report['status'] in {'tests_failed', 'needs_revision'}
        except Exception as exc:
            entry = self.failures.record(run_id, report['stage'], getattr(exc, 'provider', None), exc)
            report.update(status='failed', failure=dict(stage=report['stage'], exception=entry['exception'],
                                                        message=entry['message'], code=entry['code']))
            if isinstance(exc, NoAgentAvailable):
                report['failure']['routing'] = exc.decision
            blocker = getattr(exc, 'blocker', None)
            if blocker and blocker['reasons']:
                # RoutingBlocked always says provider_unavailable; the observation has the cause.
                report['failure']['code'] = (blocker['reasons'][0].get('observation') or {}).get('code') or entry['code']
        return self._finish(report)

    def _finish(self, report):
        report.update(ended_at=timestamp(), agent_failures=self.failures.entries(report['run_id']))
        self._append('runs.jsonl', {k: v for k, v in report.items() if k != 'diff'})
        return report


def _revision(task, feedback):
    if feedback is None:
        return task
    return (f'{task}\n\nYour previous attempt is still in the worktree (committed if its tests passed). '
            f'Revise it to address this feedback:\n{feedback[:6000]}')


def _commit(service, agent, attempt):
    """Commit the tested change to the candidate's private task branch; hooks never run."""
    # Changed since base, plus anything a revision reverted since the previous commit.
    since_head = service._workgit('diff', '--name-only', '-z', 'HEAD').split('\0')
    files = sorted(set(service.record['files_changed']) | {name for name in since_head if name})
    service._workgit('--literal-pathspecs', 'add', '-A', '--', *files)
    if not service._workgit('diff', '--cached', '--name-only'):
        raise ToolDenied('Revision made no changes to commit')
    identity = ['-c', f'user.name={agent}', '-c', f'user.email={agent}@jarvis.invalid', '-c', 'commit.gpgsign=false']
    service._workgit(*identity, 'commit', '--no-verify', '-q', '-m',
                     f'{agent}: direct-dev attempt {attempt}\n\nCandidate {service.record["id"]}')
    commit = service._workgit('rev-parse', 'HEAD').strip()
    service.record['commit'] = commit
    service.store.save(service.record, 'direct_commit')
    return commit


def _implementation(value):
    if not isinstance(value, dict) or not isinstance(value.get('summary'), str):
        raise ValueError('Invalid implementation result')
    return value


def _review(value):
    if not isinstance(value, dict) or value.get('verdict') not in {'passed', 'needs_revision', 'rejected'}:
        raise ValueError('Invalid review result')
    return value
