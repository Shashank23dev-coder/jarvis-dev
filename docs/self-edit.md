# Controlled self-edit candidates

Candidate generation is isolated and never modifies the source checkout.
A separate human-approved apply operation can change main and create a local
commit after verification. No operation pushes or exposes a shell.
The existing UI and its appearance are unchanged.

## Preconditions and isolation

The host source must be a standalone Git checkout with an existing commit and a
clean working tree, including no untracked files. Ignored files are not copied.
An unborn or dirty repository produces a durable rejected attempt. Jarvis will
not invent an initial commit, stash changes, or silently test a stale checkout.

Each attempt gets a UUID, a private bare repository copied using Git's local
transport without shared object hardlinks, and a branch named
`jarvis/candidate-<UUID>` in a linked worktree. The private copy means even the
source repository's refs, index and worktree metadata stay unchanged. Git hooks,
global configuration, external diff/textconv and optional index writes are disabled.
Candidate checkout configuration is not copied from the source repository.

Default storage is `candidates/<UUID>/` beside the configured SQLite database:
`repository.git/` contains private Git metadata and `worktree/` contains the code.
These locations must be outside the source checkout and cannot be symlinked.
During candidate generation, code writes happen only in the candidate worktree; administrative writes are
limited to private candidate storage, SQLite records and isolated test scratch.
Candidates remain on disk until explicitly discarded, including useful failures.

The main checkout, host runtime, Git executable, database and configuration are
trusted host components. This is not a defense against a privileged local user
concurrently replacing directories or modifying the running application's code.
Candidate code and model output are untrusted and never execute on the host.

## Interfaces

Start a synchronous attempt with:

```http
POST /candidates
Content-Type: application/json

{
  "goal": "Correct the calculation and add regression coverage",
  "inspect_paths": ["core/orchestrator.py", "tests/test_api.py"],
  "targeted_tests": ["tests/test_api.py"]
}
```

The caller names 1–8 existing relevant files to inspect and 1–8 test paths under
`tests/`. Requests cannot set repository roots, commands, limits, credentials,
review verdicts, worktree paths or Git options. Unknown request fields are rejected.
These are local application APIs, not a multi-user authorization boundary.

| Interface | Behavior |
| --- | --- |
| `POST /candidates` | Inspect, create worktree, propose/apply, test, inspect diff, independently review |
| `GET /candidates/{id}` | Inspect durable state, diff, test/review evidence and worktree location |
| `POST /candidates/{id}/tests` | Re-run targeted tests, then full Python and available UI suite; invalidate prior review |
| `POST /candidates/{id}/review` | Fresh review only if current content has complete passing test evidence |
| `DELETE /candidates/{id}` | Explicitly remove that candidate's private repository/worktree; keep its audit record |

Unknown/invalid IDs return 404; malformed requests return 422; busy, discarded or
terminally rejected candidates cannot be tested/reviewed and return 409. Starting
a well-formed attempt returns its record even when a safety or test gate fails.
Discard is idempotent. Human-approved application uses the separate interfaces below.

Python host interfaces are `SelfEditService` and `CandidateStore`. The scoped
`CandidateEditor.apply` tool accepts validated `{path, content}` replacements;
it is intentionally absent from the normal chat tool registry. Model proposals
are strict JSON, not patches passed to a shell or executable code.

## Workflow and editable scope

The author receives only the goal and bounded inspected files. It proposes one
batch of complete text replacements/additions. All paths, contents and aggregate
limits are checked before the first write. Writes use directory descriptors,
`O_NOFOLLOW`, temporary regular files and atomic replacement; directories must
already exist. Symlinks, hardlinks, special files, path escapes, duplicate edits,
mode changes, file deletions and renames are rejected.

Approved source paths are `main.py` and Python files under `core/`, `models/`,
`agents/`, `evaluator/`, and `src/`. Also allowed: Python/JavaScript tests under
`tests/`, Markdown/text under `docs/`, and HTML/CSS/JavaScript under `static/`.

Existing protected-path rules remain authoritative. Candidate rules additionally
exclude database filenames/sidecars, credential/secret/password/token/deployment
paths and common credential-like content. `tools/` and the `core/self_edit*` and
`core/candidate_*` implementation files are outside the edit scope. `.env`, `.git`,
`.venv`, policy/security files, keys and deployment credentials remain blocked.
Content checks are defense in depth; they cannot identify every possible secret
hidden in otherwise ordinary source code. Keep secrets in protected locations.

A manifest hashes every candidate file, including protected files and the `.git`
pointer. Before and after edits, before tests, and before accepting a review,
Jarvis verifies the manifest and allowed differences. Protected changes are
rejected before any Git diff can disclose their contents. New files are included
in the displayed diff without staging them.

Targeted Python tests must pass before the full `tests/` Python suite runs. If
`tests/ui.test.cjs` exists, the fixed UI suite must also pass. Both execute inside
the existing read-only, network-isolated Bubblewrap snapshot with a cleared
environment. No candidate Python or JavaScript runs unsandboxed; unavailable
isolation fails closed. The main tree and private Git metadata are not mounted.
For Jarvis's own candidates only, the protected runtime policy dependency must
match the running trusted policy byte-for-byte before it is included for tests.

The reviewer is a separate model request in a fresh process/context. It receives
the goal, original changed files, Git diff and test evidence, with no author
reasoning or tool authority. The host also supplies candidate UUID, creation time,
base commit, branch, pre-review state, changed paths/line counts, inspection/test
paths, current and tested content fingerprints, and the SHA-256 of the exact diff.
Test evidence includes phase, paths, exit status, timeout/truncation flags, bounded
stdout/stderr and content fingerprint; only evidence matching current content is
sent. These fields come from the validated stored candidate, not chat output.

`SelfEditService._review` builds this payload after validating the current diff
and tested fingerprint. `_ask` sends it with the independent `REVIEW` rubric to
`model_request`, which serializes input/instructions to `core.candidate_model`.
That fresh worker sends one JSON user message with those instructions and no
tools or conversation history. The verdict is schema-validated, content is checked
again, and the result is sealed to the fingerprint, diff hash and review time.
The existing apply gate enforces those bindings and the 24-hour review expiry.

Change review evaluates correctness, scope, safety, regressions, documentation
content and test adequacy. Final Assistant reporting requirements (such as listing
the UUID and formatting an evidence report) are separate response validation;
they cannot veto the candidate because the final response is absent from review.
No verdict is automatically upgraded: a fresh independent review is still required
following a metadata-only veto. Existing `needs_revision` candidates can be
reviewed again if their worktree and passing evidence still match and lifetime
budgets remain. Source drift requires a fresh candidate before application.
The reviewer may still veto passing tests. Independence means a separate context,
not a guarantee of correctness or a requirement for a different model.

Final decision states are `passed`, `needs_revision`, and `rejected`; `created`
and `discarded` are lifecycle states. Failed tests and incomplete model responses
need revision. Safety/limit failures and reviewer rejection reject the candidate.
`passed` preserves the worktree for human inspection; it never updates main.
Re-running tests clears the old review. Content fingerprints prevent an old test
or review result from approving changed files. Revisions require a new goal/attempt
or explicit manual work in the candidate followed by the existing test/review gates;
there is no automatic retry/edit loop or general HTTP file-write endpoint.

## Hard bounds and persistence

| Limit | Ceiling/default |
| --- | --- |
| Author/reviewer model calls | 4 across the candidate's lifetime |
| Workflow tool operations, including Git and model calls | 64 |
| Changed files | 8 |
| Added + deleted lines against baseline | 400 |
| Active workflow wall time across operations | 300 seconds |
| Individual model request / Git command | 30 seconds, capped by remaining budget |
| Individual sandbox test process | 120 seconds, capped by remaining budget |
| File content / complete review diff | 256 KB / 128 KB |
| Explicit cleanup operation | 30 seconds, available after workflow budget exhaustion |

Limits and consumed budgets are persisted; restarting or re-running does not reset
them. Trusted host configuration may lower ceilings, but HTTP/model inputs cannot.
The supervisor launches trusted worker subprocesses and stops expired workers and
descendants, including subprocesses with separate process groups. Tests use trusted
scripted adapters; production does not fork the threaded FastAPI process.

SQLite schema version 2 migrates existing version-1 databases transactionally and
idempotently. `candidates` stores ID, creation timestamp, state, operation lease and
a JSON record containing source/base commit, branch/worktree path, limits, budgets,
changed files, diff/summary, bounded test results, reviewer result, content
fingerprints and errors. Its internal baseline manifest is omitted from API output.
`candidate_events` stores ordered, timestamped workflow stage events linked by a
foreign key. Raw model reasoning and rejected proposal contents are not recorded.

An operation lease prevents overlapping edits/tests/reviews/cleanup. After an
interrupted process's bounded lease expires, inspection marks it rejected and
invalidates its evidence. There is no automatic execution recovery. Cleanup is
restricted to the UUID-owned private directory, uses symlink-resistant removal,
and preserves the database record and evidence after deletion.

## Verification and next milestone

Tests cover real Git worktrees, source/index/ref immutability, protected paths,
symlink/hardlink/FIFO escapes, all bounds, full-batch preflight, real Bubblewrap
Python/UI execution, test failures, independent review vetoes, stale evidence,
worker/descendant timeouts, cleanup, API validation, migration and restart recovery.
Provider calls are scripted; no paid model requests are made by the test suite.

Next: a candidate review UI with visible diff, test evidence, reviewer findings and
explicit keep/discard controls and a preview of apply eligibility and audit.
A passing candidate never implies approval to apply.

### Inspection path selection

`inspect_paths` requires individual canonical, repository-relative **editable
files**, not arbitrary readable files or directories. For a documentation task,
use `inspect_paths: ["docs/persistence.md"]` and `targeted_tests: ["tests"]`.
The approved `docs/*.md` / `docs/*.txt` scope includes nested documentation,
subject to all protected-name and link checks. Root `README.md` is outside the
editing allowlist; bare `tests` is a directory, so neither belongs in
`inspect_paths`. Read additional context with the normal read-only tools.

The live rejection for candidate `55789151-7c65-4866-8646-44908a790d14`
was caused by `README.md` following the valid documentation path in
`inspect_paths`; a subsequent `tests` entry was also invalid for inspection.
`SelfEditService.start` calls `CandidatePolicy.edit_path` for each inspection
entry and returns the generic “Protected or invalid candidate path” on the first
denial, before Git/worktree/model/test operations. This message does not identify
which entry failed. No policy relaxation is necessary for `docs/persistence.md`.


## Human-approved application

Only the exact current request `Approve and apply candidate <UUID> to main`
(optionally prefixed with Please) authorizes `approve_and_apply_candidate` in
Assistant. The tool accepts only `candidate_id`; its confirmation comes from the
current host-supplied user request. Prior conversation, model arguments, file
content, “continue”, and “looks good” grant no authority.

- `GET /candidates/{UUID}/apply/preview`: eligibility and required confirmation;
  never applies or recovers interrupted main-tree changes.
- `POST /candidates/{UUID}/apply`: JSON `{"confirmation": "Approve and apply candidate <UUID> to main"}`.
- `GET /candidates/{UUID}/apply`: durable result and audit; also reconciles an
  interrupted apply while holding the exclusive source lock.

Apply preview is read-only and does not modify main.

Assistant exposes `preview_candidate_apply` for named eligibility requests and
`inspect_candidate_apply` for read-only audit snapshots. Unlike HTTP audit recovery,
the Assistant audit tool does not change candidate state or main. Exact current
approval exposes `approve_and_apply_candidate` for that UUID only.

These retain the existing local trusted-user API model; they do not introduce
multi-user authentication. Invalid UUIDs are unavailable, malformed data is
rejected, and invalid confirmation is denied. Eligibility failures return a
persisted `denied` audit. Duplicate requests return the recorded result and never
apply twice. A denied or rolled-back attempt requires a new candidate.

A passed candidate must have complete targeted/full Python and available UI test
evidence, matching fingerprint, and a passing review sealed to its exact diff.
Reviews expire after 24 hours. Older records without review seals require fresh
review. The source checkout must be clean, on a local branch, at the original
base commit. Drift requires a new candidate; this flow never rebases or stashes.
Protected paths, worktree manifests, symlinks, file/line limits and remaining
candidate tool/time budgets are rechecked. Preview also consumes bounded Git/diff
checks from that budget. No schema migration is needed: review seals and apply
journals extend the existing candidate JSON records in SQLite schema version 2.

The service serializes applies per source checkout with an OS file lock. It
journals the approved patch before `git apply --index`, validates exact changed
paths, bytes and modes, then runs targeted checks, full Python and available UI
checks in the existing read-only, network-isolated Bubblewrap sandbox. It checks
the index tree, working tree, branch and HEAD again. Only then does fixed
`commit-tree` create a local commit with message `Jarvis: apply candidate <UUID>`;
`update-ref` advances the branch using the expected baseline as a compare-and-swap.
Hooks and signing are disabled. No shell, push, deployment or service restart is
involved. Empty/unavailable UI suites are recorded as `not_present`; a present UI
suite must pass.

Failure reverses only the approved patch in both index and working tree, then
checks the original clean baseline. Rollback has a separate bounded recovery
allowance even when candidate budgets are exhausted. No broad reset/clean is used.
If a concurrent external edit prevents safe rollback, the audit reports
`rollback_failed` and requires manual intervention rather than deleting human
work. Cooperating Jarvis operations are serialized; the existing trust boundary
still excludes privileged concurrent replacement of host files. After interruption,
audit inspection reconciles a completed commit or rolls back the journaled patch.
Candidate operations are blocked while apply/recovery is unresolved.

The audit records UUID, exact approval text, timestamps, files, patch hash,
baseline/branch, verification results, commit hash, and rollback outcome. The
reviewed patch is stored internally for recovery. Candidate isolation, UI and
memory behavior otherwise remain unchanged.

Live validation: start from a clean committed checkout, create a small docs
candidate using `inspect_paths: ["docs/persistence.md"]`, inspect its passed
state/diff/tests/review, and request the preview endpoint. Then send exactly
`Approve and apply candidate <UUID> to main` in Assistant or the apply POST.
Inspect the audit and local Git log/status. Repeating the same approval must
return the same result without another commit. Do not expect a push or restart.
