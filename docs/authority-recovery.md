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

## Revision R1 (Claude review)

R1-1 separates reviewed candidate integration from live promotion. Host-only
Portfolio registration now accepts `integration_repository` and
`integration_branch` for `jarvis-core`. The destination must be a separate,
canonical standalone Git checkout on a `jarvis/integration` branch (optional
suffix allowed), outside every registered project root and every executing
source root. The standalone checkout requirement retains the existing safe Git
and locking implementation; this is a separate integration clone, not a linked
Git worktree. Provisioning it is an owner-approved host step, not an operation
available to the model or mailbox. No checkout has been provisioned here.

Jarvis apply refuses missing integration configuration and any live-root target,
even with candidate approval. The original source identity remains bound to the
candidate; the integration target must have the exact clean baseline. Review,
test and diff hashes, path checks, locking, post-apply sandbox verification,
compare-and-swap commit and rollback still apply, with the target recorded in the
journal. A changed target cannot redirect recovery. Existing audit records remain
readable; legacy live-checkout recovery requires the host owner.

The current request must name exactly one candidate ID before any apply. Routine
integration needs no approval phrase. Moving integration changes into the
broker/worker/app checkout is `production_promote`, classified by `assess_action`
as `major_change`, and requires explicit owner approval. There is no automatic
promotion executor. Integration does not run candidate Python on the host.

R1-1 validation: candidate apply/tools/bridge and authority model/acceptance files:
224 passed, 2 live-sandbox tests skipped. Follow-up promotion and destination
denial checks: 13 passed. Docker execution was disabled by a temporary validation
plugin; no host integration checkout or promotion was performed.

R1-2 makes project resolution fail closed. `resolve_project` is the single
selector for workspace discovery, candidate creation, engineering, job creation,
broker requests and `development_service`; it returns a project only when the
goal or a registered `project_id` identifies exactly one registered project.
Unknown or ambiguous goals raise `unknown_project` / `project_ambiguous` with one
clarification. Addressing Jarvis ("Jarvis fix the order bug in the trading bot")
does not select Jarvis over the named project. `submission_decision` also stops
when the decision has no project or the project has no registered source.
Stored candidates and jobs remain readable; legacy candidates without a project
ID resolve only by a unique registered source root, and legacy jobs without one
are blocked by the worker. Unnamed read-only inspection keeps the native
read-only registry. Preflight for an unproven read-only project (e.g. "Fix my
trading bot") returns NEED_MORE_INFORMATION again; an explicit job submission
for it may still build an isolated candidate (applying is gated, see R1-4).

R1-3 removes the mailbox as an approval channel. Every broker `preview`/`apply`
request is marked as relayed: `request.goal` is written by the production
container, so it may name the candidate but is never approval. A relayed apply
that `assess_action` escalates (authority_expansion, major_change or any other
category) raises `host_approval_required` before any mutation, and relayed
previews show the host command instead of a chat phrase. Routine applies still
proceed without approval. The only channel that honors the exact phrase
`Approve and apply candidate <ID> to main` for an escalated apply is the host
controller entrypoint, run by the owner in an interactive terminal on the host:

    JARVIS_MODE=development python -m core.candidate_broker --approve-apply <ID>

It refuses non-interactive stdin, a different checkout or a non-development
mode, then runs the same CandidateApply checks (integration target only for
Jarvis, evidence, locking, verification, rollback). The container has no path
to invoke it, analogous to the deployment controller's host `deploy` prompt.

R1-4 enforces the operator-set `read_only` Portfolio flag again, as an apply
gate rather than an inspection gate. Read-only projects (trading-bot by default)
can still be inspected and get isolated candidates, tests and review. Applying a
change to one is classified by `assess_action(..., read_only_target=True)` as
`major_change` and needs owner approval (through the host channel when the
request arrives via the mailbox, see R1-3). Engineering jobs for such projects
finish as `waiting_for_approval`. The flag comes only from host Portfolio
metadata; an unresolvable project is treated as read-only. Routine applies to
writable non-Jarvis registered projects remain autonomous, but only into that
project's own registered root: the apply refuses a root that changed since the
candidate, belongs to Jarvis, or overlaps an executing checkout or another
registered project.

R1-5 fixes the misleading "prepared" release message. Broker preparation (no
`JARVIS_DEPLOY_CONFIG`) writes to a private `release-preparation` journal that
the deploy controller does not read. Its result now carries
`controller_validated: false` and no `required_confirmation`; the chat reply
says validation passed in isolated preparation, nothing was deployed, and the
deployment controller must re-validate the exact commit on the host before an
approval can be accepted. Only a controller-recorded result offers the exact
approval phrase. The broker still receives no production credentials or
controller journal.

R1-6 derives the protected set instead of hand-maintaining it.
`loaded_modules()` parses (never imports) the trusted executing tree and returns
the local import closure of the runtime entrypoints (`main`,
`core.candidate_broker`, `core.candidate_bridge`, `core.engineering_worker`,
`core.deployment_server`, `core.deployment_release`,
`core.deployment_healthcheck`), including imports inside functions and package
`__init__` files. Every module in it (for example core/recall.py,
core/experience*.py, core/task_store.py, core/memory.py, core/brain_status.py,
core/task_context.py, tools/policy.py) is protected: applying a change escalates
as `authority_expansion`. The security tests (tests/conftest.py,
tests/test_tools.py, tests/test_authority*.py, tests/test_candidate_*.py,
tests/test_engineering_authority.py, tests/test_owner_readonly_flow.py,
tests/test_deployment*.py, tests/test_sandbox*.py) escalate the same way. They
remain editable inside isolated candidates. The stale core/agent_runner.py entry
was removed. A test imports the entrypoints in a subprocess and fails if any
actually loaded local module is missing from the closure or applies routinely.

Held deployment tests now assert the candidate design rather than the old
hard denials: deployment perimeter modules (core/deployment*.py and their
tests) are editable only inside an isolated candidate, which tests its own
copies in the sandbox; applying them escalates. Dockerfile and compose.yaml
remain outside the candidate edit scope. Escape paths (for example
`../var/lib/jarvis/jarvis.sqlite3`, `/var/lib/jarvis`) and symlinked or
hardlinked deployment files are still denied. Host test snapshots still
provision trusted runtime deployment bytes.

Protection cases removed by 9b872a1..084807b were restored in adapted form
(tests/test_engineering_authority.py, tests/test_candidate_apply.py): protected
paths may be prepared in isolation but escalate on apply, jobs touching them
finish `waiting_for_approval`, and secret/escape paths remain hard denials.
The forged-approval tests replace the old "bridge has no apply" case. Read-only
project tests are no longer disabled: R1-4 keeps isolated tests for them and
gates only apply.

### Remaining owner approvals and host steps

- Provision the standalone Jarvis integration clone and register
  `integration_repository` / `integration_branch` (`jarvis/integration...`) for
  jarvis-core in host Portfolio metadata. Until then Jarvis candidates stay
  prepared and tested, and apply reports that integration is not configured.
- Promote reviewed integration commits into the live broker/worker/app
  checkout. This is `production_promote` (major_change). It has no executor and
  remains a manual owner operation.
- Approve escalated applies (authority paths, loaded runtime modules, security
  tests, major changes, read-only projects) only on the host with
  `python -m core.candidate_broker --approve-apply <ID>`.
- Run host controller validation (`validate SHA`) before production deployment
  approval; broker preparation alone does not satisfy deploy.
