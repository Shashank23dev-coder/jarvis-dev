"""Release browser check orchestration: throwaway names, isolation, budget, teardown.
No Docker daemon or browser is used here; the real check runs in release validation."""
import json
from pathlib import Path

import pytest

from core import deployment_browser
from core.deployment_browser import BrowserCheck, throwaway_names


class FakeDocker:
    def __init__(self, result=None, runner_writes=True, clock=None, have_runner=True, ran_image=None):
        self.calls, self.result, self.runner_writes = [], result, runner_writes
        self.clock, self.have_runner, self.live = clock, have_runner, {}
        self.env_files, self.images, self.ran_image = {}, {}, ran_image

    def __call__(self, *args, timeout=30, optional=False, merge_stderr=False):
        self.calls.append(args)
        if args[:2] == ('image', 'inspect'):
            return '[{}]' if self.have_runner else None
        if args[0] == 'volume' and args[1] == 'create':
            self.live[args[-1]] = args[3].split('=', 1)[1]
            return ''
        if args[0] == 'run':
            name = args[args.index('--name') + 1]
            self.live[name] = args[args.index('--label') + 1].split('=', 1)[1]
            env = Path(args[args.index('--env-file') + 1])
            self.env_files[name] = env.read_text()
            if '-d' in args:  # app container: image precedes the boot script
                self.images[name] = self.ran_image or args[-2]
            else:
                if self.clock:
                    self.clock.now += 200
                out = Path(next(a for a in args if a.endswith(',dst=/out')).split(',')[1][4:])
                if self.runner_writes:
                    (out / 'result.json').write_text(json.dumps(self.result))
                    (out / 'phone-390x664-1-login.png').write_bytes(b'png')
                    (out / 'phone-390x664-console.json').write_text('{}')
            return 'id\n'
        if args[0] == "build":
            return ""
        if args[0] == "logs":
            # Like the real app: stdout is nearly silent, logs (and here a leak of every secret) go to stderr.
            secrets = [l.split('=', 1)[1] for l in self.env_files[args[-1]].splitlines()
                       if l.split('=', 1)[0] in {'JARVIS_AUTH_TOKEN', 'BCHECK_PASSWORD', 'OPENAI_API_KEY'}]
            stderr = ''.join(f'stderr: request_completed leaked={s}\n' for s in secrets)
            return 'request_completed TOKEN\n' + (stderr if merge_stderr else '')
        if args[:2] in {('container', 'inspect'), ('volume', 'inspect')}:
            run = self.live.get(args[2])
            if run is None:
                return None
            key = 'Labels'
            return json.dumps([{'Config': {key: {'jarvis.bcheck': run}}, key: {'jarvis.bcheck': run},
                                'Image': self.images.get(args[2])}])
        if args[0] == 'rm' or args[:2] == ('volume', 'rm'):
            self.live.pop(args[-1], None)
            return ''
        raise AssertionError(args)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


PASS = dict(passed=True, failures=[], viewports={'phone-390x664': {'passed': True}}, seed={'messages': 30})


def runs(docker):
    return [c for c in docker.calls if c[0] == 'run']


def test_throwaway_containers_are_isolated_and_never_production(tmp_path):
    docker = FakeDocker(PASS)
    evidence = BrowserCheck(docker=docker).run('sha256:release', tmp_path / 'art')
    assert evidence['passed'] and evidence['teardown_clean'] and not docker.live
    assert evidence['image'] == evidence['image_id'] == 'sha256:release'
    app, runner = runs(docker)
    names = [c[c.index('--name') + 1] for c in (app, runner)]
    assert all(n.startswith('jarvis-bcheck-') and n != 'jarvis-jarvis-1' for n in names)
    assert app[app.index('--network') + 1] == 'none' and 'sha256:release' in app
    assert runner[runner.index('--network') + 1] == 'container:' + names[0]
    for call in (app, runner):
        joined = ' '.join(call)
        assert '--publish' not in call and '-p' not in call and 'jarvis_state' not in joined
        assert 'docker.sock' not in joined and '--privileged' not in call and '--cap-drop=ALL' in call
        assert '--read-only' in call and 'JARVIS_AUTH_TOKEN' not in joined  # secrets only in env files
    assert 'JARVIS_MODE=production' in app and '--user=10001:10001' in app
    assert deployment_browser.RUNNER_IMAGE in runner and deployment_browser.RUNNER_BASE in deployment_browser.RUNNER_DOCKERFILE.read_text()
    app_env = docker.env_files[names[0]]
    assert 'JARVIS_PUBLIC_ORIGIN=https://localhost:8443' in app_env and 'OPENAI_API_KEY=sk-bcheck-' in app_env
    token = next(l for l in app_env.splitlines() if l.startswith('JARVIS_AUTH_TOKEN=')).split('=', 1)[1]
    assert len(token) >= 32 and token in docker.env_files[names[1]]
    assert 'JARVIS_OWNER_LOGIN_HASH' not in app_env  # hashed inside the release image at boot
    assert evidence['screenshots'] and evidence['console_logs'] == ['phone-390x664-console.json']
    assert 'TOKEN' in (tmp_path / 'art' / 'app-container.log').read_text()  # only literal fixture text


def test_app_container_log_keeps_stderr_and_redacts_secrets(tmp_path):
    docker = FakeDocker(PASS)
    BrowserCheck(docker=docker).run('img', tmp_path / 'art')
    log = (tmp_path / 'art' / 'app-container.log').read_text()
    assert log.count('stderr: request_completed leaked=[redacted]') == 3
    app = runs(docker)[0]
    for line in docker.env_files[app[app.index('--name') + 1]].splitlines():
        key, value = line.split('=', 1)
        if key in {'JARVIS_AUTH_TOKEN', 'BCHECK_PASSWORD', 'OPENAI_API_KEY'}:
            assert value not in log


def test_docker_merges_stderr_into_stdout_only_when_asked(monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs)
        return deployment_browser.subprocess.CompletedProcess(argv, 0, b'out\nerr\n', None)

    monkeypatch.setattr(deployment_browser.subprocess, 'run', fake_run)
    assert deployment_browser.docker('logs', 'x', merge_stderr=True) == 'out\nerr\n'
    deployment_browser.docker('ps')
    assert seen[0]['stderr'] is deployment_browser.subprocess.STDOUT
    assert seen[1]['stderr'] is deployment_browser.subprocess.PIPE
    assert all(k['stdout'] is deployment_browser.subprocess.PIPE for k in seen)


def test_each_run_uses_fresh_names_and_secrets(tmp_path):
    docker = FakeDocker(PASS)
    BrowserCheck(docker=docker).run('img', tmp_path / 'a')
    BrowserCheck(docker=docker).run('img', tmp_path / 'b')
    names = [c[c.index('--name') + 1] for c in runs(docker)]
    assert len(set(names)) == 4
    assert len(set(docker.env_files.values())) == 4


@pytest.mark.parametrize('case', ['check_failed', 'no_result', 'budget'])
def test_failures_are_ineligible_and_still_torn_down(tmp_path, case):
    clock = Clock() if case == 'budget' else None
    result = dict(PASS, passed=False, failures=['phone-390x664:page_not_scrolling']) if case == 'check_failed' else PASS
    docker = FakeDocker(result, runner_writes=case != 'no_result', clock=clock)
    evidence = BrowserCheck(docker=docker, **({'clock': clock} if clock else {})).run('img', tmp_path / 'art')
    assert not evidence['passed'] and not docker.live and evidence['teardown_clean']
    assert evidence['failure_kind'] == {'check_failed': 'browser_check_failure', 'no_result': 'browser_environment',
                                        'budget': 'browser_budget_exceeded'}[case]
    if case == 'check_failed':
        assert 'phone-390x664:page_not_scrolling' in evidence['failures']


def test_app_container_running_a_different_image_fails_without_running_browser(tmp_path):
    docker = FakeDocker(PASS, ran_image='sha256:other')
    evidence = BrowserCheck(docker=docker).run('sha256:release', tmp_path / 'art')
    assert not evidence['passed'] and evidence['failure_kind'] == 'browser_image_mismatch'
    assert evidence['image'] == 'sha256:release' and evidence['image_id'] == 'sha256:other'
    assert len(runs(docker)) == 1 and not docker.live and evidence['teardown_clean']


def test_runner_image_pulled_outside_budget_and_artifacts_dir_must_be_new(tmp_path):
    docker = FakeDocker(PASS, have_runner=False)
    BrowserCheck(docker=docker).run('img', tmp_path / 'art')
    assert docker.calls[1][0] == 'build'
    with pytest.raises(FileExistsError):
        BrowserCheck(docker=FakeDocker(PASS)).run('img', tmp_path / 'art')


def test_names_are_validated():
    names = throwaway_names('0123456789ab')
    assert names['volume'] == 'jarvis-bcheck-state-0123456789ab'
    with pytest.raises(RuntimeError):
        throwaway_names('../jarvis')


def test_stub_reply_selection_and_real_turn_shape(monkeypatch):
    from core.release_browser import boot
    text, delay = boot.choose_reply({'input': [{'role': 'user', 'content': 'BCSEED-7 format a sample answer'}]})
    assert '## Seed section 7' in text and '```python' in text and '**bold point 7**' in text and delay == 0
    text, delay = boot.choose_reply({'input': [{'role': 'user', 'content': 'BCSEED-7'},
                                               {'role': 'assistant', 'content': '## Seed section 7'},
                                               {'role': 'user', 'content': 'hello'}]})
    assert text == boot.HELLO_REPLY and delay > 0
    monkeypatch.setattr(boot.time, 'sleep', lambda s: None)
    import models.openai_client as client
    monkeypatch.setattr(client, 'get_client', lambda: boot.StubClient())
    turn = client.request_agent_turn(input=[{'role': 'user', 'content': 'hello'}], tools=[],
                                     instructions='x', allow_tools=False)
    assert turn.text == boot.HELLO_REPLY and turn.status == 'completed'
    assert turn.output[0]['type'] == 'message'


def test_runner_and_boot_files_ship_with_controller():
    root = Path(deployment_browser.__file__).resolve().parent / 'release_browser'
    runner = (root / 'runner.py').read_text()
    for marker in ("'phone-390x664', 390, 664", "'desktop-1440x900', 1440, 900", 'cross_site_task_403',
                   'no_console_errors', 'no_page_errors', 'reply_replaced_thinking', 'jump_latest_shown',
                   'composer_in_viewport', 'page_not_scrolling', 'root_redirects_to_login', 'login_post_303', 'logout'):
        assert marker in runner
    assert 'api.openai.com' not in runner + (root / 'boot.py').read_text()
