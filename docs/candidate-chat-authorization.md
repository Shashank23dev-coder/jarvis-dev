# Production candidate authorization

Production advertises five candidate tools, but discovery grants no authority.
The current user goal must authorize the action. The chat adapter binds the task
and conversation UUIDs; the broker rechecks the goal and action independently.
Requests contain UUIDs, goal, action, relative inspection/test paths and creation
time. There is no request field for a repository, workspace, command or credentials.

The host broker runs in development mode and pins its service to the configured
`/home/ubuntu/jarvis-dev` checkout. It checks that source on every operation,
rejecting mismatched roots, symlink aliases, traversal and unavailable checkouts.
The trusted constructor's source override exists for isolated fixture repositories;
the executable broker always uses the fixed development source. Production has no
host checkout mount or Docker authority. Candidate edits live in private host
candidate worktrees, never in the source checkout or production filesystem.

## Diagnosing a denied creation

The registry's `Repository policy denied the operation` message covers any
`ToolDenied`, including intent authorization. It does not identify a path denial.
At commit `4c49d641fb07a605e25d841e6c93d42f074cae03`, the negative-intent expression
matched `Do not change` inside the constraint `Do not change production behavior
by default.`. This emptied the allowed-action set and `CandidateTools.create`
raised `One candidate per explicit request` before creating a mailbox request.
The broker, repository allowlist and path normalization were not reached.

The complete standalone production-default constraint is now excluded from that
negative-intent scan. An affirmative creation/build request is still required.
Other negative instructions, including an additional prohibition on creating or
editing, still deny the request. This is a narrow grammar exception, not general
natural-language authorization. Equivalent unfamiliar wording can fail closed.

Protected paths, secret exclusions, clean-source requirements, candidate budgets,
sandbox tests and independent review remain unchanged. Apply and deployment retain
their separate exact-message approval gates. Missing/misconfigured brokers never
fall back to local production editing. A successful tool call can still return a
rejected candidate; inspect its state and evidence before claiming success.

The intent fix runs in both the chat adapter and host broker. Updating or restarting
only the host broker cannot fix the old production adapter. Until an approved
deployment, `Preserve production behavior by default.` avoids the old false match
while expressing the same constraint. Live tests using that wording verify the
production HTTP/chat/mailbox/broker path, not installation of the new adapter.
