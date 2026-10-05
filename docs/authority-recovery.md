# Interrupted authority simplification recovery

Recovered on 2026-10-05 at HEAD `62fcbb98be2e1659e0a822e8475f1d8a590cf581`.
No staged or untracked files; eight modified files, 214 insertions / 32 deletions.
The complete recovered patch and status are preserved in `/tmp/jarvis-authority-recovered.patch`
and `/tmp/jarvis-authority-recovered-status.txt`. No reset, revert, deployment, or
parallel implementation was performed.

Preserved work: authenticated owner project reads and partial-result continuation
from HEAD; lazy provider imports and sandbox fixture fixes from `9603289` and
`e48f4d1`; unfinished central action assessment, candidate preparation scope,
advisory preflight changes, and mailbox fixture error handling from the working tree.

Prior evidence: `/tmp/jarvis-validation-62fcbb9.json` records UI 19/19 and a Python
timeout, not a production security failure. `/tmp/jarvis-fixture-isolated.json`
records four passing sandbox fixture tests. Its fix pins the child source to the
fixture checkout and responds to child failures instead of waiting for mailbox
timeout. Earlier full-suite results belong to the previous commit and cannot
validate the unfinished authority changes.

Recovery findings: central assessment was not yet wired through executors or
assistant guidance. Conversation preparation, candidate UUID scoping and advisory
capability outcomes still acted as duplicate permissions. External-project job
execution still selected Jarvis dev regardless of project identity. The new
candidate apply rule needs coverage for authority/security paths and must run
against verified change evidence. Production release approval and runtime safety
checks remain intact. Source preparation must never execute changed authority
code on the host before approval.

Validation and final checkpoints are recorded below as work completes.

## Second interruption recovery, 2026-10-05

Recovered the same HEAD with 36 modified files and two untracked files (this
report and `tests/test_authority_model.py`). Preserved all work. The exact input
patch, status and untracked contents are saved under
`/tmp/jarvis-authority-resume-20261005*`. No repository instruction files were
found in this checkout or its ancestor directories. No deployment was performed.

The earlier recovery description above describes the first interruption, not the
second. By this interruption, classifier wiring, routine candidate apply,
registered-project development/test selection, research/release preparation,
provider ID generalization and most compatibility updates were already present.
Saved focused validation had 190 passed, 3 failed, 2 skipped; integration had
477 passed, 16 failed, 1 skipped. These failures were retained as the starting
point, not presented as completed validation.

Remaining work: resolve stale gate expectations and provider projection/role
mismatches; validate actual candidate apply through authenticated HTTP; complete
eight task acceptance scenarios, full Python/UI suites and exact-commit release
readiness. Review preserves source/path/secret isolation, reviewed candidate
integrity and rollback, and exact approval before production deployment.

Checkpoint 1: focused authority tests passed **82/82**, recorded in
`/tmp/jarvis-authority-resume_authority.txt` and `.xml`. A further server restart
occurred after the test completed; the result and all edits were recovered.
The default OpenAI review adapter retains its existing reviewer role; additional
registered adapters can route by their declared roles. Job projections now
accept the same bounded provider IDs as registration and action schemas.

Checkpoint 2: repository/candidate/job/provider tests passed **430**, with **1**
explicit opt-in Docker integration skipped, in 584.79 seconds. Evidence:
`/tmp/jarvis-authority-resume_repository.txt` and `.xml`. This recovered the old
failing group without discarding its security, persistence, integrity or rollback
checks. Later additions (project separation, provider extension, release harness
classification and task acceptance) receive separate focused validation below.

Checkpoint 3: assistant/partial-success and real private-mailbox tests passed
**51**, with **1** opt-in sandbox integration skipped, in 265.52 seconds.
Evidence: `/tmp/jarvis-authority-resume_partial.txt` and `.xml`. Source inspection
continued through missing runtime evidence, project clarification remained
concise, protected paths stayed blocked, and child-process fixture failures
returned errors promptly instead of leaving requests waiting.

Checkpoint 4: release harness plus the new authority/provider regressions passed
**184**, with **1** opt-in integration skipped. The group initially had one
obsolete expectation that unavailable test setup must raise a policy denial;
its focused rerun passed after expecting explicit failed harness evidence.
Artifacts: `/tmp/jarvis-authority-resume_release*`. Production secret/context
checks, exact-SHA approval, clean checkout, evidence freshness and rollback checks
remain enforced.

Checkpoint 5: all **eight authenticated UI/task/Brain acceptance scenarios**
passed using deterministic provider responses and real execution adapters.
The routine fixture fix was tested, applied and committed without an approval
phrase. Release preparation and deployment requests prepared existing exact-SHA
fixture evidence with zero production runtime mutations. Two fixture directory
collisions were corrected and those two cases rerun successfully. Evidence:
`/tmp/jarvis-authority-resume_acceptance*`. Compilation and `git diff --check`
passed. Live-provider acceptance and exact-current-commit readiness are separate
checks, driven by `tests/live_authority_acceptance.py`; deterministic providers
are not presented as evidence of live model/provider availability.

## Governing behavior

Authenticated owner plus trusted registered project is normal operational scope.
The existing `engineering_authority` module supplies one consequence assessment;
it does not introduce a second permission system. Preparation/workspace discovery,
conversation identity, advisory preflight outcomes, stale capability observations
and the legacy project `read_only` observation no longer require owner permission
for ordinary isolated source work. Existing IDs, tables, queues, leases, provider
routing, conversations and candidate audits are retained.

Normal operations include repository inspection, registered provider use, research,
candidate/job creation, isolated implementation/testing/review, reversible reviewed
development apply, project observations and release preparation. An available
adapter is still needed: registration does not invent runtime connectivity, an
external database connection, or access to another account.

Only consequential execution escalates:

1. **Financial:** compare trusted adapter amounts and currency against
   `JARVIS_AUTONOMOUS_FINANCIAL_LIMIT` and
   `JARVIS_AUTONOMOUS_FINANCIAL_CURRENCY`. At/below a valid limit proceeds;
   above it, missing/invalid limits, unknown costs or mismatched currency need
   approval. There is no invented limit or exchange rate. Existing configured
   provider use remains normal; no new purchase/transfer executor was added.
2. **Major:** design, isolated development, tests and release preparation proceed;
   high-impact execution/promotion and production deployment require approval.
3. **Destructive:** inspect consequences and prepare recovery automatically;
   destructive/hard-to-reverse execution requires approval. Arbitrary production
   commands remain unavailable through repository tools.
4. **Authority expansion:** changing limits, privileges, permissions or approval
   boundaries requires explicit owner approval. Candidate preparation may touch
   authority source inside isolation; its promotion needs exact candidate approval.

Candidate apply still checks immutable reviewed/tested evidence, the actual diff,
clean matching baseline, source identity, locking, verification and rollback.
Production deploy still requires current-message approval for the exact clean
commit and independently enforces all production safety checks. Path traversal,
symlink/hardlink escape, arbitrary roots, secret disclosure and credential leakage
remain protected. No production deployment is part of this work.

Project names resolve naturally (`my trading bot`, `Jarvis itself`, `CreatorOS`);
root selection is host Portfolio metadata, never request-supplied filesystem paths.
Ambiguous registered references produce one clarification. Missing registered
roots return a specific unknown instead of selecting another project. Legacy
native seed wording is upgraded without changing operator roots or custom notes.
Unavailable Windows/runtime/provider observations do not cancel usable source
inspection. Assistant guidance asks for findings, observed limitations and useful
next actions instead of policy diagnostics or repeated permission requests.

Release validation exports exact Git blobs and tests in the existing sandbox.
Fixture source/state identity is explicit; child failures receive mailbox replies.
Validation evidence distinguishes application test failures, fixture failures,
test-environment failures, timeouts and output limits. Passing evidence is revoked
before a rerun, and failed harness evidence cannot authorize deployment. Production
context checks still run separately and retain their hard denials. Secret-marker
fixtures construct synthetic markers at runtime; whole-file hash exemptions were
removed. Full Python, UI and live-provider/readiness evidence is saved under `/tmp`
and reported against the final commit after the checks finish.
