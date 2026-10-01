# Persistent engineering jobs

Authenticated owners use `prepare_engineering(provider="codex", scope="code")`
(or documentation scope) to obtain the configured-dev editable/test catalog, then
`create_engineering_job` to queue the current goal, problem, hypothesis, inspection
paths and targeted tests. The compatibility name is `engineer_candidate`.
The host action binds submission content for integrity and idempotency; it does
not restrict later inspection or control to the originating conversation.

The broker commits jobs to private host SQLite storage outside the checkout.
Production has no editing fallback or writable host mount. The separate worker
implements in a real isolated candidate rooted in the fixed jarvis-dev source,
runs baseline and targeted tests, the complete Python suite and UI suite in
Docker, and requests independent review. Codex is primary; OpenAI is the separate
review provider. Provider adapters return data and gain no filesystem authority.
The provider contract and failure schema also support a future Claude adapter
without changing owner authorization. Provider installation/routing is host-owned.

States are queued, planning, implementing, testing, reviewing,
waiting_for_approval, completed, blocked and failed. Success stops at
waiting_for_approval. Candidate apply and deployment are separate human gates.
Three explicit worker attempts are allowed. Each asynchronous job attempt has a
900-second ceiling, at most two implementation proposals, four total provider
turns, eight changed files, 400 changed lines and 64 internal operations. A
930-second lease and inherited flock prevent overlap, including after crashes.
Legacy synchronous candidate creation retains its 300-second default.

`list_engineering_jobs`, `inspect_engineering_job`, `inspect_self_edit_candidate`,
`pause_engineering_job`, `cancel_engineering_job` and `resume_engineering_job` work
from any authenticated owner conversation. Control arguments are structured UUIDs;
no exact phrasing is required. Active jobs/candidates cannot be controlled in a
way that races execution. Cancel is terminal; pause is resumable. Failed attempts
require explicit resumption. Retrying creates a fresh candidate for the same job
and preserves previous candidate identities and evidence. Trusted owner recovery
can call `EngineeringJobs.revise_inputs(..., authority=...)` for an inactive blocked
job when old inspection paths become obsolete. Inputs are revalidated and audited;
the job ID, objective, original source and submission action stay unchanged.
Listings return five jobs per page and `next_before`; pass that cursor as `before`
to inspect older jobs. The bounded mailbox never materializes the full history.

Inspection projects only closed status, provider, stage, attempt count, tests,
review verdict and sanitized failure evidence. Failures carry provider, normalized
code, retryability, stage, attempt and a fixed sanitized reason/recovery hint. Authentication,
quota, rate limiting, timeout, invalid response and provider availability are
distinct. Raw errors, paths, logs, credentials and reasoning never cross chat.
Failure history survives resumption and subsequent success.

`inspect_engineering_runtime` is a read-only tool returning `worker` (active,
inactive or unknown), `observed_at` (Unix time or null), `supported` and up to five
projected recent `jobs`. A current heartbeat or a live execution lease supports
active status; observations expire. It never probes a provider or starts work.
`core.engineering_tools.runtime(candidate_tools)` returns this same typed result
for trusted runtime integration. An unavailable broker means unknown, never an
invented inactive worker or missing feature. A previous provider success/failure
is timestamped evidence about that attempt, not proof of current quota or access.

For runtime capability awareness, use the actual bound tool registry names,
`core.brain_status.read_current_brain(store, task_id)` for the current Brain flag
and provider route, and `core.deployment_status.configured_inspector().status()`
for the sanitized production observation. Production revision is the controller's
last observed revision. Do not infer capabilities from old chats or file names.

Back up the private job database and candidate storage together. The executable
worker and broker always pin the configured jarvis-dev checkout. Installation and
restart of these dev services are separate from production deployment.
