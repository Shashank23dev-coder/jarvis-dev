# Engineering providers

Before this change, engineering candidates used a fixed Codex implementation
adapter (`gpt-5.3-codex` through the OpenAI Responses API) and an independent
OpenAI API reviewer. After this change, those same structured proposal/review
contracts go through a small trusted registry in `core/engineering_providers.py`.
Claude Code is an additional CLI transport. Jarvis still applies validated edits,
runs tests in the existing sandbox, binds review to the diff and fingerprints,
and stops for human approval. Provider routing grants no apply or deploy authority.

## Registration and evidence

Each trusted registration has a stable ID, API/CLI type, enabled/configured
flags, implementation/review capabilities and code/documentation task types.
The initial implementation order is `codex`, then `claude_code`. The existing
`openai` registration remains the default independent reviewer.
Registrations are host-owned, never supplied by models or job arguments.
The generic registration/state contract can represent additional trusted
providers; adding one requires an adapter and an explicit routing-policy update.
No Grok, DeepSeek, arena execution or OpenAI implementation fallback is included.

Provider observations live in `engineering_provider_state` in the existing dev
SQLite database, initialized idempotently without changing existing job records.
One bounded record per provider retains its latest observation, last successful
response, last failure and last quota/unavailability observation. Evidence has
a timestamp, closed reason code, source and scope. Raw CLI output, exception
prose, account details, executable paths and credential values are not projected
or stored as observations.

States are `available`, `unavailable`, `not_authenticated`, `quota_blocked`,
`invocation_failed` and `unknown`. Configuration never proves availability.
Disabled/missing adapters cannot run. A configured provider with unknown
usability is eligible for a bounded attempt. A schema-valid response establishes
timestamped availability, independently of whether the proposed change passes
tests/review. All execution observations expire after 300 seconds: their historic
state remains visible, while current usability becomes unknown. New requests can
then establish fresh evidence. Negative observations are a temporary backoff,
not a permanent claim about quota or service health.

## Routing

[Engineering Operator v1](engineering-operator.md) pins the providers required
by its selected mode and resumes a blocked step without discarding completed
work. The fallback order below remains the behavior for legacy engineering jobs.

Use Codex when usable or not yet observed. Skip a provider with a fresh negative
observation or missing configuration. On an invocation failure, record a closed
reason and try Claude within the same existing request deadline. If neither can
return a valid response, persist an `engineering_providers_blocked` blocker with
provider states/reasons. Deadline exhaustion is explicit and does not count as
a failure of a provider that was never invoked. The job retains the usual
blocked/retry behavior and candidate ID.

Independent review tries OpenAI, Claude, then Codex, excluding every provider
that applied implementation edits to this candidate, including earlier revisions.
Excluded author IDs are included in review blockers. If no independent reviewer
is usable, implementation cannot become a passed candidate. Codex and OpenAI
retain the existing separate-adapter review contract, even though they can share
the same API credential/quota.

The engineering worker refreshes Claude's local authentication observation at
most once per evidence interval. Runtime inspection reads metadata and persisted
observations without making paid provider requests. The owner self-state includes
the engineering registry, current usability, last successful route, historic
failures and the exact fallback policy. Engineering facts are scoped separately
from chat-adapter availability; runtime facts override stale conversation claims.

## Claude Code

Discovery checks `JARVIS_CLAUDE_EXECUTABLE` (a trusted absolute executable path),
PATH, the user's native install, then a single unambiguous user nvm/npm install.
Multiple nvm installations require an explicit PATH or executable setting.
The CLI's bin directory is added to its subprocess PATH without sourcing shell
startup files. No authentication is installed or changed by this integration.

`claude auth status` runs with a short deadline in an empty temporary cwd. Only
an explicit `loggedIn: false` establishes `not_authenticated`. Successful local
login leaves remote usability unknown. Missing executables are unavailable;
unsupported/malformed auth output or a probe timeout remains unknown. No account
or token fields are retained. Authentication is rechecked on every Claude
invocation. Auth/quota failures in the result envelope are classified in memory
and converted to closed failure codes.

Execution uses print mode with JSON output, an empty built-in tool set, plan
permissions, restricted/safe mode, no MCP servers or skills, disabled hooks,
no user/project/local settings and no session persistence. The goal/context goes
through stdin. Source and candidate worktrees are never the CLI cwd. The CLI
returns only proposal/review data. Jarvis applies its existing schema, path,
credential-content, diff, test and review gates. There is no permissions bypass.
Stdout uses temporary spooling with a bounded read, stderr is discarded, and
timeouts kill the CLI process group. The outer transport watchdog also kills
descendants, including separate CLI groups.

Flags and authentication behavior follow the installed CLI help and the
[official CLI reference](https://code.claude.com/docs/en/cli-reference).
Older CLIs that lack required safety flags fail closed as invocation failures;
the adapter never retries by dropping those flags.

## Attribution

Trusted response records persist provider ID, role, attempt/iteration, response
hash and execution origin. Successful edit application and review responses
determine actual implementation/review provider lists. Configured roles alone
never count as work performed. Job provider metadata follows the actual author;
job attribution unions provider evidence across retries/candidate replacements.

`codex_autonomous` and `claude_autonomous` describe work implemented by that
engineering agent, with the existing independent OpenAI API review recorded
separately. Work involving both engineering agents, or other combinations of
actual authors/reviewers, is `multi_provider`. Injected host adapters or detected
external worktree edits are `operator_assisted`, permanently overriding autonomy
for that candidate/job. Provider contributions remain visible when an operator
also intervenes. Normal goal submission or human apply approval does not itself
mean the operator implemented the change. Autonomy describes execution origin,
not successful testing, review, application or deployment.

Legacy responses lacking origin evidence are `unknown`, never retroactively
autonomous. Existing queued jobs, candidate approval gates, source authority and
tool schemas continue to work. Provider blockers and attribution use closed,
reprojectable metadata across the existing dev broker boundary.

## Validation and limits

Focused tests use scripted transports and subprocess fixtures: registration,
unauthenticated/missing Claude, Codex fallback, all-provider blockers, independent
review, stale observations, malformed responses, audit persistence, mixed and
operator-assisted work, CLI deadlines/output limits and secret suppression.
No authenticated Claude session is required.

Live authentication detection on this VPS found Claude installed but
not authenticated. No paid Claude implementation/review smoke test was run.
Codex/OpenAI still share API infrastructure, so successful Claude implementation
can remain blocked if no independent reviewer is usable. Evidence is local,
timestamped and advisory; it is not continuous remote health monitoring.
Claude's administrative managed policy remains authoritative. This is a
tool-disabled CLI integration, not a new OS sandbox for the vendor binary.
Output spooling is deadline-bounded with a bounded read, rather than a streaming
disk quota. Registered adapters route by declared role, with authors excluded
from independent review. Routine reviewed development apply is autonomous;
production deployment still requires exact owner approval.

Final verification on 2026-10-04:

- Provider and watchdog checks: 35 passed.
- Bridge and exact fixture release-guard checks: 76 passed, 1 opt-in Docker skip.
- Broader engineering compatibility checks: 202 passed, 2 opt-in Docker skips.
- Full Python suite: 1,040 passed, 14 skipped. Skips cover opt-in Docker and
  Bubblewrap-specific assertions. Existing trusted test-adapter fork paths emit
  Python deprecation warnings; the normal engineering worker uses subprocesses.
- UI suite: 14 passed.
- Live read-only authentication probe: Claude installed, not authenticated.

The full run exposed existing scripted-clock interference from background reads
and a watchdog fixture deadline too short to reliably reach its test subprocess.
The fixtures now isolate the caller's clock and allow Git setup to finish before
testing descendant termination. The watchdog assertion still checks the child
cannot write after the supervisor deadline. Its reviewed test-file identity was
updated in the release guard; exact-path/blob checks and rejection of unapproved
key material passed. No release or deployment was performed.

The passing full-suite JUnit artifact is
`/tmp/jarvis-provider-full-python-verified.xml`. Implementation, integration,
subprocess verification and fixture corrections were saved in separate commits.
