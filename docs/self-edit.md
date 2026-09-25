# Controlled self-edit candidates

Self-editing is an explicit workflow, separate from the read-only `/task` agent.
It never merges, commits, pushes, edits the source working tree, or exposes a shell.
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
Code writes happen only in the candidate worktree; administrative writes are
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
Discard is idempotent. There is no merge, commit, push or apply-to-main endpoint.

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
reasoning or tool authority. It may veto passing tests. The review is independent
in context, not a guarantee of correctness or a requirement for a different model.

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
explicit keep/discard controls. Any later apply-to-main milestone should introduce
a separate human-approved integration operation with fresh baseline validation;
it must not be inferred from a passing candidate.
