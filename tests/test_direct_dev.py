"""Direct development: adapters, routing, isolation, environment and failure evidence."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from core import dev_agents
from core.candidate_provider import ProviderFailure
from core.database import Database
from core.direct_dev import DirectDevelopment, NoAgentAvailable, Router
from core.engineering_portfolio import Portfolio
from core.engineering_providers import EngineeringRegistry, Registration
from tools import CommandResult

API_VARS = {'OPENAI_API_KEY': 'sk-do-not-pass-0123456789', 'OPENAI_BASE_URL': 'https://api.invalid',
            'OPENAI_ORG_ID': 'org-x', 'ANTHROPIC_API_KEY': 'sk-ant-do-not-pass', 'ANTHROPIC_BASE_URL': 'https://x.invalid',
            'AZURE_OPENAI_API_KEY': 'azure', 'GEMINI_API_KEY': 'gemini', 'CODEX_API_KEY': 'codex',
            'GITHUB_TOKEN': 'ghp_x', 'CLAUDE_CODE_OAUTH_TOKEN': 'oauth'}


def git(root, *args):
    return subprocess.run(['/usr/bin/git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def fixture_project(tmp_path):
    source = tmp_path / 'fixture'
    (source / 'src').mkdir(parents=True)
    (source / 'tests').mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.email', 'tests@example.invalid')
    git(source, 'config', 'user.name', 'Tests')
    (source / 'src/calc.py').write_text('def add(a, b):\n    return a - b\n')
    (source / 'tests/test_calc.py').write_text('from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    git(source, 'add', '.')
    git(source, 'commit', '-q', '-m', 'Fixture baseline')
    database = Database(tmp_path / 'state.db')
    portfolio = Portfolio(database)
    template = next(p for p in portfolio.projects() if p['project_id'] == 'jarvis-core')
    portfolio.register_project(template | dict(project_id='dd-fixture', name='Direct dev fixture',
                                               repository=str(source)), replace=True)
    return source, database


class Stub:
    provider_type = 'cli'
    failures = run_id = None

    def __init__(self, name, state='available', edit=None, verdict='passed'):
        self.name, self.live, self.edit, self.verdict, self.calls = name, state, edit, verdict, []

    def executable(self):
        return '/usr/bin/true'

    def probe(self, timeout):
        return self.live

    def complete(self, payload, instructions, timeout):
        self.calls.append(payload['action'])
        if payload['action'] == 'implement':
            path = Path(payload['worktree']) / 'src/calc.py'
            path.write_text(path.read_text().replace('a - b', 'a + b'))
            return dict(agent=self.name, summary='fixed add')
        assert 'a + b' in payload['diff'] and payload['tests']
        return dict(agent=self.name, verdict=self.verdict, summary='reviewed the diff')


class APIStub(Stub):
    provider_type = 'api'

    def complete(self, payload, instructions, timeout):
        pytest.fail('API-billed agent must never be invoked without authorization')


def passed(repository, phase, paths):
    assert 'a + b' in (repository.root / 'src/calc.py').read_text()
    return CommandResult(('pytest',), 0, '1 passed')


def registry(tmp_path, agents):
    return EngineeringRegistry(Database(tmp_path / 'routing.db'),
        [Registration(a, a.provider_type, bool(a.executable())) for a in agents])


def test_default_roles_codex_builds_claude_reviews_on_real_diff(tmp_path):
    source, database = fixture_project(tmp_path)
    codex, claude = Stub('codex_cli'), Stub('claude_cli')
    before = git(source, 'status', '--porcelain'), git(source, 'rev-parse', 'HEAD')
    report = DirectDevelopment(database, candidate_root=tmp_path / 'candidates', state_dir=tmp_path / 'state',
                               agents=[codex, claude], tester=passed).run(
        'dd-fixture', 'Fix add()', ['src/calc.py'], ['tests/test_calc.py'])
    assert report['status'] == 'passed', report.get('failure')
    assert (report['author'], report['reviewer']) == ('codex_cli', 'claude_cli')
    assert codex.calls == ['implement'] and claude.calls == ['review']
    assert '+    return a + b' in report['diff'] and report['files_changed'] == ['src/calc.py']
    assert [d['reason'] for d in report['routing']] == ['default_usable', 'default_usable']
    # The registered source is untouched; edits exist only in the private worktree.
    assert (git(source, 'status', '--porcelain'), git(source, 'rev-parse', 'HEAD')) == before
    assert 'a - b' in (source / 'src/calc.py').read_text()
    assert 'a + b' in (Path(report['worktree']) / 'src/calc.py').read_text()
    routing = [json.loads(line) for line in (tmp_path / 'state/routing.jsonl').read_text().splitlines()]
    assert [r['stage'] for r in routing] == ['implementing', 'reviewing']
    assert oct((tmp_path / 'state/routing.jsonl').stat().st_mode & 0o777) == '0o600'


def test_unavailable_default_routes_to_other_agent_and_reviewer_still_differs(tmp_path):
    source, database = fixture_project(tmp_path)
    codex, claude = Stub('codex_cli', state='not_authenticated'), Stub('claude_cli')
    report = DirectDevelopment(database, candidate_root=tmp_path / 'candidates', state_dir=tmp_path / 'state',
                               agents=[codex, claude], tester=passed).run(
        'dd-fixture', 'Fix add()', ['src/calc.py'], ['tests/test_calc.py'])
    assert report['author'] == 'claude_cli'
    assert report['routing'][0]['reason'] == 'default_probe_not_authenticated'
    # Claude authored, so it can never review; Codex is unusable, so review blocks.
    assert report['status'] == 'failed' and report['failure']['stage'] == 'review'
    assert report['failure']['exception'] == 'core.direct_dev.NoAgentAvailable'
    reasons = {c['agent']: c['reason'] for c in report['routing'][1]['candidates']}
    assert reasons == {'claude_cli': 'reviewer_must_differ_from_author', 'codex_cli': 'recent_not_authenticated'}
    assert claude.calls == ['implement'] and codex.calls == []


def test_failing_tests_stop_before_review(tmp_path):
    _, database = fixture_project(tmp_path)
    codex, claude = Stub('codex_cli'), Stub('claude_cli')
    report = DirectDevelopment(database, candidate_root=tmp_path / 'candidates', state_dir=tmp_path / 'state',
                               agents=[codex, claude],
                               tester=lambda *a: CommandResult(('pytest',), 1, '1 failed')).run(
        'dd-fixture', 'Fix add()', ['src/calc.py'], ['tests/test_calc.py'])
    assert report['status'] == 'tests_failed' and report['tests'][0]['returncode'] == 1
    assert claude.calls == [] and len(report['routing']) == 1


def test_configured_reviewer_role_cannot_review_its_own_change(tmp_path):
    codex, claude = Stub('codex_cli'), Stub('claude_cli')
    router = Router(registry(tmp_path, [codex, claude]), roles={'reviewing': 'codex_cli'})
    assert router.choose('implementing')[0] == 'codex_cli'
    chosen, decision = router.choose('reviewing', exclude={'codex_cli'})
    assert chosen == 'claude_cli' and decision['reason'] == 'default_reviewer_must_differ_from_author'


def test_cli_unavailable_blocks_without_api_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('JARVIS_CODEX_EXECUTABLE', str(tmp_path / 'missing-codex'))
    monkeypatch.setenv('JARVIS_CLAUDE_EXECUTABLE', str(tmp_path / 'missing-claude'))
    monkeypatch.setattr(dev_agents.subprocess, 'Popen', lambda *a, **k: pytest.fail('no process may start'))
    monkeypatch.setattr(dev_agents.subprocess, 'run', lambda *a, **k: pytest.fail('no process may start'))
    agents = [dev_agents.CodexCLIAgent(tmp_path / 'bin'), dev_agents.ClaudeCLIAgent(tmp_path / 'bin'),
              APIStub('codex_api')]
    router = Router(registry(tmp_path, agents))
    with pytest.raises(NoAgentAvailable) as blocked:
        router.choose('implementing')
    reasons = {c['agent']: c['reason'] for c in blocked.value.decision['candidates']}
    assert reasons == {'codex_cli': 'cli_not_installed', 'claude_cli': 'cli_not_installed',
                       'codex_api': 'api_billing_not_authorized'}
    assert blocked.value.decision['reason'] == 'no_usable_agent'


def fake_codex(tmp_path):
    dump = tmp_path / 'env'
    script = tmp_path / 'fake-codex'
    script.write_text(f'''#!/bin/sh
env > {dump}.$1
if [ "$1" = login ]; then echo "Logged in using ChatGPT" >&2; exit 0; fi
while [ $# -gt 0 ]; do if [ "$1" = -o ]; then echo "done" > "$2"; fi; shift; done
''')
    script.chmod(0o755)
    return script, dump


def test_codex_process_inherits_no_api_variables_and_no_bwrap(tmp_path, monkeypatch):
    script, dump = fake_codex(tmp_path)
    monkeypatch.setenv('JARVIS_CODEX_EXECUTABLE', str(script))
    for key, value in API_VARS.items():
        monkeypatch.setenv(key, value)
    agent = dev_agents.CodexCLIAgent(tmp_path / 'bin')
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    result = agent.complete(dict(action='implement', worktree=str(worktree), task='t'), '', 30)
    assert result['summary'].strip() == 'done'
    for phase in ('login', 'exec'):
        env = dict(line.split('=', 1) for line in Path(f'{dump}.{phase}').read_text().splitlines() if '=' in line)
        assert not set(env) & set(API_VARS), phase
        assert not any(k.startswith(('OPENAI', 'ANTHROPIC', 'CLAUDE')) or 'API' in k or 'TOKEN' in k for k in env)
        assert env['PATH'] == str(tmp_path / 'bin')
    tools = {p.name for p in (tmp_path / 'bin').iterdir()}
    assert 'bwrap' not in tools and {'codex', 'sh', 'git'} <= tools


def test_agent_failure_logs_real_class_sanitized_message_and_stage(tmp_path, monkeypatch):
    script, _ = fake_codex(tmp_path)
    monkeypatch.setenv('JARVIS_CODEX_EXECUTABLE', str(script))
    failures = dev_agents.FailureLog(tmp_path / 'private/failures.jsonl')
    agent = dev_agents.CodexCLIAgent(tmp_path / 'bin', failures, 'run-1')

    def broken(*args, **kwargs):
        raise PermissionError('cannot open /work with api_key=sk-secretsecret123456 Bearer abc.def')
    monkeypatch.setattr(dev_agents, 'run_cli', broken)
    with pytest.raises(ProviderFailure) as error:
        agent.complete(dict(action='implement', worktree=str(tmp_path), task='t'), '', 30)
    assert error.value.code == 'provider_unavailable'
    [entry] = failures.entries('run-1')
    assert entry['exception'] == 'builtins.PermissionError' and entry['stage'] == 'implementing.exec'
    assert 'sk-secret' not in entry['message'] and 'abc.def' not in entry['message']
    assert '/work' in entry['message']
    assert oct(failures.path.stat().st_mode & 0o777) == '0o600'


def test_cli_exit_failure_keeps_process_error_and_classifies(tmp_path, monkeypatch):
    script = tmp_path / 'fake-codex'
    script.write_text('#!/bin/sh\nif [ "$1" = login ]; then echo "Logged in using ChatGPT" >&2; exit 0; fi\n'
                      'echo "You hit your usage limit" >&2; exit 3\n')
    script.chmod(0o755)
    monkeypatch.setenv('JARVIS_CODEX_EXECUTABLE', str(script))
    failures = dev_agents.FailureLog(tmp_path / 'failures.jsonl')
    agent = dev_agents.CodexCLIAgent(tmp_path / 'bin', failures, 'run-2')
    with pytest.raises(ProviderFailure) as error:
        agent.complete(dict(action='implement', worktree=str(tmp_path), task='t'), '', 30)
    assert error.value.code == 'provider_quota'
    [entry] = failures.entries('run-2')
    assert entry['exception'] == 'core.dev_agents.AgentProcessError' and 'usage limit' in entry['message']


def test_prepare_failure_is_recorded_with_its_stage(tmp_path):
    source, database = fixture_project(tmp_path)
    (source / 'src/calc.py').write_text('dirty\n')
    report = DirectDevelopment(database, candidate_root=tmp_path / 'candidates', state_dir=tmp_path / 'state',
                               agents=[Stub('codex_cli'), Stub('claude_cli')], tester=passed).run(
        'dd-fixture', 'Fix add()', ['src/calc.py'], ['tests/test_calc.py'])
    assert report['failure']['stage'] == 'prepare'
    assert report['failure']['exception'] == 'tools.policy.ToolDenied'
    assert 'clean' in report['failure']['message']


def test_review_verdict_parsing():
    assert dev_agents.parse_verdict('```json\n{"verdict":"passed","summary":"ok"}\n```')['verdict'] == 'passed'
    with pytest.raises(dev_agents.AgentResponseError):
        dev_agents.parse_verdict('looks fine to me')
