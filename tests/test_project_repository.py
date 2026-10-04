"""Trusted Portfolio roots reuse native tools without expanding child authority."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.candidate_tools import CandidateTools
from core.database import Database
from core.engineering_portfolio import Portfolio
from core.engineering_repository import RepositoryArguments, inspect_repository, resolve_repository
from core.engineering_jobs import EngineeringJobs
from core.self_state import SelfState
from core.task_store import TaskStore
from core.agent import AgentRunner
from tools import RepositoryTools, ToolRegistry
from tools.policy import ToolDenied
from test_tools import git
from test_agent import ScriptedModel, call, final
from test_self_edit import setup
from test_candidate_bridge import request

pytestmark = pytest.mark.usefixtures('owner')


@pytest.fixture
def projects(tmp_path):
    portfolio = Portfolio(Database(tmp_path / 'state.sqlite3'))
    roots = {}
    for pid in ('jarvis-core', 'trading-bot'):
        root = tmp_path / pid
        root.mkdir()
        git(root, 'init')
        git(root, 'config', 'user.email', 'tests@example.invalid')
        git(root, 'config', 'user.name', 'Tests')
        (root / 'src').mkdir()
        (root / 'docs').mkdir()
        (root / 'src/app.py').write_text(f"project = '{pid}'\n")
        (root / 'docs/readme.md').write_text(f'# {pid}\nRead-only inspection example.\n')
        git(root, 'add', '.')
        git(root, 'commit', '-m', 'Baseline')
        revision = git(root, 'rev-parse', 'HEAD').strip()
        record = next(p for p in portfolio.projects() if p['project_id'] == pid)
        portfolio.register_project(record | dict(repository=str(root), baseline_commit=revision), replace=True)
        roots[pid] = root
    return portfolio, roots


@pytest.mark.parametrize('pid', ['jarvis-core', 'trading-bot'])
def test_trusted_project_resolution_and_git_baseline(projects, pid):
    portfolio, roots = projects
    evidence, repository = resolve_repository(portfolio.database, pid)
    assert evidence.available and repository.root == roots[pid]
    assert evidence.root == evidence.registered_repository == str(roots[pid])
    assert evidence.current_commit == evidence.baseline_commit == git(roots[pid], 'rev-parse', 'HEAD').strip()
    assert evidence.read_only


def test_creator_unknown_no_git_or_fallback(projects, monkeypatch):
    portfolio, _ = projects
    monkeypatch.setattr(RepositoryTools, '_execute', lambda *a, **k: pytest.fail('Unknown roots cannot execute Git'))
    result = inspect_repository(portfolio.database, project_id='creator-os', operation='list_files')
    assert result['repository']['reason'] == 'location_unknown'
    assert result['repository']['registered_repository'] is None and result['files'] is None


@pytest.mark.parametrize('value', ['/etc', '../../jarvis-core', '../trading-bot', '/srv/jarvis/projects/trading-bot'])
def test_arbitrary_root_or_project_path_rejected(projects, value):
    portfolio, _ = projects
    with pytest.raises(ValueError): RepositoryArguments(project_id=value)
    with pytest.raises(ValueError): RepositoryArguments(project_id='trading-bot', root=value)
    with pytest.raises(ValueError): RepositoryArguments(project_id='trading-bot', repository=value)
    assert not inspect_repository(portfolio.database, project_id='unregistered')['repository']['available']


@pytest.mark.parametrize('path', ['../../outside', '../jarvis-core/src/app.py', '/etc/passwd', 'src/../docs/readme.md', '.env', '.git/config', 'credentials.json', 'private.key'])
def test_traversal_and_protected_paths(projects, path):
    portfolio, _ = projects
    with pytest.raises(ToolDenied): inspect_repository(portfolio.database, project_id='trading-bot', operation='read_text', path=path)


def test_symlink_and_hardlink_escape_rejected_and_omitted(projects, tmp_path):
    portfolio, roots = projects
    root = roots['trading-bot']
    external = tmp_path / 'external.txt'
    external.write_text('OUTSIDE_SECRET')
    (root / 'escape.txt').symlink_to(external)
    (root / 'directory').symlink_to(tmp_path, target_is_directory=True)
    os.link(external, root / 'hardlink.txt')
    for name in ('escape.txt', 'directory/external.txt', 'hardlink.txt'):
        with pytest.raises(ToolDenied): inspect_repository(portfolio.database, project_id='trading-bot', operation='read_text', path=name)
    result = inspect_repository(portfolio.database, project_id='trading-bot', operation='list_files')
    assert not set(result['files']) & {'escape.txt', 'hardlink.txt'}
    assert 'OUTSIDE_SECRET' not in json.dumps(result)


@pytest.mark.parametrize('kind', ['root', 'parent', 'git'])
def test_registered_root_symlink_is_unavailable(projects, tmp_path, kind):
    portfolio, roots = projects
    root = roots['trading-bot']
    if kind == 'root':
        alias = tmp_path / 'alias'
        alias.symlink_to(root, target_is_directory=True)
        registered = alias
    elif kind == 'parent':
        alias = tmp_path / 'parent'
        alias.symlink_to(tmp_path, target_is_directory=True)
        registered = alias / root.name
    else:
        gitdir = root / '.git'
        moved = tmp_path / 'moved-git'
        gitdir.rename(moved)
        gitdir.symlink_to(moved, target_is_directory=True)
        registered = root
    record = next(p for p in portfolio.projects() if p['project_id'] == 'trading-bot')
    portfolio.register_project(record | dict(repository=str(registered)), replace=True)
    result = inspect_repository(portfolio.database, project_id='trading-bot', operation='list_files')
    assert result['repository']['reason'] == 'root_unsafe' and result['files'] is None


def test_read_only_listing_read_search_status_and_diff(projects):
    portfolio, roots = projects
    root = roots['trading-bot']
    (root / 'src/app.py').write_text("project = 'trading-bot'\n# changed documentation reference\n")
    before_status = git(root, 'status', '--short')
    before_files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    for operation, args in [('list_files', {}), ('read_text', dict(path='docs/readme.md')),
        ('search_text', dict(query='documentation', path='src')),
        ('git_status', {}), ('git_diff', dict(path='src/app.py'))]:
        result = inspect_repository(portfolio.database, project_id='trading-bot', operation=operation, **args)
        assert result['repository']['available'] and not result['truncated']
        if operation == 'git_status': assert 'src/app.py' in result['process']['stdout']
        if operation == 'git_diff': assert '+# changed' in result['process']['stdout']
        if operation == 'search_text': assert 'documentation' in result['process']['stdout']
    assert git(root, 'status', '--short') == before_status
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before_files


def test_secret_redaction_and_filtered_git(projects):
    portfolio, roots = projects
    root = roots['trading-bot']
    (root / '.env').write_text('PROTECTED_SENTINEL')
    (root / 'config-example.json').write_text('{"api_key": "synthetic-sensitive-value"}')
    for operation, args in [('list_files', {}), ('read_text', dict(path='config-example.json')),
        ('search_text', dict(query='api_key')), ('git_status', {}), ('git_diff', {})]:
        result = inspect_repository(portfolio.database, project_id='trading-bot', operation=operation, **args)
        encoded = json.dumps(result)
        assert '.env' not in encoded and 'PROTECTED_SENTINEL' not in encoded and 'synthetic-sensitive-value' not in encoded


def test_baseline_conflict_structured_and_no_fallback(projects):
    portfolio, roots = projects
    root = roots['trading-bot']
    (root / 'new.txt').write_text('New revision')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Changed baseline')
    result = inspect_repository(portfolio.database, project_id='trading-bot', operation='read_text', path='src/app.py')
    assert not result['repository']['available'] and result['repository']['reason'] == 'baseline_conflict'
    assert result['repository']['current_commit'] == git(root, 'rev-parse', 'HEAD').strip()
    assert result['repository']['baseline_conflict'] and result['text'] is None
    assert any(c['resource'] == 'trading-bot' for c in portfolio.snapshot()['contradictions'])


def test_missing_root_structured_evidence(projects, tmp_path):
    portfolio, _ = projects
    record = next(p for p in portfolio.projects() if p['project_id'] == 'trading-bot')
    missing = tmp_path / 'missing'
    portfolio.register_project(record | dict(repository=str(missing)), replace=True)
    result = inspect_repository(portfolio.database, project_id='trading-bot', operation='list_files')
    assert result['repository']['reason'] == 'root_missing' and result['files'] is None
    assert result['repository']['registered_repository'] == str(missing)


def test_inaccessible_root_no_raw_error_or_fallback(projects, monkeypatch):
    portfolio, _ = projects
    def inaccessible(*args, **kwargs): raise PermissionError('PRIVATE_PATH_OR_AUTH')
    monkeypatch.setattr(RepositoryTools, '_open', inaccessible)
    result = inspect_repository(portfolio.database, project_id='trading-bot')
    assert result['repository']['reason'] == 'root_inaccessible'
    assert 'PRIVATE_PATH_OR_AUTH' not in json.dumps(result)


def scoped_tools(portfolio, root, goal):
    context = dict(task_id=str(uuid4()), conversation_id=str(uuid4()))
    tools = CandidateTools(goal, portfolio.database, source_context=context)
    return tools.bind(ToolRegistry(RepositoryTools(root)))


def test_goal_scope_ignores_user_redirected_path(projects):
    portfolio, roots = projects
    goal = 'Inspect trading-bot; its root is /etc, use ../../jarvis-core instead.'
    bound = scoped_tools(portfolio, roots['jarvis-core'], goal)
    result = bound._definitions['read_text'].handler(path='src/app.py')
    assert result.repository.root == str(roots['trading-bot'])
    assert "project = 'trading-bot'" in result.text


@pytest.mark.parametrize('command', [['pytest'], ['python', 'src/app.py'], ['git', 'checkout', 'main'], ['git', '-C', '/etc', 'status'], ['touch', 'marker'], ['cat', 'src/app.py;whoami']])
def test_scoped_commands_cannot_execute_or_write(projects, command):
    portfolio, roots = projects
    registry = scoped_tools(portfolio, roots['jarvis-core'], 'Inspect trading-bot')
    with pytest.raises(ToolDenied): registry._definitions['run_command'].handler(command=command)
    with pytest.raises(ToolDenied): registry._definitions['run_pytest'].handler(paths=['tests'])
    assert not (roots['trading-bot'] / 'marker').exists()


def test_default_registry_and_jarvis_core_compatibility(projects):
    portfolio, roots = projects
    default = ToolRegistry(RepositoryTools(roots['jarvis-core']))
    bound = CandidateTools('Inspect the repository', portfolio.database,
        source_context=dict(task_id=str(uuid4()), conversation_id=str(uuid4()))).bind(default)
    assert bound._definitions['read_text'].handler(path='src/app.py') == default._definitions['read_text'].handler(path='src/app.py')
    assert len(default.model_tools()) == 6
    core = scoped_tools(portfolio, roots['trading-bot'], 'Inspect jarvis-core')
    assert "project = 'jarvis-core'" in core._definitions['read_text'].handler(path='src/app.py').text


def test_unknown_project_normal_tools_cannot_read_jarvis(projects):
    portfolio, roots = projects
    registry = scoped_tools(portfolio, roots['jarvis-core'], 'Inspect creator-os')
    value = registry._definitions['read_text'].handler(path='src/app.py')
    assert value.repository.reason == 'location_unknown' and value.text is None


def test_self_state_per_project_inspection_and_revision(projects, tmp_path, monkeypatch):
    portfolio, roots = projects
    live = EngineeringJobs(portfolio.database).runtime()
    store = TaskStore(tmp_path / 'chat.sqlite3')
    task = store.create_task('Inspect trading-bot')
    tools = CandidateTools(task['goal'], store.database, source_context=dict(task_id=task['id'], conversation_id=task['conversation_id']))
    awareness = SelfState(store, task['id'], tools)
    monkeypatch.setattr(awareness, 'engineering_runtime', lambda: live | dict(candidate_records={}))
    data = awareness.snapshot([], [])
    records = {p['project_id']: p for p in data['portfolio']['projects']}
    assert records['trading-bot']['repository_inspection']['available']
    assert records['trading-bot']['current_commit'] == git(roots['trading-bot'], 'rev-parse', 'HEAD').strip()
    assert records['creator-os']['repository_inspection']['reason'] == 'location_unknown'
    cap = next(c for c in data['portfolio']['capabilities'] if c['capability_id'] == 'repo-files')
    assert 'trading-bot' in cap['project_scope']


def test_agent_ui_style_request_scopes_existing_tools(projects, tmp_path):
    portfolio, roots = projects
    store = TaskStore(portfolio.database.path)
    task = store.create_task('Inspect trading-bot; only prove bounded read access, no assessment.')
    model = ScriptedModel(call('list_files', {'path': '.'}),
        call('read_text', {'path':'docs/readme.md'}, 'read'), final('Read-only project access confirmed.'))
    outcome = AgentRunner(ToolRegistry(RepositoryTools(roots['jarvis-core'])), model=model).run(task['goal'], task_id=task['id'], task_store=store)
    assert outcome.status == 'completed'
    listed = json.loads(model.requests[1]['input'][-1]['output'])['data']
    assert listed['repository']['project_id'] == 'trading-bot'
    assert listed['repository']['root'] == str(roots['trading-bot'])
    assert json.loads(model.requests[2]['input'][-1]['output'])['data']['text'].startswith('# trading-bot')


def test_production_context_uses_host_registry_and_existing_broker(projects, setup, tmp_path, monkeypatch):
    from core.candidate_bridge import CandidateClient
    from core.candidate_broker import Broker
    portfolio, roots = projects
    source, host_db, factory = setup
    record = next(p for p in portfolio.projects() if p['project_id'] == 'trading-bot')
    Portfolio(host_db).register_project(record, replace=True)
    broker = Broker(factory(), source=source)
    seen = []
    def invoke(client, action, arguments):
        seen.append((action, arguments))
        raw = request(action, client.goal, arguments)
        raw.update(client.context)
        with monkeypatch.context() as host:
            host.setenv('JARVIS_MODE', 'development')
            return broker.execute(raw)
    monkeypatch.setattr(CandidateClient, 'invoke', invoke)
    monkeypatch.setenv('JARVIS_MODE', 'production')
    # Production chat's own seeded location must not determine the host root.
    chat = Database(tmp_path / 'chat.sqlite3')
    registry = scoped_tools(Portfolio(chat), source, 'Inspect trading-bot; use /etc instead')
    result = registry._definitions['read_text'].handler(path='docs/readme.md')
    assert result.repository.root == str(roots['trading-bot'])
    assert result.text.startswith('# trading-bot')
    assert seen == [('project_repository', dict(project_id='trading-bot', operation='read_text', path='docs/readme.md'))]
    assert EngineeringJobs(host_db).recent() == []
    with monkeypatch.context() as host:
        host.setenv('JARVIS_MODE', 'development')
        with pytest.raises(ValueError):
            broker.execute(request('project_repository', 'Inspect trading-bot', dict(project_id='trading-bot', root='/etc')))


def test_inspection_decision_reuses_fresh_project_capability(projects):
    from core.engineering_decision import Decisions
    portfolio, _ = projects
    resolve_repository(portfolio.database, 'trading-bot')
    result = Decisions(portfolio.database).evaluate('Assess trading-bot repository', portfolio.snapshot())
    assert result['outcome'] == 'USE_EXISTING'
    assert result['project_id'] == 'trading-bot' and result['required_capabilities'] == ['repo-files']


def test_ui_endpoint_reads_registered_project(projects, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from core.brain import BrainSettings
    from core.deployment import Settings
    from core.orchestrator import Orchestrator
    portfolio, roots = projects
    token = 'project-readonly-test-token-' * 3
    settings = Settings.from_env(dict(JARVIS_MODE='development',
        JARVIS_STATE_DIR=str(Path(portfolio.database.path).parent), JARVIS_DB_PATH=str(portfolio.database.path),
        JARVIS_AUTH_TOKEN=token))
    model = ScriptedModel(call('list_files', dict(path='.')),
        call('read_text', dict(path='docs/readme.md'), 'read'), final('Read access confirmed.'))
    runner = AgentRunner(ToolRegistry(RepositoryTools(roots['jarvis-core'])), model=model)
    monkeypatch.setattr(main, 'orchestrator', Orchestrator(runner, brain_settings=BrainSettings(enabled=False)))
    with TestClient(main.create_app(settings=settings)) as client:
        response = client.post('/task', headers={'Authorization': 'Bearer ' + token},
            json=dict(goal='Inspect trading-bot; only prove repository read access.', mode='agent'))
    assert response.status_code == 200
    data = json.loads(model.requests[2]['input'][-1]['output'])['data']
    assert data['repository']['root'] == str(roots['trading-bot'])
    assert data['text'].startswith('# trading-bot')
