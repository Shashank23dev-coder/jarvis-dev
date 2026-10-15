"""Subscription Codex CLI adapter. The CLI receives data, never a worktree path."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .candidate_provider import ProviderFailure


def codex_executable():
    configured = os.getenv('JARVIS_CODEX_EXECUTABLE')
    if configured:
        path = Path(configured)
        return str(path) if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which('codex')
    return found or ('/usr/local/bin/codex' if os.access('/usr/local/bin/codex', os.X_OK) else None)


def codex_failure_code(data):
    value = data.decode('utf-8', errors='replace').lower()[:256000]
    if any(term in value for term in ('not logged in', 'login required', 'authentication', 'unauthorized')):
        return 'provider_authentication'
    if any(term in value for term in ('usage limit', 'quota', 'rate limit', 'insufficient credits')):
        return 'provider_quota'
    return 'provider_invocation_failed'


def codex_request(payload, instructions, timeout):
    executable = codex_executable()
    if not executable:
        raise ProviderFailure('codex_cli', 'provider_unavailable')
    prompt = instructions + '\n\nInput JSON:\n' + json.dumps(payload)
    # The temporary cwd contains no checkout. The CLI sandbox is read only;
    # no tool can apply an edit. Jarvis alone applies validated proposals.
    with (tempfile.TemporaryDirectory(prefix='jarvis-codex-') as directory,
          tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors):
        env = dict(os.environ)
        env.pop('OPENAI_API_KEY', None)
        env.pop('OPENAI_BASE_URL', None)
        env.pop('OPENAI_ORG_ID', None)
        env.pop('OPENAI_PROJECT_ID', None)
        process = None
        try:
            status = subprocess.run([executable, 'login', 'status'], cwd=directory, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(timeout, 5), check=False)
            if status.returncode or status.stdout.strip() != b'Logged in using ChatGPT':
                raise ProviderFailure('codex_cli', 'provider_authentication')
            process = subprocess.Popen([executable, 'exec', '--sandbox', 'read-only',
                '--skip-git-repo-check', '--ephemeral', '--ignore-user-config',
                '--color', 'never', '-'], cwd=directory, env=env,
                stdin=subprocess.PIPE, stdout=output, stderr=errors)
            process.communicate(prompt.encode(), timeout=timeout)
            output.seek(0)
            data = output.read(256001)
            errors.seek(0)
            diagnostic = errors.read(256001)
            if len(data) > 256000:
                raise ProviderFailure('codex_cli', 'provider_output_limit')
            if process.returncode:
                raise ProviderFailure('codex_cli', codex_failure_code(diagnostic + data))
            try:
                return json.loads(data)
            except (ValueError, UnicodeError):
                raise ProviderFailure('codex_cli', 'provider_invalid_response') from None
        except subprocess.TimeoutExpired:
            raise ProviderFailure('codex_cli', 'provider_timeout') from None
        except OSError:
            raise ProviderFailure('codex_cli', 'provider_unavailable') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdin:
                    process.stdin.close()


class CodexCLIProvider:
    name = 'codex_cli'

    def __init__(self, transport):
        self.transport = transport

    def complete(self, payload, instructions, timeout):
        return self.transport(payload, instructions, timeout, provider=self.name)
