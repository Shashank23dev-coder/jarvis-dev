"""Engineering decisions, isolated implementation, revision and approval boundaries."""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("owner")

from core.agent import AgentRunner
from core.brain import BrainSettings, JarvisBrain
from core.candidate_bridge import project
from core.candidate_provider import EngineeringProviders, ProposalProvider
from core.candidate_tools import CandidateTools
from core.engineering_authority import EscalationRequired
from core.self_edit import REVIEW, Limits
from core.task_store import TaskStore
from models.provider import ProviderRegistry
from tools import CommandResult, RepositoryTools, ToolRegistry
from test_agent import call, final
from test_brain import ScriptedProvider
from test_self_edit import setup, tree_bytes, git, propose


ARGS = dict(inspect_paths=['src/app.py'], targeted_tests=['tests/test_app.py'],
            problem_detected='The value is incorrect.', hypothesis='Changing the value to 2 fixes the failing assertion.')


def run_checks(repository, phase, paths):
    value = repository.read_text('src/app.py')
    return CommandResult(('pytest',), 0 if value == 'value = 2\n' else 1, 'Assertion result')


def model(payload, instructions, timeout):
    if instructions == REVIEW:
        assert payload['candidate']['baseline_comparison']['result'] == 'improved'
        return dict(verdict='passed', summary='Fix verified independently.')
    return dict(edits=[dict(path='src/app.py', content='value = 2\n')])


@pytest.mark.parametrize('engineering', [False, True])
def test_proposal_cannot_replace_uninspected_existing_test(setup, engineering):
    source, _, factory = setup
    original = (source / 'tests/test_app.py').read_text()
    def author(*args):
        return dict(edits=[dict(path='tests/test_app.py', content='def test_fake(): pass\n')])
    service = factory(model=author, tester=run_checks)
    candidate = (service.engineer('Fix value', **ARGS) if engineering else
                 service.start('Fix value', ARGS['inspect_paths'], ARGS['targeted_tests']))
    assert candidate['state'] == 'rejected'
    assert candidate['error'] == 'Existing edit target was not inspected'
    assert candidate['failure']['code'] == 'candidate_policy_denied'
    assert Path(candidate['worktree'], 'tests/test_app.py').read_text() == original
    assert (source / 'tests/test_app.py').read_text() == original


@pytest.mark.parametrize('phase', ['tests', 'review'])
def test_unsuccessful_evidence_has_normalized_stage_and_provider(setup, phase):
    _, _, factory = setup
    count = 0
    def author(payload, instructions, timeout):
        nonlocal count
        if instructions == REVIEW:
            return dict(verdict='needs_revision', summary='Missing required behavior.')
        count += 1
        return dict(edits=[dict(path='src/app.py', content=f'value = 2\n# attempt {count}\n')])
    def checks(*args):
        return CommandResult(('pytest',), 1 if phase == 'tests' else 0, 'Assertion result')
    candidate = factory(model=author, tester=checks).engineer('Fix value', **ARGS)
    failure = candidate['failure']
    assert failure['code'] == ('tests_failed' if phase == 'tests' else 'review_failed')
    assert failure['stage'] == ('testing' if phase == 'tests' else 'reviewing')
    assert failure['provider'] == ('codex_cli' if phase == 'tests' else 'claude_cli')
    assert failure['retryable'] is True and failure['attempt'] == 1
    assert len(candidate['failures']) == 2
    assert candidate['failures'][0] == candidate['failures'][1]


def test_engineering_isolated_baseline_and_durable_metadata(setup):
    source, database, factory = setup
    before = tree_bytes(source)
    candidate = factory(model=model, tester=run_checks).engineer('Fix incorrect value', **ARGS)
    assert candidate['state'] == 'passed', candidate
    assert tree_bytes(source) == before
    metadata = candidate['improvement']
    assert metadata['problem_detected'] == ARGS['problem_detected']
    assert metadata['hypothesis'] == ARGS['hypothesis']
    assert metadata['files_changed'] == ['src/app.py']
    assert metadata['baseline_comparison']['baseline']['returncode'] == 1
    assert metadata['baseline_comparison']['candidate']['returncode'] == 0
    assert metadata['baseline_comparison']['result'] == 'improved'
    assert metadata['review_result']['verdict'] == 'passed'
    assert metadata['confidence'] == 'supported_by_tests_and_review'
    assert len(metadata['failures_revisions']) == 1
    assert len(metadata['tests_run']) == 2
    database.close()
    assert factory().inspect(candidate['id'])['improvement'] == metadata
    projected = project(candidate)
    assert project(projected) == projected
    assert 'src/app.py' not in json.dumps(projected)
    assert 'incorrect value' not in json.dumps(projected)
    assert projected['improvement']['baseline_comparison']['result'] == 'improved'


@pytest.mark.parametrize('failure', ['tests', 'review'])
def test_failed_candidate_is_revised_with_evidence(setup, failure):
    source, _, factory = setup
    before = tree_bytes(source)
    attempts = []
    reviews = []
    def author(payload, instructions, timeout):
        if instructions == REVIEW:
            reviews.append(payload)
            return dict(verdict='needs_revision' if failure == 'review' and len(reviews) == 1 else 'passed',
                        summary='Review of current diff.')
        attempts.append(payload)
        if len(attempts) == 2:
            assert payload['tests'] and payload['diff']
            if failure == 'review':
                assert payload['review']['verdict'] == 'needs_revision'
        value = 3 if failure == 'tests' and len(attempts) == 1 else 2
        content = f'value = {value}\n'
        if failure == 'review' and len(attempts) == 2:
            content += '# Corrected constant documented after review.\n'
        return dict(edits=[dict(path='src/app.py', content=content)])
    def checks(repository, phase, paths):
        value = repository.read_text('src/app.py').splitlines()[0]
        return CommandResult(('pytest',), 0 if value == 'value = 2' else 1, 'Assertion result')
    candidate = factory(model=author, tester=checks).engineer('Fix the value', **ARGS)
    assert candidate['state'] == 'passed', candidate
    assert candidate['failure'] is None
    assert candidate['failures'][0]['code'] == ('tests_failed' if failure == 'tests' else 'review_failed')
    assert project(project(candidate))['failures'][0]['code'] == candidate['failures'][0]['code']
    assert candidate['iterations'] <= 4 and candidate['tool_calls'] <= 64
    attempts = candidate['improvement']['failures_revisions']
    assert [item['state'] for item in attempts] == ['needs_revision', 'passed']
    assert tree_bytes(source) == before


@pytest.mark.parametrize('always_noop', [False, True])
def test_noop_proposals_receive_bounded_revision_and_durable_evidence(setup, always_noop):
    source, _, factory = setup
    calls = []
    original = (source / 'src/app.py').read_text()
    def author(payload, instructions, timeout):
        if instructions == REVIEW:
            return dict(verdict='passed', summary='Reviewed current change')
        calls.append(payload)
        if len(calls) == 2:
            assert payload['previous_failure']['code'] == 'provider_no_changes'
        return dict(edits=[dict(path='src/app.py', content=original if len(calls) == 1 or always_noop else 'value = 2\n')])
    candidate = factory(model=author, tester=run_checks).engineer('Fix value', **ARGS)
    results = candidate['provider_results']
    assert results[0]['status'] == 'no_changes'
    assert len(results) <= 4
    assert candidate['state'] == ('needs_revision' if always_noop else 'passed')
    if always_noop:
        assert candidate['failure']['code'] == 'provider_no_changes'
        assert candidate['failure']['retryable'] is True
        assert candidate['iterations'] == 2
    else:
        assert candidate['failure'] is None
        assert results[1]['status'] == 'applied'
        assert results[-1]['provider'] == 'claude_cli'
    # The closed projection keeps every recorded field and fills route fields
    # (absent without a provider route) with None.
    projected = project(candidate)['provider_results']
    assert [{k: p[k] for k in r} for p, r in zip(projected, results)] == results
    assert len(projected) == len(results)
    assert all(p[k] is None for p, r in zip(projected, results) for k in p if k not in r)


@pytest.mark.parametrize('repeat_failed_proposal', [False, True])
def test_revisions_keep_inspection_redacted_and_detect_unchanged_failed_diff(setup, repeat_failed_proposal):
    source, _, factory = setup
    (source / 'tests/test_fixture.py').write_text("value = dict(OPENAI_API_KEY='synthetic-provider-fixture')\n")
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Synthetic inspection fixture')
    calls = []
    def author(payload, instructions, timeout):
        assert 'synthetic-provider-fixture' not in json.dumps(payload)
        if instructions == REVIEW:
            return dict(verdict='passed', summary='Reviewed')
        assert 'OMITTED' in payload['files']['tests/test_fixture.py']
        calls.append(payload)
        value = 3 if len(calls) == 1 or repeat_failed_proposal else 2
        return dict(edits=[dict(path='src/app.py', content=f'value = {value}\n')])
    candidate = factory(model=author, tester=run_checks).engineer('Fix value',
        **(ARGS | {'inspect_paths': ['src/app.py', 'tests/test_fixture.py']}))
    assert sum(r['stage'] == 'implementing' for r in candidate['provider_results']) == 2
    assert candidate['state'] == ('needs_revision' if repeat_failed_proposal else 'passed')
    if repeat_failed_proposal:
        assert candidate['failure']['code'] == 'provider_no_changes'
        assert len(candidate['test_results']) == 1
        assert candidate['provider_results'][-1]['status'] == 'no_changes'


@pytest.mark.parametrize('kind', ['protected', 'provider', 'review_rejected', 'baseline_timeout', 'baseline_error', 'baseline_truncated'])
def test_engineering_failures_never_earn_approval(setup, kind):
    source, _, factory = setup
    before = tree_bytes(source)
    def author(payload, instructions, timeout):
        if kind == 'provider':
            raise RuntimeError('PRIVATE_PROVIDER_EXCEPTION')
        if kind == 'protected':
            return dict(edits=[dict(path='core/candidate_provider.py', content='bypass = True\n')])
        if instructions == REVIEW and kind == 'review_rejected':
            return dict(verdict='rejected', summary='Unsafe fix')
        return model(payload, instructions, timeout)
    def tests(repository, phase, paths):
        if phase == 'baseline' and kind.startswith('baseline_'):
            return CommandResult(('pytest',), 2 if kind == 'baseline_error' else 0, '',
                                 timed_out=kind == 'baseline_timeout', truncated=kind == 'baseline_truncated')
        return run_checks(repository, phase, paths)
    candidate = factory(model=author, tester=tests).engineer('Fix the value', **ARGS)
    assert candidate['state'] != 'passed'
    assert candidate['improvement']['confidence'] == 'insufficient'
    assert 'PRIVATE_PROVIDER_EXCEPTION' not in json.dumps(candidate)
    assert tree_bytes(source) == before
    if kind.startswith('baseline_'):
        assert candidate['failure']['code'] == ('baseline_timeout' if kind == 'baseline_timeout' else 'baseline_failed')
        assert candidate['failure']['stage'] == 'testing'
        assert candidate['failures'] == [candidate['failure']]
        assert candidate['improvement']['failures_revisions'] == []
        assert candidate['improvement']['baseline_outcome']['provider_invoked'] is False


def test_engineering_budget_and_shared_one_candidate_limit(setup):
    _, database, factory = setup
    service = factory(model=model, tester=run_checks, limits=Limits(iterations=1))
    candidate = service.engineer('Fix the value', **ARGS)
    assert candidate['state'] != 'passed' and candidate['iterations'] == 0
    from uuid import uuid4
    tools = CandidateTools('Fix the value in Jarvis', database, lambda db: factory(model=model, tester=run_checks),
        source_context=dict(task_id=str(uuid4()), conversation_id=str(uuid4())))
    tools.prepare('codex')
    tools.engineer(**ARGS)
    from tools import ToolDenied
    with pytest.raises(ToolDenied):
        tools.create(ARGS['inspect_paths'], ARGS['targeted_tests'], project_id='jarvis-core')


def test_brain_decides_to_invoke_engineering_and_reports_authoritative_evidence(setup):
    source, database, factory = setup
    before = tree_bytes(source)
    store = TaskStore(database.path)
    task = store.create_task('Diagnose and fix the incorrect value in Jarvis')
    provider = ScriptedProvider(call('prepare_engineering', {'provider': 'codex'}),
                                final('Prepared. Ask me again to implement.'),
                                call('engineer_candidate', dict(ARGS, provider='codex'), 'implementation'), final('Deployed!'))
    runner = AgentRunner(ToolRegistry(RepositoryTools(source)),
                        candidate_service_factory=lambda db: factory(model=model, tester=run_checks))
    brain = JarvisBrain(runner, settings=BrainSettings(True, 'local'), providers=ProviderRegistry([provider]))
    outcome = brain.run(task['goal'], task_id=task['id'], task_store=store)
    assert 'queued' in outcome.response and 'Deployed!' not in outcome.response
    assert 'Queued for isolated development, tests and independent review.' in outcome.response
    assert 'Consequential promotion and production deployment require approval.' in outcome.response
    from core.engineering_jobs import EngineeringJobs
    from core.engineering_worker import EngineeringWorker
    job = EngineeringJobs(store.database).recent()[0]
    assert job['id'] in outcome.response
    result = EngineeringWorker(factory(model=model, tester=run_checks), source=source).once()
    assert result['status'] == 'completed'
    candidate = factory().inspect(result['candidate_id'])
    assert candidate['state'] == 'passed'
    with database.transaction() as db:
        assert all(json.loads(row['record']).get('apply') is None
                   for row in db.execute('SELECT record FROM candidates'))
    assert tree_bytes(source) == before
    names = {tool['name'] for tool in provider.requests[0].tools}
    assert {'prepare_engineering', 'engineer_candidate', 'create_engineering_job'} <= names
    assert 'engineer_candidate' in {tool['name'] for tool in provider.requests[2].tools}
    assert 'Do not ask for another user request' in provider.requests[2].input[-1]['content']
    # Apply exposure follows the routine-apply contract; authority-path applies still escalate.
    assert all('approve_and_deploy_commit' not in {tool['name'] for tool in request.tools}
               for request in provider.requests)


@pytest.mark.parametrize('goal', ['Hello', 'Explain the bug', 'Fix the bug but only plan',
                                'Diagnose the problem without changes', 'Do not edit anything'])
def test_isolated_engineering_available_regardless_of_goal_with_promotion_gated(setup, goal):
    source, database, factory = setup
    before = tree_bytes(source)
    def author(payload, instructions, timeout):
        result = propose(payload, instructions, timeout)
        if instructions != REVIEW:
            result['edits'].append(dict(path='src/permissions.py', content='can_edit = True\n'))
        return result
    tools = CandidateTools(goal, database, lambda db: factory(model=author), engineering=True)
    assert {'create', 'engineer'} <= tools.allowed
    names = {tool['name'] for tool in tools.bind(ToolRegistry()).model_tools()}
    assert {'create_self_edit_candidate', 'engineer_candidate', 'create_engineering_job'} <= names
    assert 'approve_and_deploy_commit' not in names
    candidate = tools.create(ARGS['inspect_paths'], ARGS['targeted_tests'], project_id='jarvis-core')
    assert candidate.state == 'passed'
    assert 'src/permissions.py' in json.loads(candidate.evidence)['files_changed']
    with pytest.raises(EscalationRequired) as caught:
        tools.goal = 'Apply candidate ' + candidate.candidate_id
        tools.apply(candidate.candidate_id)
    assert caught.value.decision.escalation == ['authority_expansion']
    assert tree_bytes(source) == before


def test_separate_provider_contract_and_role_routing():
    calls = []
    def transport(payload, instructions, timeout, *, provider):
        calls.append(provider)
        return {'result': provider}
    providers = EngineeringProviders.defaults(transport)
    assert providers.implementer.complete({}, '', 1) == {'result': 'codex'}
    assert providers.reviewer.complete({}, '', 1) == {'result': 'openai'}
    assert calls == ['codex', 'openai']
    with pytest.raises(ValueError):
        EngineeringProviders(providers.implementer, providers.implementer)
    # A later host-installed Claude reviewer can use the same contract.
    claude = ProposalProvider('claude_code', transport)
    assert EngineeringProviders(providers.implementer, claude).reviewer is claude


def test_engineering_broker_rechecks_authority_and_is_idempotent(setup):
    from core.candidate_broker import Broker
    from test_candidate_bridge import request
    from tools import ToolDenied
    source, _, factory = setup
    broker = Broker(factory(model=model, tester=run_checks), source=source)
    raw = request('engineer', 'Diagnose and fix the value in Jarvis', ARGS)
    record = broker.execute(raw)
    assert record['state'] == 'passed'
    assert broker.execute(raw) == record
    unauthenticated = request('engineer', 'Fix the value', ARGS)
    unauthenticated.pop('authority')
    with pytest.raises(ValueError):
        broker.execute(unauthenticated)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        broker.execute(request('engineer', 'Fix the value', dict(ARGS, provider='claude_code')))
    assert 'diff' not in record and 'worktree' not in record


def test_production_brain_uses_bridge_without_local_authority(setup, tmp_path, monkeypatch):
    from core.candidate_bridge import CandidateClient
    from core.candidate_broker import Broker
    from test_candidate_bridge import request
    source, _, factory = setup
    broker = Broker(factory(model=model, tester=run_checks), source=source)
    def invoke(client, action, arguments):
        raw = request(action, client.goal, arguments)
        raw.update(client.context)
        raw['engineering_action'] = client.engineering_action.model_dump() if client.engineering_action else None
        with monkeypatch.context() as host:
            host.setenv('JARVIS_MODE', 'development')
            return broker.execute(raw)
    monkeypatch.setattr(CandidateClient, 'invoke', invoke)
    monkeypatch.setenv('JARVIS_MODE', 'production')
    store = TaskStore(tmp_path / 'production.db')
    task = store.create_task('Fix the value in Jarvis')
    provider = ScriptedProvider(call('prepare_engineering', {'provider': 'codex'}),
                                call('engineer_candidate', dict(ARGS, provider='codex'), 'implementation'), final('Applied!'))
    brain = JarvisBrain(AgentRunner(candidate_service_factory=lambda db: pytest.fail('No local writes')),
                       settings=BrainSettings(True, 'local'), providers=ProviderRegistry([provider]))
    outcome = brain.run(task['goal'], task_id=task['id'], task_store=store)
    assert 'queued' in outcome.response and 'Applied!' not in outcome.response
    assert 'src/app.py' not in outcome.response
    with store.database.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0


def test_codex_worker_pins_model_and_disables_tools(monkeypatch, capsys):
    import io
    from core import candidate_model
    from models.provider import ProviderTurn
    calls = []
    monkeypatch.setattr(candidate_model.sys, 'stdin', io.StringIO(json.dumps(
        dict(input={'goal': 'Fix a bug'}, instructions='Return edits', provider='codex'))))
    def complete(**kwargs):
        calls.append(kwargs)
        return ProviderTurn([], '{"edits":[]}')
    # candidate_model imports the API client lazily so subscription CLI paths never load it.
    monkeypatch.setattr('models.openai_client.request_agent_turn', complete)
    candidate_model.main()
    assert calls[0]['model'] == 'gpt-5.3-codex'
    assert calls[0]['tools'] == [] and calls[0]['allow_tools'] is False
    assert json.loads(capsys.readouterr().out) == {'ok': True, 'text': '{"edits":[]}'}
    monkeypatch.setattr(candidate_model.sys, 'stdin', io.StringIO(json.dumps(
        dict(input={}, instructions='', provider='uninstalled'))))
    with pytest.raises(ValueError):
        candidate_model.main()
    assert len(calls) == 1


def test_changed_tests_are_not_claimed_as_baseline_improvement(setup):
    _, _, factory = setup
    def author(payload, instructions, timeout):
        if instructions == REVIEW:
            assert payload['candidate']['baseline_comparison']['result'] == 'changed_test_suite'
            return dict(verdict='passed', summary='Review includes test changes.')
        return dict(edits=[dict(path='src/app.py', content='value = 2\n'),
                          dict(path='tests/test_added.py', content='def test_extra(): assert 1 + 1 == 2\n')])
    candidate = factory(model=author, tester=run_checks).engineer('Fix value and add test', **ARGS)
    assert candidate['state'] == 'passed', candidate
    assert candidate['improvement']['baseline_comparison']['result'] == 'changed_test_suite'


def test_rerun_and_tamper_invalidate_improvement_review(setup):
    _, _, factory = setup
    service = factory(model=model, tester=run_checks)
    candidate = service.engineer('Fix the value', **ARGS)
    tested = service.tests(candidate['id'])
    assert tested['state'] == 'needs_revision'
    assert tested['improvement']['review_result'] is None
    assert tested['improvement']['confidence'] == 'insufficient'
    reviewed = service.review(candidate['id'])
    assert reviewed['state'] == 'passed'
    Path(reviewed['worktree'], 'src/app.py').write_text('value = 3\n')
    inspected = service.inspect(candidate['id'])
    assert inspected['state'] == 'needs_revision'
    assert inspected['improvement']['review_result'] is None
    assert inspected['improvement']['confidence'] == 'insufficient'


def test_review_gate_failure_is_distinct_from_passing_tests(setup):
    _, _, factory = setup
    service = factory(model=model, tester=run_checks)
    candidate = service.engineer('Fix value', **ARGS)
    record = service.store.get(candidate['id'], internal=True)
    record['improvement']['baseline_comparison']['baseline']['timed_out'] = True
    service.store.save(record)
    reviewed = service.review(candidate['id'])
    assert reviewed['state'] == 'needs_revision'
    assert reviewed['tested_fingerprint'] == reviewed['fingerprint']
    assert reviewed['failure']['code'] == 'review_failed'
    assert reviewed['failure']['stage'] == 'reviewing'
    assert reviewed['reviewer_result'] is None


@pytest.mark.skipif(os.environ.get('JARVIS_DOCKER_TESTS') != '1', reason='Opt-in Docker integration')
def test_engineering_real_sandbox_baseline_and_candidate(setup):
    source, _, factory = setup
    before = tree_bytes(source)
    candidate = factory(model=model, tester=None).engineer('Fix the value', **ARGS)
    assert candidate['state'] == 'passed', candidate
    assert candidate['improvement']['baseline_comparison']['baseline']['sandbox']['backend'] == 'docker'
    assert all(item['sandbox']['backend'] == 'docker' for item in candidate['test_results'])
    assert tree_bytes(source) == before
