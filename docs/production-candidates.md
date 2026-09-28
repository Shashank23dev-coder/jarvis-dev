# Production chat candidates

Every production agent turn advertises creation, inspection, test rerun, review
and discard tools. Visibility is not action authorization or a broker-health claim.
Requests such as `Build Phase 1 of JarvisBrain` authorize one candidate creation.
The model selects 1–8 source inspection files and test paths. Creation performs
proposal, isolated edits, sandbox tests and independent review synchronously.
Explicit plan-only/negative requests grant no candidate authority. Host checks the
current request again; previous conversation messages and tool output grant none.

`Inspect candidate <UUID>`, `Rerun tests for candidate <UUID>`,
`Review candidate <UUID>`, and `Discard candidate <UUID>` address candidates created
through production chat. Tests invalidate review. Creation also permits inspection,
testing and review of its own candidate within the originating task. Discard needs
its own request. All authenticated chat users share the existing single-user trust
boundary; conversation IDs are audit provenance, not separate tenant authorization.

Production writes bounded request/response JSON only beneath its existing private
`/tmp/jarvis-candidate-mailbox`. No persistent production candidate DB is created.
Normal conversation/task activity still writes the normal chat DB. Candidate work
never receives that DB or the production filesystem. A single operator-run host
broker polls the fixed production container using `docker exec`; it reads no
production env, state, Docker config, SSH files, controller secrets or Caddy config.
No host mount, socket, network listener, credential or extra environment variable
is added to production. Mailbox entries reject symlinks/hardlinks, expire, and have
size/concurrency limits. Broker loss fails closed; it never runs a local fallback.
Restarting production loses the temporary mailbox, not the durable host candidates.
A source task creates at most one durable candidate; retry with a new chat task
can create another. Inspect before retrying a timed-out request.

The fixed host runtime/source is `/home/ubuntu/jarvis-dev`; candidate records and
private cloned worktrees live in `/home/ubuntu/.local/state/jarvis-chat-candidates`.
The original checkout must be clean. No model argument selects roots, commands,
budgets, environment or backend. Existing protected paths, link checks, 8-file /
400-line / 64-tool / 4-model-iteration / 300-second budgets remain enforced.
Tests use the existing fail-closed Bubblewrap/Docker sandbox, with no network,
production mounts or credentials. Reviewer model calls run from trusted dev code,
never from candidate code. Only the development model credential is used by the
trusted model worker; candidate test code receives the existing isolated environment.

Candidate creation atomically records source `production_chat`, request, task and
conversation UUIDs. Subsequent operations retain their originating metadata and
are replay-bounded. Chat receives only candidate identity/state, bounded numeric
diff counts, test phase/exit/incomplete flags and reviewer verdict. Raw diff, paths,
logs, arbitrary reviewer prose and errors stay in dev. Inspect detailed evidence
using the host candidate store, not the production `/candidates` API (disabled).

The mailbox deliberately has no apply or deployment operation, including under
exact approval. Applying still requires the existing exact UUID approval through
the trusted development candidate service **using this broker's candidate DB and
root**. Deployment still requires the existing exact full-commit approval through
the host deployment controller. Creation/review never grant either permission.

## Operator lifecycle

After reviewing this commit, install `deployment/jarvis-candidate-broker.service`
as `~/.config/systemd/user/jarvis-candidate-broker.service`, then run
`systemctl --user daemon-reload` and `systemctl --user enable --now jarvis-candidate-broker`.
The service uses the existing development `.env` model credential; it never loads
the production env file or deployment controller configuration. Docker access is
host-only and remains a trusted operator capability. The unit clears inherited
environment variables before launching. Do not run it from another checkout.

The broker may start before the approved application release; the old image lacks
the transport module, so it will simply wait. Production chat becomes usable only
after the separately approved release containing these modules is deployed.
Do not mount the broker's state or checkout into production. Stop the broker with
`systemctl --user stop jarvis-candidate-broker` to disable delegation; requests fail
closed. No deployment is performed by installing or starting this service.

## Tool discovery

Candidate schemas are rebuilt for each task, including fresh capability questions;
there is no session capability cache. Execution still checks current-message intent
and UUID scope in both the adapter and host broker. A displayed discard tool does
not authorize discard during a build request. Apply/deploy schema approval gates
are unchanged. Broker connection is lazy at invocation; missing/unreachable broker
requests time out and fail closed, with no local candidate fallback. Read-only
repository and deployment tools remain available independently of broker health.
