# JarvisBrain and engineering candidates

Jarvis owns task state, context selection, routing and tool execution. A model is
a provider of bounded turns, not the owner of the conversation or its authority.
Phase 1 introduces this boundary while retaining the existing execution engine.

```text
Existing /task API (agent mode)
  Orchestrator
    JarvisBrain (feature flag)
      normalized TaskContext
      shared bounded memory / recall / experience assembly
      configured ProviderRegistry route
        OpenAIProvider -> existing request_agent_turn transport
      AgentRunner -> existing authorized tools -> normalized AgentOutcome
      TaskStore -> routing activity, ordinary task/messages/experience

Flag off: Orchestrator -> legacy AgentRunner model callable
Provider failure: same running loop -> legacy model callable
```

`core/brain.py` owns provider choice and per-task fallback. The shared
`core/task_context.py` assembles context using existing services and their limits,
provenance, sensitive-content filtering and current-task cutoffs. Brief/missing
recall can still produce a deterministic answer without calling any provider.
Experience capture, retrieval evaluation and durable-memory writes retain their
existing rules. There is no new memory store or database migration.

`AgentRunner` remains the execution engine. It binds tools to the original current
user goal and task/conversation IDs, enforces call/iteration limits, executes tools,
and applies authoritative candidate/deployment response overrides. Provider output
never grants authority. Normalized results remain the existing `AgentOutcome` and
task API records; UI code and response field contracts are unchanged. Explicit
`mode=plan` continues through the legacy planner. Candidate author/reviewer model
workers retain their existing path for legacy creation. The enabled Brain also
offers the engineering action described below.

## Configuration and rollback

Configuration is read when the orchestrator is constructed, normally at process
startup. It is host configuration, never an API field, user message or tool input.

| Variable | Default | Behavior |
| --- | --- | --- |
| `JARVIS_BRAIN_ENABLED` | `0` | Only exact `1` enables the new path. Other values leave legacy behavior enabled. |
| `JARVIS_BRAIN_PROVIDER` | `openai` | Selects one explicitly registered provider for the task. |

For an operator-managed development process, set `JARVIS_BRAIN_ENABLED=1` and
optionally `JARVIS_BRAIN_PROVIDER=openai` before startup. Only OpenAI is implemented
and registered by default. Unknown or uninstalled providers fall back to the
legacy callable; the arbitrary configuration value is not included in audit data.
Changing environment variables after construction does not reroute running tasks.

Rollback to legacy orchestration by unsetting the flag or setting it to `0`, then
restarting the relevant development process. Existing data needs no conversion;
older code can read routing entries as ordinary activity records. A production
rollback remains subject to the existing release controller and approval contract.
The production controller currently supplies a fixed environment without these
flags, so deploying this milestone keeps production on the legacy path. Enabling
it in controlled production requires a separately reviewed operator configuration
change; this milestone does not expand the controller's environment allowlist.

## Provider contract and extension points

`models/provider.py` defines `ProviderRequest`, `ProviderTurn`, the `Provider`
protocol and an explicit immutable registry. A provider implements
`complete(request) -> ProviderTurn`, and declares its host-owned `name`.
Reserved extension names are `claude`, `codex`, `gemini` and `local`; none is an
implemented integration or automatic plugin. There are no request-supplied module
names, executable commands, base URLs or credential locations.

The Jarvis transcript contract retains the established item vocabulary:

- Input messages have `role` and `content`. Assistant message content can contain
  `output_text` or `refusal` parts, as emitted by the existing OpenAI transport.
- A tool request is a `function_call` with `name`, `call_id`, and JSON-string
  `arguments`. A completed tool response is `function_call_output` with the same
  `call_id` and a JSON-string `output`.
- Tool definitions are the existing registry function schemas. `instructions` and
  `allow_tools` are host constraints that adapters must carry to their provider.
- Results contain `output`, user-facing `text`, and `status` (`completed` for a
  usable turn). The runner independently rejects unsupported operations, multiple
  calls per turn, malformed calls and execution-limit violations.
- OpenAI `reasoning` items are transient opaque continuation state. They remain
  only in the in-memory transcript and are never routing metadata or stored
  messages. Adapters must never put hidden chain-of-thought in answer text.

To add a future provider, implement an adapter that translates this vocabulary to
and from its SDK, enforces bounded timeouts/output and disables remote tool
execution, then register a trusted instance in `ProviderRegistry`. Configure its
credentials only through trusted host configuration. Add transport, context,
tool-loop, failure and continuation-isolation tests before making it selectable.
Providers must keep request-specific state local; the registry is shared across
tasks. No future SDK dependencies or autonomous model debates are included here.

`OpenAIProvider` delegates to the existing bounded `request_agent_turn` function.
The model, timeout, retry policy, tool settings and response handling are unchanged.
`models.openai_client.AgentTurn` remains an alias for the normalized `ProviderTurn`
to preserve existing Python callers and candidate-worker imports.

## Failure and persistence semantics

A provider exception, invalid result object, or incomplete turn switches the task
to its legacy callable. Fallback is sticky for the rest of that task. It retries
only the failed model turn with the current context and tool results, never the
whole task. Existing call IDs, tool counts and remaining iteration budget stay in
place, and already completed tools are not re-executed by fallback. There is at
most one additional model call beyond the original iteration budget. If legacy
also fails, the normal sanitized failed-task result is returned. Invalid tool
operations remain host denials and do not trigger a second attempt with expanded
authority. Context/storage failures also do not bypass checks by restarting.

Adapter requests are copied so a failed adapter cannot mutate the fallback's
context or tool definitions. When crossing from a non-OpenAI provider to legacy
OpenAI, opaque reasoning items are removed; normalized calls and results remain.
An OpenAI-to-legacy fallback preserves compatible OpenAI continuation data.

Routing decisions and attempt outcomes use the existing SQLite activity table:
`type=brain_route`, task ID, timestamp, provider, iteration, status and a fixed
reason code. The closed schema accepts only known provider names and reason/status
codes. It stores no goal copies, prompts, tool arguments/results, model outputs,
exception details, credentials or reasoning. The ordinary user message and final
answer are still stored through the existing conversation system. Routing records
survive process restarts and appear through existing task/activity APIs. Flag-off
requests now record the legacy path with the fixed `brain_disabled` category.
Tasks answered by deterministic context
handling also create no provider-turn events because no provider was called.

## Read-only runtime introspection

Ordinary chat can call `inspect_current_brain` with no arguments. The runner binds
it to the current task (the current user request/turn), never a model-supplied ID.
It reads a bounded projection of that task's routing activity, not process
environment, prompts, model payloads, conversation content or private tool traces.
No flag changes, provider selection, candidate apply or deployment capability is
added. The ordinary registry still logs only the tool's safe status summary.

At each Brain invocation a `brain_runtime` event records the effective cached
settings: enabled/disabled and the selected registered provider. Existing
`brain_route` events record which path actually executes each model iteration.
The tool exposes the following allowlisted fields:

- `available` and `recording_status`: distinguish recorded evidence, missing
  records, invalid metadata and an unavailable store.
- `jarvisbrain_enabled` and `feature_flag_state`: effective settings for this
  execution, with the flag normalized to `1` or `0`, never raw configuration.
- `brain_path`: `jarvisbrain`, `legacy`, `legacy_fallback` or `not_recorded`.
- `provider_selected`, `provider_used`, `fallback_used`, `fallback_reason`,
  `routing_decision_category`, `model_iteration` and `route_status`.
- `task_id` (the request/turn ID), `recorded_at`, `latest_route_at`, and
  `observed_at`, with timestamps normalized to UTC.

Provider names and categories come from closed allowlists. `legacy` identifies
the legacy adapter; it does not claim a vendor or model identity that wasn't
recorded. Provider exceptions, arbitrary configuration strings, event messages,
extra fields, system prompts, secrets and hidden reasoning are never projected.
Readers validate stored fields again rather than trusting the entire event.

Missing metadata is unknown, not proof that the Brain was disabled. Older tasks
without the new runtime marker, and callers bypassing the Brain, report metadata
not recorded. A context-only answer may have recorded settings but no provider
execution; its path and provider-used fields remain explicitly unrecorded. A new
runtime marker excludes previous executions of the same task.

When chat uses the tool, the final routing answer is regenerated from the latest
sanitized record. This includes the answer-generating iteration, so fallback after
the initial tool snapshot cannot produce a stale “no fallback” claim. Existing
candidate/apply/deployment outcome reporting retains priority. No provider API call
is made by the reader, and no database migration or production setting change is
required by this introspection patch.

## Phase 2

Future work includes separately tested provider adapters, explicit provider
capabilities, cost/latency-aware routing policies, richer bounded context planning,
and operator-controlled production activation. Multi-model debate, parallel
specialists and local-model execution require their own budgets, isolation and
evaluation; they are not Phase 1 behaviors.

## Engineering action

The authenticated owner capability exposes development engineering tools with or
without the Brain enabled. `prepare_engineering` discovers the configured-dev
workspace; `create_engineering_job` queues the goal, `problem_detected`,
`hypothesis`, `inspect_paths`, and `targeted_tests`. No wording parser or semantic
authorization classifier is involved. The assistant follows the user's scope,
including plan-only requests. Jobs and candidates are accessible across owner
conversations. Task IDs retain submission idempotency, not access restrictions.

The host exports a clean committed baseline to the existing private Git worktree.
It runs targeted baseline tests before any edit, asks the implementation provider
for bounded replacement texts, validates every edit with `CandidateEditor`, runs
targeted/full Python/UI tests, and obtains a fresh independent review. A test failure
or `needs_revision` verdict can trigger a second implementation attempt with the
current files, diff and failure/review evidence. Rejection stops the workflow.
Asynchronous attempts have a 900-second ceiling, four provider turns, 64 internal
operations, eight changed files and 400 changed lines. Exhaustion never earns approval.
Rerunning tests invalidates review. Normalized provider failures preserve provider,
stage, attempt, retryability and a safe recovery hint. They grant no shell access
or changes to the source checkout.

`core/candidate_provider.py` defines the separate `EngineeringProvider` contract
and host-owned implementer/reviewer pair. The first implementer is Codex via the
Responses API (`gpt-5.3-codex`); see the
[official model documentation](https://developers.openai.com/api/docs/models/gpt-5.3-codex).
This is a bounded Codex model integration, not an autonomous Codex CLI process.
The existing OpenAI transport performs independent review in a fresh request,
without the author's transcript. Providers receive only selected text/evidence;
they have no worktree handle, tools, shell, Docker socket or promotion capability.
The host applies their data proposals and alone launches sandboxed tests.
Credentials stay in the existing trusted model worker, outside candidate test
containers. No additional credentials, dependencies or provider calls are needed
for the scripted regression tests.

A later Claude Code reviewer can implement `complete(payload, instructions,
timeout)` and be installed as the trusted review instance independently of the
implementer. Its transport must enforce the same timeout, output, no-tool and
JSON validation contract. It is not implemented or automatically imported today;
model/user arguments cannot select providers, commands, endpoints or credentials.
The provider module is covered by the existing `core/candidate_*` edit protection.

Each engineering candidate persists an `improvement` object in its existing JSON
record (no database migration):

| Field | Evidence |
| --- | --- |
| `problem_detected`, `hypothesis` | Bounded task-specific claims, treated as untrusted context |
| `implementation_provider`, `review_provider` | Host-selected provider identities |
| `files_changed`, `tests_run` | Host-observed paths, phases, exits and fingerprints |
| `failures_revisions` | Up to two attempt results with test/review evidence |
| `baseline_comparison` | Baseline SHA, test paths, sandbox outcome, candidate fingerprint and comparison |
| `review_result` | Independently validated verdict and summary |
| `confidence` | Qualitative test/review support, never a calibrated probability or approval |

Comparison covers the selected tests, not arbitrary behavior or a production
experiment. It reports improved, regressed, unchanged pass/failure, or inconclusive.
If tests change, it reports `changed_test_suite` rather than claiming an equivalent
baseline improvement. Timeouts, truncation and collection/infrastructure errors
make the baseline inconclusive and prevent a passed review. A passing result still
requires all candidate suites and independent review. Full metadata stays in the
development candidate record; production receives only bounded structured status,
counts and comparison results, never source paths, diffs, logs or provider prose.

The finished candidate is presented for human review. Existing exact UUID approval
is still required to apply it to main, and exact commit approval is separately
required for production deployment. Engineering never invokes either operation.
The Brain flag remains default-off, and production activation still requires the
separately reviewed configuration change described above. Deploying this code
does not enable that flag or change production permissions.
