"""Claude Code is a data-only provider, never an editor or shell executor."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from .candidate_provider import ProviderFailure


def claude_executable():
    # User installs may be absent from a service's PATH.
    configured = os.getenv('JARVIS_CLAUDE_EXECUTABLE')
    if configured:
        path = Path(configured)
        return str(path) if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which('claude')
    user_install = Path.home() / '.local/bin/claude'
    if found:
        return found
    if user_install.is_file() and os.access(user_install, os.X_OK):
        return str(user_install)
    # A single user-level npm/nvm installation is unambiguous. Multiple versions
    # require PATH or an explicit trusted host setting; never source shell files.
    installs = [p for p in (Path.home() / '.nvm/versions/node').glob('*/bin/claude')
                if p.is_file() and os.access(p, os.X_OK)]
    return str(installs[0]) if len(installs) == 1 else None


def run_claude(arguments, *, timeout, prompt=None):
    executable = claude_executable()
    if not executable:
        raise ProviderFailure('claude_code', 'provider_unavailable')
    # No source cwd, project settings, CLI transcripts, stderr or raw auth data
    # are retained. Stdout spools to disk so an oversized response cannot fill RAM.
    with tempfile.TemporaryDirectory(prefix='jarvis-claude-') as directory, tempfile.TemporaryFile() as output:
        process = None
        try:
            environment = {key: value for key, value in os.environ.items() if key not in {
                'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'}}
            environment['PATH'] = str(Path(executable).parent) + os.pathsep + environment.get('PATH', '')
            process = subprocess.Popen([executable, *arguments], cwd=directory,
                stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL, start_new_session=True,
                env=environment)
            process.communicate(prompt.encode() if prompt is not None else None, timeout=timeout)
            output.seek(0)
            data = output.read(256001)
            if len(data) > 256000:
                raise ProviderFailure('claude_code', 'provider_output_limit')
            return process.returncode, data
        except subprocess.TimeoutExpired:
            raise ProviderFailure('claude_code', 'provider_timeout') from None
        except OSError:
            raise ProviderFailure('claude_code', 'provider_unavailable') from None
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if process.stdin:
                    process.stdin.close()


def probe_claude(timeout=5, *, subscription_only=False):
    if not claude_executable():
        return 'unavailable'
    try:
        code, data = run_claude(['auth', 'status'], timeout=timeout)
        status = json.loads(data)
        if isinstance(status, dict) and status.get('loggedIn') is False:
            return 'not_authenticated'
        if subscription_only and (not isinstance(status, dict) or
                                  status.get('authMethod') != 'claude.ai'):
            return 'not_authenticated'
        # Unsupported auth status, malformed output, and successful login all
        # leave remote usability unknown. Never infer authentication from code 1.
        return 'unknown'
    except ProviderFailure as exc:
        return 'unavailable' if exc.code == 'provider_unavailable' else 'unknown'
    except (ValueError, UnicodeError):
        return 'unknown'


def claude_failure_code(value):
    """Classify CLI error output in memory; only closed codes cross the boundary."""
    text = str(value).lower()[:256000]
    if any(term in text for term in ('not logged in', 'not authenticated', 'authentication_error',
                                    'invalid api key', 'please run /login', 'login required',
                                    'oauth token has expired', 'authentication failed',
                                    'invalid authentication credentials', 'token has been revoked')):
        return 'provider_authentication'
    if any(term in text for term in ('rate_limit', 'rate limit', 'usage limit', 'quota',
                                    'hit your limit', 'credit balance', 'insufficient credits')):
        return 'provider_quota'
    return 'provider_invocation_failed'


def claude_request(payload, instructions, timeout, *, subscription_only=False):
    # Authentication is checked per invocation, including after a stale success.
    import time
    deadline = time.monotonic() + timeout
    state = probe_claude(min(5, timeout), subscription_only=True) if subscription_only else probe_claude(min(5, timeout))
    if state in {'not_authenticated', 'unavailable'}:
        raise ProviderFailure('claude_code', 'provider_authentication' if state == 'not_authenticated' else 'provider_unavailable')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderFailure('claude_code', 'provider_timeout')
    arguments = ['-p', '--output-format', 'json', '--tools', '',
        '--permission-mode', 'plan', '--restricted', '--safe-mode',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--disallowedTools', 'mcp__*', '--setting-sources', '',
        '--settings', '{"disableAllHooks":true}', '--disable-slash-commands',
        '--no-session-persistence', '--max-turns', '1', '--system-prompt', instructions]
    code, data = run_claude(arguments, timeout=remaining, prompt=json.dumps(payload))
    try:
        envelope = json.loads(data)
    except (ValueError, UnicodeError):
        raise ProviderFailure('claude_code', claude_failure_code(data) if code else 'provider_invalid_response') from None
    if not isinstance(envelope, dict):
        raise ProviderFailure('claude_code', 'provider_invalid_response')
    if code or envelope.get('is_error'):
        raise ProviderFailure('claude_code', claude_failure_code(envelope))
    if envelope.get('type') != 'result' or not isinstance(envelope.get('result'), str):
        raise ProviderFailure('claude_code', 'provider_invalid_response')
    return envelope['result']


class ClaudeCodeProvider:
    name = 'claude_code'

    def __init__(self, transport):
        self.transport = transport

    def probe(self, timeout):
        return probe_claude(timeout)

    def complete(self, payload, instructions, timeout):
        return self.transport(payload, instructions, timeout, provider=self.name)


class ClaudeCLIProvider(ClaudeCodeProvider):
    name = 'claude_cli'

    def probe(self, timeout):
        return probe_claude(timeout, subscription_only=True)
