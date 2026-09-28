# Repository tools

`tools.RepositoryTools` is the synchronous low-level Python interface. Agent mode
uses these same tools through `tools.ToolRegistry`, with strict schemas and
metadata-only activity logging. See [agent-tools.md](agent-tools.md) for the
bounded model loop and HTTP integration. The legacy planning API is preserved.

```python
from dataclasses import asdict
from tools import RepositoryTools, ToolDenied, ToolError

tools = RepositoryTools()  # defaults to this Jarvis checkout
files = tools.list_files("core")
source = tools.read_text("core/orchestrator.py")
status = tools.git_status()
diff = tools.git_diff()                 # unstaged changes
staged = tools.git_diff(staged=True)    # staged changes
tests = tools.run_pytest(["tests/test_api.py"])
result = tools.run_command(["git", "status"])
payload = asdict(result)
```

Instantiate the object in trusted application code. Root, policy, limits and
private methods must not be exposed to model-generated arguments. Register only
`list_files`, `read_text`, `git_status`, `git_diff`, `run_pytest`, and
`run_command`. These interfaces do not grant access to arbitrary Python code.

File listing returns sorted relative file names; reading returns UTF-8 text.
Process-style tools return an immutable `CommandResult` with `command`,
`returncode`, `stdout`, `stderr`, `timed_out`, and `truncated`. A nonzero pytest
exit code can mean test failure or sandbox startup failure; inspect stderr.
Rejected operations raise `ToolDenied`; infrastructure/limit failures raise
`ToolError` (the base class of `ToolDenied`). Git errors are sanitized rather
than returning unfiltered diagnostics that could reveal protected paths.

## Allowed commands

The command facade accepts a string parsed by `shlex` or a sequence of arguments:

| Command | Behavior |
| --- | --- |
| `pwd` | Return the configured repository root |
| `ls [relative-directory]` | Recursively list safe files |
| `cat relative-file` | Read a bounded text file |
| `git status` | Filtered porcelain status (filenames quoted with Python `repr`) |
| `git diff [--cached] [relative-file]` | Filtered staged or unstaged diff |
| `pytest [relative-test-path ...]` | Run sandboxed tests |

There is no shell interpreter, arbitrary executable, arbitrary flag, cwd override,
environment override, pipe, redirection, command substitution, or command chaining.
Git operations disable external diff/textconv, filesystem monitors, hooks, renames,
submodule inspection, and optional index writes. No staging, commits, checkout,
reset, fetch, push, or other Git mutation is exposed. Git pathspecs are literal.
Standalone checkouts are supported; linked worktrees/submodules with `.git` files
are rejected. Git executable, installation, metadata and host policy are trusted;
this interface is not a sandbox for a concurrently hostile host process changing
the checkout or Git database.

## Protected paths and limits

Discovery omits protected paths. Explicit access rejects them. Rules apply to
every path component, case-insensitively, including nested directories:

- `.env` and `.env.*`, `.env-*`, `.env_*` variants; `.git`; `.venv` and `venv`.
- `policy`, `policies`, `security`, and common policy/security file names such as
  `SECURITY.md`, `policy.json`, `security_rules.py`, and `access_policy.py`.
- `.agents`, `.codex`, `AGENTS.md`, `SKILL.md`, Git attributes/modules, credential
  directories, private key/certificate files, and generated Python/pytest caches.

Use `ToolPolicy(additional_protected=("config/private.json",))` for application
specific sensitive files. Built-in protections cannot be removed through that
interface. This is a path policy, not secret detection: secrets copied into an
ordinary source file are not automatically recognized.

Absolute paths, `..`, backslashes, NULs, symlinks (including directory symlinks),
hard-linked files and special files are rejected. File reads use directory
descriptors and `O_NOFOLLOW`. UTF-8 decoding and binary-control-character checks
reject binary content. Default limits are 1 MB per file, 1 MB combined subprocess
output, 10,000 files, 30 seconds for Git, and 120 seconds for pytest. Timed-out or
output-limited subprocess groups are killed. Partial Git status is never returned.

## Pytest isolation

Linux and a functional sandbox backend are required. Bubblewrap is probed first;
development falls back to a separately built, hardened Docker runtime if that
probe fails. Production remains Bubblewrap-only. If neither backend is usable,
execution is denied. **There is no fallback to host execution.** See
[development sandbox](development-sandbox.md) for image setup, resource limits,
audit fields, security boundaries and live validation.

Tests run against a temporary snapshot of allowed regular files (at most 50 MB),
mounted read-only at `/workspace`. Protected files and links are absent, with one
execution-only exception: when testing Jarvis's own runtime checkout, the trusted
`tools/policy.py` runtime dependency is included in the read-only snapshot. This
does not permit discovery or direct reads through repository tools and does not
include policy files from arbitrary target repositories. `/usr`
and the Python runtime are read-only; only isolated scratch storage is writable.
Namespaces isolate the network, processes and host filesystem, and capabilities
are dropped. Host environment variables and credentials are not forwarded.
Pytest config discovery, plugin autoload, bytecode writing, and pytest caching are
disabled. Only test file/directory paths are accepted, not node IDs or pytest flags.

Tests can still execute arbitrary code *inside* this sandbox. Tests that require
network access, repository writes, secrets, other protected policy imports, or automatically
loaded plugins can fail. OpenAI client initialization is now lazy, so imports do
not require credentials. The sandbox cwd and pytest Python path are `/workspace`,
so Jarvis imports resolve against the snapshot just as they do at the repo root.
The cleared environment has no `PYTHONPATH`; Python isolated mode remains enabled.
The host developer test suite remains independently runnable using
`.venv/bin/python -m pytest -q` and `node --test tests/ui.test.cjs`.
Wall-clock and output bounds are enforced. Docker additionally enforces CPU,
memory, process-count and scratch limits; Bubblewrap retains its existing limits.

## Next milestone

Deployment preview/history and exact-current-message deployment authorization are
documented in [the controlled deployment pipeline](deployment-pipeline.md). The
controller is development-only and disabled until configured by the operator.

Validate a production sandbox deployment and add persistent execution records,
cancellation and resource quotas. Keep file mutation disabled; introduce reviewed
change proposals and human approval before a later editing tool.

## Isolated editing workflow

The separate [self-edit candidate service](self-edit.md) owns private Git copies
and narrowly scoped candidate edits. These read-only repository tools and their
command allowlist are unchanged. A host-only `run_ui_tests()` method runs the fixed
UI suite in the same Bubblewrap snapshot; it is not exposed by the chat registry
or the command facade.
