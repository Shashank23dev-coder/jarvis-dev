# Owner read-only authorization

**Authenticated owner + trusted registered project + bounded read-only operation
= allow by default.** Project Portfolio supplies the root. The capability registry
describes resources; it is not another read permission system. Candidate
eligibility, baseline matching, engineering preparation and conversation history
do not authorize repository reads.

## Live failure and root cause

On October 4, 2026 the exact request, “Inspect the registered trading-bot
project read-only.”, was submitted to the running production `POST /task` with
existing owner authentication. Task `0649fab7-e470-4b13-ab4b-ddbc3fc50529`
routed through enabled JarvisBrain/OpenAI. Both `inspect_project_repository`
and scoped `list_files` failed with `denied`. HTTP 200 and task `completed`
meant the model had answered; no repository read succeeded.

Host broker PID 318862 started on **October 1 at 11:28:45 UTC**, immediately
after commit `4296789`. Its imported `Request.checked()` action allowlist
predated Portfolio and repository inspection added October 4 (`43b883c` and
`6b1d7d7`). The startup-time validator rejects `project_repository` with
**`ToolDenied: Unsupported candidate operation`**. Replaying that validator
reproduces the rejection. The running process discarded exceptions, so this
internal gate attribution was reconstructed from its start time and startup
source snapshot, rather than obtained from a retained exception log.

A fresh host broker using the same host database/root successfully inspected
identity, listed 181 files and read clean Git status. The production container
has no `/srv/jarvis/projects/trading-bot` mount; the existing host mailbox is
the intended route. The live denial preceded Portfolio/root resolution and
native repository policy. File-policy exceptions could not resolve it.
Evidence: `/tmp/jarvis-owner-readonly-before.json`.

## Complete before authorization path

| Stage / gate | Before behavior | Live result |
| --- | --- | --- |
| Browser Assistant | Sends goal, agent mode and optional conversation ID to `/task` | Exact goal submitted through this endpoint |
| `DeploymentMiddleware` | Checks host, owner Bearer/Basic token and same origin | Passed; missing/invalid auth returns 401 |
| Owner context | Issues in-process `OwnerDevAuthority`; sync endpoint inherits ContextVar | Passed; JSON/model text cannot authenticate |
| `main.create_task` | Validates goal/conversation existence and persists task | Passed; conversation existence is continuity, not project permission |
| Orchestrator / Brain | Pins provider; runner retains execution authority | Passed with enabled OpenAI route |
| Agent loop | Bounds calls/iterations, validates model calls, requires task audit | Passed; two calls attempted |
| Candidate binding | Owner read tools nested under job/source-context binding | Exposed, but coupled to unrelated candidate/job scope |
| Self-state / capabilities | Runtime inventory and optional preflight; fresh scoped evidence required to declare repository reuse | No hard capability check in repo dispatch; stale guidance can discourage inspection |
| Project association | Recognizes seeded names and scopes tools to project ID | `trading-bot`; prompt paths do not supply roots |
| `ToolRegistry` | Strict tool schema, input bounds, metadata audit | Passed; later erased every `ToolDenied` into generic repository-policy denial |
| `CandidateTools.inspect_repository` | Reasserts captured owner; selects production client | Passed |
| `CandidateClient` | Reasserts current owner; requires task/conversation audit IDs | Passed |
| Typed request | Schema, supported action, UUIDs, freshness and repeated operation/path policy | Passed in current production code; repeated checks at client/receive/host |
| Mailbox / transfer | Same-UID private directory, regular single-link files, size/count bounds; fixed Docker exec | Passed; rejection response returned |
| Host private channel | Trusts fixed application's private mailbox; serialized authority is audit metadata | Host trust boundary, separate from model arguments |
| `Broker.check_source` | Requires candidate development checkout before all actions | Unrelated write workspace gate applied to reads |
| Host `Request.checked` | Long-lived imported action allowlist | **Denying gate: October 1 validator does not support `project_repository`** |
| Host Portfolio | Registered ID to trusted root; capability records grant no execution | Not reached for failing calls |
| Trusted resolution | Absolute canonical existing root; no traversal/root/parent/Git symlink; safe directory open | Not reached; fresh invocation succeeds |
| Git identity/baseline | Git and exact saved baseline required even for ordinary reads | Not reached; unnecessary additional read gates |
| Native repo policy/tools | Protected paths, descriptor-relative no-follow reads, regular single-link files; bounded output and fixed Git | Not reached; protections retained |
| Response/audit | Broker erases exception to `ok:false`; client erases reason; registry says “Repository policy denied” | Conceals actual gate at three layers |

Conversation equality remains in candidate creation/idempotency. Repository
resolution had no cross-conversation ownership restriction and no hard
capability-registry permission check. This change does not invent either.

## Removed or reduced gates

- Bind owner project metadata/read tools independently of candidate/job scope.
- Read host metadata/repositories directly through the existing broker's database.
  Reads neither construct `SelfEditService` nor check candidate checkout eligibility.
  Writes/execution retain those controls.
- Stop treating serialized authority fields as authentication. Their schema is
  still strict; HTTP and the private host channel remain trust boundaries.
- Remove repeated adapter path-policy checks. Native `RepositoryTools` retains
  filesystem safety, including discovery filtering and direct-access checks.
- Permit current-root reads despite baseline drift; retain revision/conflict
  and contradiction evidence. Missing Git permits ordinary reads and reports
  `git_unavailable`; Git operations still require usable Git.
- Read-only reuse decisions do not require capability freshness/scope. Engineering
  and execution prerequisites remain controlled.
- Keep the existing broker/mailbox and wire request. The operator loop dispatches
  each request through a fixed fresh local invocation of the same broker. Policy
  therefore cannot remain imported from an old release. This adds no broker,
  remote agent, permission framework or transport. Activating this dispatcher
  still requires replacing the currently running October 1 process at rollout.
- Preserve closed `gate`/`reason` diagnostics through broker, client, result and
  activity. Never expose raw exceptions. Old opaque responses identify
  `broker_response: legacy_rejection` instead of guessing a filesystem denial.
- Add fixed bounded Git log/revision reads. Log returns at most ten commit/parent
  hashes and timestamps, without commit prose, notes or patches.

## Final rules and path

1. Authenticate the owner at HTTP entry, propagate its host-issued context, and
   authenticate the existing private host channel.
2. Accept a registered **project ID**, resolve its root only from trusted host
   Portfolio metadata, and dispatch a closed bounded read operation. No capability
   freshness, conversation permission, baseline match or candidate approval.
3. Apply native filesystem/output protections. Reads grant no writes, execution,
   account access, promotion or deployment.

```text
owner -> HTTP /task -> Orchestrator -> Brain -> AgentRunner -> owner project tools
 -> existing mailbox -> fresh existing host Broker -> host Project Portfolio
 -> trusted registered root -> native read-only repo tools -> bounded evidence
```

Capability lookup and project metadata use the same owner host route. Architecture,
test discovery, dependency and documentation inspection use listing/read/search;
they do not execute project code. Trading Bot is inspection-only. CreatorOS returns
`location_unknown` until trusted registration supplies its root, without fallback.
Default Jarvis behavior and its existing isolated tests remain available.

## Dangerous-action boundaries

Tool arguments, prompt suggestions, cwd and production chat metadata cannot supply
roots. Unregistered IDs remain unavailable. Absolute paths, traversal, protected
files, `.env`, credentials, symlink/hardlink escape and special files remain denied
or omitted. Content/Git diffs retain secret guards and redaction. There is no added
project execution, shell, service/process control, root administration, live trading,
funding, external account or credential access. Candidate edits remain isolated in
configured dev. Apply and production promotion/deployment retain separate exact
approval requirements.

## Real production-equivalent acceptance

The opt-in test submits the exact goal through production-mode HTTP authentication,
enabled Brain and a scripted configured provider. It exercises actual client,
mailbox files, a separate host broker process, host Portfolio, the actual Trading
Bot root and native reads/Git. Client invocation, authorization, resolution and
repository reads are not mocked. Host/chat state is isolated. The chat-side root
deliberately points elsewhere to prove only host metadata determines the root.

Eight successful calls cover capability inventory, metadata, listing, README,
Git status/log/revision/diff. Root `/srv/jarvis/projects/trading-bot`; commit
`e03c307c64fdaaf267ecb7be3898325af555f966`; 181 files; 15,149 README characters.
Missing/invalid HTTP auth returns 401. `git status --short` is empty before/after.
Sanitized proof: `/tmp/jarvis-owner-readonly-acceptance.json`.

Negative production-mode tests cover arbitrary/absolute paths, root overrides,
traversal, protected paths, credential-like content, symlink/hardlink escape,
writes, service/root/trading/external-account commands and test execution.
Other tests cover forged owner metadata, missing owner, unknown location, baseline
drift, missing Git, candidate checkout/service independence and legacy diagnostics.

This proves authorization/transport with real filesystem evidence and scripted
planning. It does not claim an autonomous assessment succeeded on the deployed
application. The deployed app and old broker remain unchanged. Rollout must
activate both app and host dispatcher, then repeat the live UI request.

## Validation

- Focused authorization: `tests/test_owner_readonly_flow.py` and
  `tests/test_engineering_authority.py`: **55 passed, 1 skipped**. The skip is
  the opt-in actual-root acceptance, which passed separately with
  `JARVIS_REGISTERED_READ_ACCEPTANCE=1`.
- Project/repo integration (project repository, portfolio, bridge, candidate
  tools, self-state, jobs, engineering action, agent and Brain):
  **315 passed, 1 skipped**. The skip retains the existing opt-in Docker check.
- Actual Trading Bot production-equivalent acceptance: **1 passed**.
- UI suite including the exact inspection request: **15 passed**.
- Recovered final full Python suite: **1,169 passed, 15 skipped**. The actual-root
  opt-in check passed separately; other skips cover Docker opt-in and
  Bubblewrap-only checks. Final focused regressions: **205 passed, 1 skipped**.
  Final UI suite: **19 passed**.

JUnit evidence: `/tmp/jarvis-owner-focused.xml`,
`/tmp/jarvis-owner-integration.xml`, `/tmp/jarvis-owner-full-python.xml`.
An existing 0.2-second process-start timing check failed under concurrent suite
load and passed on its isolated rerun. Recovery gave the test two seconds for
Python startup while preserving termination/output assertions; production timeout
behavior was not changed. The old self-state test adapter was updated to use
the actual CandidateTools interface. Final recovery evidence is in
`/tmp/jarvis-recovered-full-python.xml` and
[Assistant experience](assistant-experience.md), including the successful
seven-scenario actual-model run with default UI budgets.
