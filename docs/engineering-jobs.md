# Persistent engineering jobs

JarvisBrain authorizes a typed engineering action bound to the current task,
conversation and goal. `create_engineering_job` (also available through the
existing `engineer_candidate` tool) queues that action and returns its durable ID.
Production uses the bounded broker mailbox; acceptance means the host SQLite
transaction has committed. A lost response can be retried with the same task;
different content for that task is rejected. Browser and production restarts do
not erase accepted jobs. An unavailable broker fails closed: no acceptance is
reported and no local production editing fallback exists.

The separate `core.engineering_worker` process uses the broker's private database
outside the checkout. The supplied systemd unit is an operator installation
artifact, not installed or activated by chat. Its only source is configured
`/home/ubuntu/jarvis-dev`. Production receives no host mount, Docker socket, shell,
secrets, or writable source tree. Codex returns structured edits without tools;
the existing candidate policy, isolated worktree, Docker test sandbox and separate
review provider enforce execution boundaries. Production env files are never read.

States are queued, planning, implementing, testing, reviewing,
waiting_for_approval, completed, blocked and failed. This version stops at
waiting_for_approval; it never marks a job completed by applying or deploying.
Every transition and candidate identity is persisted. Improvement evidence records
the observed problem, hypothesis, baseline, proposal, implementation attempts,
tests, review and candidate result. Detailed evidence remains private in dev;
chat inspection returns a bounded status/history projection.

Read tools are discoverable on every task with host-bound source metadata,
including ordinary status questions without a special opening phrase. Inspection
does not require creation authority or a new engineering action. The broker still
checks the source conversation for every supplied job ID; an ID copied into a
different conversation grants no access. Missing jobs, invalid metadata and an
unavailable broker fail closed without a filesystem fallback. The projection
validates closed stage, provider, attempt, candidate, test and review fields and
reports a categorical blocker, lease active/expired flags, recovery count and
retry availability. It excludes lease owner IDs, raw provider errors and paths.
Controls are advertised only for an explicit current-message control request.

A SQLite transaction claims a job with a 330-second lease. A permanent flock
serializes workers, and the candidate subprocess inherits that lock. Lease expiry
alone cannot start overlapping execution. After a crash, recovery waits for both
lock release and lease expiry. A fully reviewed candidate is re-inspected and
reconciled; interrupted candidates are retained for audit and a fresh isolated
candidate is created. At most three worker attempts are allowed, each with the
existing 300-second candidate budget and two implementation revisions. There is
no automatic retry of a known failed outcome: explicit resume is required.

`list_engineering_jobs` and `inspect_engineering_job` are read-only and scoped to
the source conversation. Controls require the current message `Pause engineering
job <UUID>`, `Cancel engineering job <UUID>` or `Resume engineering job <UUID>`.
Pause/cancel are accepted only while inactive, so they cannot race ongoing edits.
Cancellation is terminal (blocked with cancel reason); pause is resumable.
Exhausted retries cannot resume. Queue controls grant no candidate apply or
deployment permission. Existing exact approval gates are unchanged.

Back up the private job/candidate database and candidate storage together. Use
one configured database per checkout; do not replace its lock file while a worker
is alive. The host operator remains trusted. The deployment preview and worker
installation are separate operations; deploying the web image alone does not
activate this dev worker.
