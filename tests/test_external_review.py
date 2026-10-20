"""CLAUDE_EXTERNAL_REVIEW is an owner server setting and passes only on grok's recorded verdict.

Reuses the operator harness: real Git/SQLite/worker, scripted providers, no paid calls.
"""
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core import engineering_operator as operator
from core import external_review
from core.candidate_apply import CandidateApply
from core.candidate_store import CandidateStore
from core.engineering_jobs import EngineeringJobs
from tools import ToolDenied
from test_engineering import ARGS
from test_self_edit import setup  # noqa: F401  (fixture used by harness)
from test_engineering_operator import allow_external, harness  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures('owner')

FLAG = 'JARVIS_ALLOW_CLAUDE_EXTERNAL_REVIEW'
REFUSED = 'Review evidence is incomplete or changed'


def test_setting_is_off_by_default_and_only_exact_values_enable(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert not operator.external_review_allowed()
    for value in ['', '0', 'false', 'True', 'TRUE', 'yes', ' 1', 'true ']:
        monkeypatch.setenv(FLAG, value)
        assert not operator.external_review_allowed(), value
    for value in ['1', 'true']:
        monkeypatch.setenv(FLAG, value)
        assert operator.external_review_allowed()


def test_setting_off_refuses_job_creation(harness, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    with pytest.raises(ToolDenied, match='not enabled'):
        harness['enqueue']('CLAUDE_EXTERNAL_REVIEW')
    assert EngineeringJobs(harness['db']).recent() == []
    assert harness['enqueue']('CLAUDE_ONLY')['status'] == 'queued'  # other modes unaffected


def test_job_created_while_on_cannot_run_after_turned_off(harness, monkeypatch):
    monkeypatch.setenv(FLAG, '1')
    queued = harness['enqueue']('CLAUDE_EXTERNAL_REVIEW')
    monkeypatch.delenv(FLAG)
    result = harness['worker']().once()
    assert result['id'] == queued['id'] and result['status'] in {'blocked', 'failed'}, result
    assert not result.get('operator', {}).get('ready')
    assert not harness['log'].exists()  # no provider call, test or release ran


def test_coordinate_rechecks_the_setting(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    service = SimpleNamespace(record=dict(operator=dict(mode='CLAUDE_EXTERNAL_REVIEW', phase='review')))
    with pytest.raises(ToolDenied, match='not enabled'):
        operator.coordinate(service)


def test_planner_cannot_enable_the_mode(harness, monkeypatch):
    from core.candidate_tools import (BrainEngineeringArguments, CandidateTools, EngineeringArguments,
                                      OperatorArguments)
    monkeypatch.delenv(FLAG, raising=False)
    for model in (BrainEngineeringArguments, OperatorArguments, EngineeringArguments):
        assert not [f for f in model.model_fields if re.search(r'allow|flag|enable|external|setting', f)]
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    tools = CandidateTools('Fix the value in Jarvis', harness['db'], source_context=context)
    assert 'Never infer it' not in tools.instructions and FLAG not in tools.instructions
    assert 'explicitly asks' not in tools.instructions
    for call in (tools.start_operator, tools.engineer):
        with pytest.raises(ToolDenied, match='not enabled'):
            call(**ARGS, project_id='jarvis-core', execution_mode='CLAUDE_EXTERNAL_REVIEW')
    # No argument can carry the setting; strict schemas refuse unknown fields.
    with pytest.raises((ToolDenied, ValueError)):
        harness['enqueue']('CLAUDE_EXTERNAL_REVIEW', allow_claude_external_review=True)
    assert EngineeringJobs(harness['db']).recent() == []
    assert not harness['log'].exists()


@pytest.fixture
def built(harness, allow_external, tmp_path):
    """A sealed CLAUDE_EXTERNAL_REVIEW candidate with a configured integration target."""
    from test_candidate_apply import integration_target
    integration_target((harness['source'], harness['db'], None), tmp_path)
    harness['enqueue']('CLAUDE_EXTERNAL_REVIEW')
    result = harness['worker']().once()
    assert result['status'] == 'waiting_for_approval' and result['operator']['ready'], result
    service = harness['worker']().service
    return service, result['candidate_id']


def refused(service, candidate_id):
    apply = CandidateApply(service)
    apply._setup(candidate_id)
    apply._target()
    with pytest.raises(ToolDenied, match=REFUSED):
        apply._eligible()
    assert not CandidateApply(service).preview(candidate_id)['eligible']


def test_apply_requires_recorded_passed_verdict_for_exact_sha(built, monkeypatch):
    service, cid = built
    store = service.store
    sha = store.get(cid, internal=True)['reviewed_fingerprint']
    refused(service, cid)  # sealed external_review_required alone is not a pass

    external_review.record_review(store.database, cid, sha=sha, verdict='needs_revision', reviewer='grok')
    assert store.get(cid, internal=True)['external_review']['verdict'] == 'needs_revision'
    refused(service, cid)

    record = store.get(cid, internal=True)
    record['external_review'] = dict(verdict='passed', reviewer='grok', sha='0' * 64,
                                     recorded_at=record['external_review']['recorded_at'])
    store.save(record)
    refused(service, cid)

    recorded = external_review.main(['record-review', cid, '--sha', sha, '--verdict', 'passed',
                                     '--reviewer', 'grok'], database=store.database)
    assert recorded['verdict'] == 'passed' and recorded['sha'] == sha and recorded['recorded_at']
    assert CandidateApply(service).preview(cid)['eligible']

    monkeypatch.delenv(FLAG)  # turned off after review: apply cannot proceed
    refused(service, cid)


def test_record_review_refuses_wrong_sha_and_other_modes(built, harness, monkeypatch):
    service, cid = built
    store = service.store
    record = store.get(cid, internal=True)
    for sha in ['0' * 64, record['reviewed_fingerprint'][:-1], '']:
        with pytest.raises(ToolDenied):
            external_review.main(['record-review', cid, '--sha', sha, '--verdict', 'passed',
                                  '--reviewer', 'grok'], database=store.database)
    with pytest.raises(SystemExit):
        external_review.main(['record-review', cid, '--sha', record['fingerprint'], '--verdict', 'passed',
                              '--reviewer', 'codex'], database=store.database)
    with pytest.raises(SystemExit):
        external_review.main(['record-review', cid, '--sha', record['fingerprint'], '--verdict', 'rejected',
                              '--reviewer', 'grok'], database=store.database)
    assert 'external_review' not in store.get(cid, internal=True)
    assert not store.get(cid)['busy']

    other = CandidateStore(harness['db']).create('Fix the value', ['src/app.py'], ['tests'])
    with pytest.raises(ToolDenied, match='not in CLAUDE_EXTERNAL_REVIEW mode'):
        external_review.record_review(harness['db'], other['id'], sha=record['fingerprint'],
                                      verdict='passed', reviewer='grok')
    record['operator']['mode'] = 'CLAUDE_ONLY'
    store.save(record)
    with pytest.raises(ToolDenied, match='not in CLAUDE_EXTERNAL_REVIEW mode'):
        external_review.record_review(harness['db'], cid, sha=record['fingerprint'],
                                      verdict='passed', reviewer='grok')
    record['operator']['mode'] = 'CLAUDE_EXTERNAL_REVIEW'
    store.save(record)
    monkeypatch.delenv(FLAG)
    with pytest.raises(ToolDenied, match='not enabled'):
        external_review.record_review(harness['db'], cid, sha=record['fingerprint'],
                                      verdict='passed', reviewer='grok')
    assert 'external_review' not in store.get(cid, internal=True)


def test_record_review_is_not_reachable_from_http_or_tools():
    import main
    paths = [getattr(route, 'path', '') for route in main.app.routes]
    assert paths and not [p for p in paths if re.search(r'external|record.?review', p)]
    root = Path(__file__).resolve().parents[1]
    importer = re.compile(r'from\s+(?:core)?\.external_review\b|import\s+core\.external_review\b'
                          r'|from\s+(?:core|\.)\s+import\s+[^\n]*\bexternal_review\b')
    sources = [root / 'main.py', *[p for package in ('core', 'tools', 'models', 'agents', 'evaluator', 'sandbox')
                                   for p in (root / package).rglob('*.py')]]
    assert root / 'core/candidate_tools.py' in sources
    assert [str(p.relative_to(root)) for p in sources
            if p.name != 'external_review.py' and importer.search(p.read_text())] == []


def test_resealing_discards_an_earlier_verdict():
    saved = []
    record = dict(files_changed=["src/app.py"], tested_fingerprint="a" * 64,
                  external_review=dict(verdict="passed", reviewer="grok", sha="b" * 64))
    s = SimpleNamespace(record=record, _diff=lambda: "c" * 64,
                        store=SimpleNamespace(save=lambda r, event: saved.append(event)))
    operator.record_external_review(s)
    assert "external_review" not in record and record["state"] == "needs_revision"
    assert saved == ["review_requires_passing_tests"]
