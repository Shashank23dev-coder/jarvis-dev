"""Reuse quality is persistent, conservative, bounded, and separate from authority."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import main
from core.agent import AgentRunner
from core.database import Database
from core.deployment import Settings
from core.experience import ExperienceStore, ExperienceLimits
from core.experience_evaluation import ExperienceEvaluation, confidence
from core.task_store import TaskStore
from test_agent import ScriptedModel, call, final
from test_deployment import production
from test_experience import capture, observation, reviewed, Registry


def reuse(store, lesson, *, observations=None, status='completed', goal='Run parser tests'):
    task = store.create_task(goal)
    store.set_status(task['id'], 'running')
    store.experiences.evaluation.track(task['id'], [lesson])
    store.set_status(task['id'], status)
    store.experiences.evaluation.evaluate(task['id'], [observation()] if observations is None else observations)
    return task, store.experiences.evaluation.quality(lesson['id'])


def test_useful_reuse_gains_confidence_and_is_idempotent():
    store = TaskStore()
    _, lesson = capture(store)
    task, quality = reuse(store, lesson)
    assert quality['retrieval_count'] == quality['helpful_count'] == 1
    assert quality['confidence_score'] == confidence(1, 0) > 0.5
    assert quality['last_used_at'] and quality['last_evaluated_at']
    store.experiences.evaluation.evaluate(task['id'], [observation(1)])
    assert store.experiences.evaluation.quality(lesson['id']) == quality
    history = store.experiences.evaluation.history(lesson['id'])['evaluations']
    assert len(history) == 1 and history[0]['source'] == 'automatic'
    assert history[0]['evidence']['reason'] == 'matching_check_outcomes'


def test_repeated_success_failure_and_ranking():
    store = TaskStore()
    _, lesson = capture(store)
    _, other = capture(store)
    for _ in range(8):
        _, useful = reuse(store, lesson)
    assert useful['confidence_score'] > 0.75
    _, one_failure = reuse(store, lesson, observations=[observation(1)])
    assert one_failure['confidence_score'] > 0.65
    assert store.experiences.search('Run parser tests')['results'][0]['id'] == lesson['id']
    for _ in range(20):
        _, harmful = reuse(store, lesson, observations=[observation(1)])
    assert harmful['confidence_score'] < 0.25
    assert harmful['misleading_count'] == 21
    assert store.experiences.search('Run parser tests')['results'][0]['id'] == other['id']
    assert store.experiences.get(lesson['id']) is not None
    assert confidence(100000, 0) == 0.95
    assert confidence(0, 100000) == 0.05


@pytest.mark.parametrize('observations,status', [([], 'completed'), ([], 'failed'),
    ([observation(name='run_command')], 'completed'),
    ([observation(path='other')], 'completed'), ([observation()], 'limit_reached'),
    ([observation(), observation(1, path='other')], 'completed')])
def test_no_attribution_without_matching_complete_evidence(observations, status):
    store = TaskStore()
    _, lesson = capture(store)
    _, quality = reuse(store, lesson, observations=observations, status=status)
    assert quality['neutral_count'] == 1 and quality['confidence_score'] == 0.5


def test_recovery_uses_final_same_scope_outcome():
    store = TaskStore()
    _, lesson = capture(store)
    _, quality = reuse(store, lesson, observations=[observation(1), observation()])
    assert quality['helpful_count'] == 1
    _, quality = reuse(store, lesson, observations=[observation(), observation(1)])
    assert quality['misleading_count'] == 1


def test_retrieval_alone_never_promotes_failed_experience():
    store = TaskStore()
    _, lesson = capture(store, observations=[observation(1)])
    for _ in range(10):
        reuse(store, lesson)
        store.experiences.search('Run parser tests')
    quality = store.experiences.evaluation.quality(lesson['id'])
    assert quality['retrieval_count'] == 10 and quality['neutral_count'] == 10
    assert quality['confidence_score'] == 0.5
    for _ in range(10):
        store.experiences.evaluation.feedback(lesson['id'], 'helpful', str(uuid4()))
    result = store.experiences.search('Run parser tests')['results'][0]
    assert result['outcome_label'] == 'FAILED' and not result['recommendation']
    assert result['effective_confidence'] <= 0.5


def test_stale_decay_is_gradual_and_retrieval_does_not_refresh_it():
    store = TaskStore()
    _, lesson = capture(store)
    created = datetime.fromisoformat(lesson['created_at']).replace(tzinfo=timezone.utc)
    evaluation = store.experiences.evaluation
    quality = evaluation.quality(lesson['id'])
    fresh = ExperienceEvaluation(store.database, now=created).effective(quality, lesson['created_at'])
    older = ExperienceEvaluation(store.database, now=created + timedelta(days=180)).effective(quality, lesson['created_at'])
    oldest = ExperienceEvaluation(store.database, now=created + timedelta(days=720)).effective(quality, lesson['created_at'])
    assert fresh == 0.5 and 0.05 < oldest < older < fresh
    task = store.create_task('Run parser tests')
    store.set_status(task['id'], 'running')
    evaluation.track(task['id'], [lesson])
    after = evaluation.quality(lesson['id'])
    assert ExperienceEvaluation(store.database, now=created + timedelta(days=180)).effective(after, lesson['created_at']) == older
    first = ExperienceStore(store.database, now=created).search('Run parser tests')['results'][0]['score']
    later = ExperienceStore(store.database, now=created + timedelta(days=180)).search('Run parser tests')['results'][0]['score']
    assert later < first


def test_conflicting_outcomes_surface_even_when_one_result_fits():
    store = TaskStore()
    _, good = capture(store)
    _, bad = capture(store, observations=[observation(1)])
    service = ExperienceStore(store.database, limits=ExperienceLimits(results=1))
    result = service.search('Run parser tests')
    assert len(result['results']) == 1 and result['truncated']
    selected = result['results'][0]
    assert selected['id'] == good['id'] and selected['conflicting']
    assert selected['conflicts'] == [{'id': bad['id'], 'outcome': 'failed'}]
    assert 'conflict' in __import__('core.experience', fromlist=['EXPERIENCE_INSTRUCTIONS']).EXPERIENCE_INSTRUCTIONS


def test_agent_tracks_once_and_current_failure_overrides_confidence():
    store = TaskStore()
    _, lesson = capture(store)
    store.experiences.evaluation.feedback(lesson['id'], 'helpful', str(uuid4()))
    task = store.create_task('Run parser tests')
    model = ScriptedModel(call('run_pytest', {'paths': ['tests']}), final('All succeeded'))
    AgentRunner(Registry(), model=model).run(task['goal'], task_id=task['id'], task_store=store)
    quality = store.experiences.evaluation.quality(lesson['id'])
    assert quality['retrieval_count'] == 1 and quality['misleading_count'] == 1
    assert 'ALWAYS override old experience' in model.requests[-1]['instructions']
    assert 'CURRENT_FAILURE' in model.requests[-1]['input'][-1]['output']


def test_casual_chat_cannot_update_confidence():
    store = TaskStore()
    _, lesson = capture(store)
    quality = store.experiences.evaluation.quality(lesson['id'])
    task = store.create_task('Hello')
    AgentRunner(model=ScriptedModel(final('Hello'))).run(task['goal'], task_id=task['id'], task_store=store)
    assert store.experiences.evaluation.quality(lesson['id']) == quality
    reuse(store, lesson, goal='What did we talk about last time?')
    assert store.experiences.evaluation.quality(lesson['id']) == quality


def test_reset_preserves_history_and_blocks_old_inflight_evaluation(tmp_path):
    path = tmp_path / 'state.db'
    store = TaskStore(path)
    _, lesson = capture(store)
    reuse(store, lesson)
    pending = store.create_task('Run parser tests')
    store.set_status(pending['id'], 'running')
    store.experiences.evaluation.track(pending['id'], [lesson])
    before = store.experiences.evaluation.quality(lesson['id'])
    store.close()
    store = TaskStore(path)
    assert store.experiences.evaluation.quality(lesson['id']) == before
    quality = store.experiences.evaluation.reset(lesson['id'])
    assert quality['generation'] == 1 and quality['confidence_score'] == 0.5
    assert quality['retrieval_count'] == quality['helpful_count'] == 0
    assert quality['last_used_at'] is quality['last_evaluated_at'] is None
    store.set_status(pending['id'], 'completed')
    store.experiences.evaluation.evaluate(pending['id'], [observation(1)])
    assert store.experiences.evaluation.quality(lesson['id']) == quality
    page = store.experiences.evaluation.history(lesson['id'], limit=1)
    assert page['evaluations'][0]['source'] == 'reset'
    earlier = store.experiences.evaluation.history(lesson['id'], before=page['next_before'])
    assert earlier['evaluations'][0]['verdict'] == 'helpful'
    reuse(store, lesson)
    assert store.experiences.evaluation.quality(lesson['id'])['helpful_count'] == 1


def test_v3_migration_retains_lessons_and_initializes_quality(tmp_path):
    path = tmp_path / 'old.db'
    store = TaskStore(path)
    _, lesson = capture(store)
    with store.database.transaction() as db:
        for table in ('experience_evaluations', 'experience_uses', 'experience_quality'):
            db.execute('DROP TABLE ' + table)
        db.execute('PRAGMA user_version=3')
    store.close()
    store = TaskStore(path)
    assert store.experiences.get(lesson['id']) == lesson
    store.close()
    Database(path).initialize()


def test_owner_endpoints_auth_validation_idempotency_and_deletion(production):
    app = main.create_app(settings=Settings.from_env(production))
    _, lesson = capture(app.state.store)
    base = '/experiences/' + lesson['id']
    headers = {'Authorization': 'Bearer ' + production['JARVIS_AUTH_TOKEN']}
    feedback = {'verdict': 'misleading', 'request_id': str(uuid4())}
    with TestClient(app, base_url=production['JARVIS_PUBLIC_ORIGIN']) as client:
        assert client.get(base + '/evaluations').status_code == 401
        for path in ['/feedback', '/confidence/reset']:
            assert client.post(base + path, json=feedback).status_code == 401
        assert client.post(base + '/feedback', json=feedback, headers=headers).json()['misleading_count'] == 1
        assert client.post(base + '/feedback', json=feedback, headers=headers).json()['misleading_count'] == 1
        assert client.post(base + '/feedback', json=dict(feedback, verdict='helpful'), headers=headers).status_code == 422
        assert client.post(base + '/feedback', json=dict(feedback, notes='secret=hidden'), headers=headers).status_code == 422
        assert client.post(base + '/feedback', json=dict(feedback, request_id='x' * 36), headers=headers).status_code == 422
        data = client.get(base + '/evaluations', headers=headers).json()
        assert data['evaluations'][0]['source'] == 'owner'
        assert client.post(base + '/confidence/reset', headers=headers).json()['confidence_score'] == 0.5
        assert client.delete(base, headers=headers).status_code == 200
        assert client.get(base + '/evaluations', headers=headers).status_code == 404
    with app.state.store.database.transaction() as db:
        for table in ('experience_quality', 'experience_uses', 'experience_evaluations'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0


def test_no_protected_text_in_evaluation_or_usage(monkeypatch):
    store = TaskStore()
    _, lesson = capture(store)
    reuse(store, lesson, observations=[observation(output='secret=DO_NOT_STORE', path='tests')])
    reuse(store, lesson, goal='Run parser tests password=DO_NOT_STORE')
    with store.database.transaction() as db:
        data = json.dumps({table: [dict(row) for row in db.execute('SELECT * FROM ' + table)]
                           for table in ('experience_uses', 'experience_evaluations')})
    assert 'DO_NOT_STORE' not in data and 'RAW_TOOL_OUTPUT' not in data
    assert store.experiences.evaluation.quality(lesson['id'])['retrieval_count'] == 1
    monkeypatch.setenv('JARVIS_AUTH_TOKEN', 'Run parser tests')
    assert store.experiences.get(lesson['id']) is None
    assert store.experiences.search('Run parser tests')['results'] == []


@pytest.mark.parametrize('verdict,expected', [('passed', 'helpful'), ('rejected', 'misleading')])
def test_host_candidate_gates_evaluate_reuse(verdict, expected):
    store = TaskStore()
    candidates, prior = reviewed(store)
    lesson = store.experiences.list()[0]
    task = store.create_task('Review candidate parser')
    store.set_status(task['id'], 'running')
    store.experiences.evaluation.track(task['id'], [lesson])
    candidates, current = reviewed(store, verdict)
    store.set_status(task['id'], 'completed')
    store.experiences.evaluation.evaluate(task['id'], [
        {'tool': 'review_self_edit_candidate', 'candidate_id': current['id'], 'ok': True}])
    history = store.experiences.evaluation.history(lesson['id'])['evaluations']
    assert history[0]['verdict'] == expected
    assert history[0]['evidence']['reason'] == 'host_candidate_gates'


def test_learning_failure_cannot_change_completed_task(monkeypatch):
    store = TaskStore()
    _, lesson = capture(store)
    def fail(*args):
        raise RuntimeError('secret=hidden')
    monkeypatch.setattr(ExperienceEvaluation, 'evaluate', fail)
    task = store.create_task('Run parser tests')
    outcome = AgentRunner(model=ScriptedModel(final('No verified result'))).run(
        task['goal'], task_id=task['id'], task_store=store)
    assert outcome.status == 'completed'
    assert store.experiences.evaluation.quality(lesson['id'])['confidence_score'] == 0.5


def test_neutral_does_not_refresh_stale_confidence():
    store = TaskStore()
    _, lesson = capture(store)
    _, quality = reuse(store, lesson, observations=[])
    assert quality['last_evaluated_at'] and quality['last_signal_at'] is None


def test_similar_success_bonus_is_scoped_to_query_topic():
    store = TaskStore()
    _, lesson = capture(store)
    reuse(store, lesson)
    same = store.experiences.search('Run parser tests')['results'][0]
    related = store.experiences.search('Run parser integration tests')['results'][0]
    assert same['similar_help'] > 0 and related['similar_help'] == 0


def test_pending_retrieval_and_evaluation_survive_restart(tmp_path):
    path = tmp_path / 'state.db'
    store = TaskStore(path)
    _, lesson = capture(store)
    task = store.create_task('Run parser tests')
    store.set_status(task['id'], 'running')
    store.experiences.evaluation.track(task['id'], [lesson])
    store.experiences.evaluation.track(task['id'], [lesson])
    store.close()
    store = TaskStore(path)
    store.set_status(task['id'], 'completed')
    store.experiences.evaluation.evaluate(task['id'], [observation(1)])
    quality = store.experiences.evaluation.quality(lesson['id'])
    assert quality['retrieval_count'] == 1 and quality['misleading_count'] == 1


def test_apply_rollback_is_misleading_despite_model_success():
    store = TaskStore()
    candidates, prior = reviewed(store)
    prior['apply'] = dict(status='applied', post_apply_verified=True,
                          verification=prior['test_results'], rollback=False)
    candidates.save(prior, 'apply_succeeded')
    lesson = store.experiences.list()[0]
    task = store.create_task('Approve and apply candidate parser')
    store.set_status(task['id'], 'running')
    store.experiences.evaluation.track(task['id'], [lesson])
    candidates, current = reviewed(store)
    current['apply'] = dict(status='rolled_back', verification=current['test_results'], rollback=True)
    candidates.save(current, 'apply_rolled_back')
    store.update_task(task['id'], status='completed', response='The model says this succeeded')
    store.experiences.evaluation.evaluate(task['id'], [
        {'tool': 'approve_and_apply_candidate', 'candidate_id': current['id'], 'ok': True}])
    assert store.experiences.evaluation.quality(lesson['id'])['misleading_count'] == 1


def test_current_sensitive_goal_hides_evaluation_api(monkeypatch, tmp_path):
    app = main.create_app(tmp_path / 'state.db')
    _, lesson = capture(app.state.store)
    reuse(app.state.store, lesson)
    monkeypatch.setenv('OPENAI_API_KEY', 'Run parser tests')
    with TestClient(app) as client:
        assert client.get('/experiences/' + lesson['id'] + '/evaluations').status_code == 404
        assert client.post('/experiences/' + lesson['id'] + '/confidence/reset').status_code == 404


def test_uninjected_search_results_and_current_task_are_not_tracked():
    store = TaskStore()
    _, lesson = capture(store)
    for _ in range(3):
        store.experiences.search('Run parser tests')
    assert store.experiences.evaluation.quality(lesson['id'])['retrieval_count'] == 0
    store.set_status(lesson['source_task_id'], 'running')
    store.experiences.evaluation.track(lesson['source_task_id'], [lesson])
    assert store.experiences.evaluation.quality(lesson['id'])['retrieval_count'] == 0
