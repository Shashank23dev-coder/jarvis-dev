# Evidence-grounded task experiences

Jarvis learns operational lessons through retrieval, without changing model weights
or making extra model calls. Reflections are deterministic reductions of validated
tool envelopes and host-recorded candidate test/review/apply outcomes. Model final
answers, raw outputs, file contents, patches, review prose and reasoning are never
reflection inputs.

## Storage and creation

SQLite schema version 3 adds `experiences` and a task-type/timestamp index. The
migration is transactional and repeatable; all existing tables and records remain
unchanged. Each row has a UUID, a unique source key, optional task/candidate foreign
keys, task type, outcome, UTC timestamp and JSON capped at 8,000 ASCII bytes.
The JSON contains the goal/context, attempted approach, outcome, what worked and
failed, structured evidence, reusable lesson and four-part reflection. Goals are
screened before truncating to 240 characters; other prose fields are at most 500
characters. Reflection fields are generated from evidence, not model guesses.

The agent captures a reflection after completing, failing or reaching its execution
limit for an explicit engineering, testing, inspection or candidate operation.
Intent recognition is deliberately conservative and English/text-based: requests
starting with run tests, fix, debug, implement, add, build, configure, inspect,
review and similar verbs qualify. Casual chat and conversation-recall requests do
not create experiences or search this store. A no-tool engineering answer is marked
`unverified`; read-only inspection is `observed`, never proof of a fix.

Up to 32 tool envelopes are reduced immediately; the last eight metadata summaries
are retained with total/omitted counts. A test exit code, timeout or truncation can
veto success regardless of the assistant's response. A later passing check clears
a previous failure only for the same tool and SHA-256 argument-scope identity;
arguments themselves are not stored. A different passing test cannot erase a
failure. Successful checks establish only those checks, not the entire goal.
Recovery records do not infer what caused the recovery.

Candidate reviews and final apply/rollback events also capture phase-specific
experiences. Passed review requires passing test phases bound to the reviewed
fingerprint; applied success requires post-apply verification. Passing tests never
override a negative review. Rollback is a failed apply with successful restoration,
not a successful application. Repeated terminal events update one row per task or
candidate phase. This is a compact lesson store, not the authoritative audit trail.
Candidate outcomes use all eligible phase results before retaining the last six
summaries with total/omitted counts, so the storage bound cannot hide a failed gate.
Read-only candidate preview/inspection creates no candidate reflection.

Reflection runs after the source state commits and is best-effort. Failure emits a
generic log event and cannot change task status, candidate review, approval gates,
commit or rollback. There is no background backfill/retry; an interrupted capture
may leave no lesson. Existing tasks are not retroactively mined.

## Retrieval and precedence

Before a meaningful task, the host scans at most 200 recent experience rows, excludes
the current task and records newer than its creation, then ranks eligible matches:

- Same task type: 4 points.
- Query topic-term coverage: up to 4 points (generic task verbs excluded).
- Outcome quality: succeeded 1, recovered 0.75, observed 0.25, failed 0.1, unverified 0.
- Recency: `1 / (1 + age_in_days / 30)`.

Distinct topics must share a term; generic requests may match by task type.
This is lexical retrieval, not semantic/vector search. Old experiences outside the
bounded recent window may be missed. Defaults inject at most three lessons and
6,000 ASCII JSON bytes, including source IDs, labels and evidence. Host settings
`ExperienceLimits` allow up to five results, 1,000 scanned rows and 10,000 bytes.
Whole entries are omitted when they cannot fit; context is never padded with all
history. The byte bound also provides a conservative token ceiling.

Experience context precedes current conversation messages and is explicitly labeled
`task_experience`. Current instructions, evidence, tool results, tests and reviews
always override it. `FAILED` and `UNVERIFIED` are cautions, not recommendations.
No experience grants tool access, candidate approval or permission to skip checks.
Normal topic/navigation recall remains a separate route and store.

## Security and APIs

The production authentication middleware protects all experience endpoints:

- `GET /experiences?limit=20` (maximum 50)
- `GET /experiences/search?q=parser&limit=3&context_bytes=6000`
- `GET /experiences/{UUID}`
- `DELETE /experiences/{UUID}`

Blank or NUL-containing search text returns a validation error (HTTP 422).

No API or model tool can author arbitrary lessons. Deletion affects only the lesson,
not conversations, memories, tasks or audits. Authentication remains the existing
single-owner model, not multi-user isolation. Local development follows the existing
development authentication policy.

Whole goals containing recognized credentials, protected paths, raw tool-result
markers, private keys or current authentication/provider secret values are excluded
before truncation. Reads recheck goal safety. Tool arguments are hashed, and output
text is discarded, so protected tool content cannot enter through those channels.
Conservative text filtering cannot identify arbitrary unlabeled secrets in user
prose; never put credentials in task goals. Historical lessons are untrusted data,
not executable instructions. No filesystem or Docker privileges were added.

Experiences live in the same external SQLite state volume and use the existing
[backup/restore procedure](deployment.md). Restart preserves them automatically.
The UI is unchanged. Conversation history records dialogue; durable memories are
explicit user/project facts; audits record authoritative events; experiences are
small, derived operational lessons with deliberately narrower evidence claims.

Next milestone: evaluate lesson usefulness on replayed tasks, then add owner review,
feedback and retirement of stale lessons before considering semantic retrieval.
