# Read-only agent execution

The Assistant now sends goals to a bounded agent loop. The model can gather
repository evidence with the existing safe tools and produce a final answer.
This chat registry has no file-writing tool, arbitrary shell, Git mutation, dynamic registration,
hosted code interpreter, or model-controlled policy/root configuration.

## API compatibility

`POST /task` accepts `{"goal": "..."}` and defaults to the tool-capable agent.
The model chooses whether tools are needed; casual questions can return directly.
Repository inspection and test requests execute the allowed tools and summarize
their results rather than returning an engineering plan.

Use `{"goal": "...", "mode": "plan"}` for explicit plan-only behavior,
including `steps`, `planned` status, and existing planning activity messages.

Use `{"goal": "Inspect this repository", "mode": "agent"}` for tool execution.
The existing Assistant submits this mode and displays the final `response` as
plain text. Layout, styling, navigation and existing plan rendering are unchanged.

Agent tasks retain `id`, `goal`, `status`, `created_at`, `steps` (an empty list),
and `activity`. They add:

```json
{
  "response": "I inspected the repository ...",
  "agent": {
    "iterations": 3,
    "tool_calls": 2,
    "stop_reason": "final_response"
  }
}
```

Agent statuses are `running`, `completed`, `limit_reached`, or `failed`.
Completed/limited runs return HTTP 200; failed runs return HTTP 500 with a fixed,
safe message and remain accessible through `/tasks` and `/tasks/{task_id}`.
`/activity` includes the tool events through its existing event list.
Requests cannot change iteration limits, repository roots, permissions, timeouts,
environment variables, or tool registration. There is no new raw-tool HTTP endpoint.

## Architecture

```text
Assistant / POST /task (mode=agent)
  -> Orchestrator.run_goal
  -> AgentRunner: Responses request -> validated call -> result -> next request
  -> ToolRegistry: strict arguments -> existing RepositoryTools -> typed result
  -> TaskStore: start and completion/failure metadata
```

`ToolRegistry` contains six explicitly bound handlers in an immutable mapping.
It never resolves model-supplied attribute names or evaluates generated code.
`model_tools()` exports strict Responses function definitions; `schemas()` exports
independent JSON Schemas for every argument object and result envelope. Local
Pydantic validation is mandatory even when provider strict mode is enabled.

| Tool | Required arguments | Result data |
| --- | --- | --- |
| `list_files` | `path: string` (`.` for root) | `files: string[]`, `truncated: boolean` |
| `read_text` | `path: string` | `text: string`, `truncated: boolean` |
| `git_status` | Empty object | Process result |
| `git_diff` | `path: string or null`, `staged: boolean` | Process result |
| `run_pytest` | `paths: string[]` | Process result |
| `run_command` | `command: string[]` (argument vector) | Process result |

A process result contains `returncode`, `stdout`, `stderr`, `timed_out`, and
`truncated`. Every response is a JSON object with `ok`, `data` (or null), and
`error` (or null). Errors have a stable `code` and a fixed safe `message`:
`unknown_tool`, `invalid_arguments`, `denied`, `tool_failed`, `command_failed`,
`timed_out`, `output_limit`, `limit_reached`, or `invalid_call`.
Failed processes can retain partial data so the model can explain a test failure;
that data is never copied into activity records.

Argument objects reject extra fields and type coercion. JSON must be an object
matching the schema; duplicate keys and oversized input are rejected. Path strings
are capped at 4,096 characters, pytest at 32 targets, command vectors at 34 elements,
and serialized arguments at 16 KiB. Repository path/command restrictions remain
authoritative and unchanged; see [tools.md](tools.md).

## Loop limits and protocol

Defaults are **8 dispatch attempts** and **10 model requests** per task. Unknown
tools, malformed arguments, tool failures and policy denials consume the dispatch
budget. Trusted host code can choose smaller/larger limits up to hard ceilings of
32 dispatches and 64 requests; the API and model cannot change them.

The final iteration is reserved for an answer with `tool_choice="none"`. Tool
choice is also disabled after the dispatch budget is exhausted. If the model
still requests a tool, the request is audited as rejected and no handler runs.
Repeated call IDs, malformed call metadata, incomplete model responses and
multiple calls in a turn are rejected. No automatic tool retries occur.

Each model request uses a 30-second SDK timeout, zero SDK retries and a 4,096-token
output cap. Tools keep their existing timeouts, process cleanup and sandbox rules.
Returned text/process output is capped at 16,000 characters; listing results at
200 names and 16,000 name characters. Truncation is explicit. Conversation history
is capped at 256,000 serialized characters. Limits control loop work; this remains
a synchronous request, not a background job with cancellation or a global deadline.

The OpenAI adapter retains the configured `gpt-5.6` model, creates the client lazily,
and uses the Responses function-calling protocol. It preserves reasoning/output
items between turns, associates results with their original `call_id`, disables
parallel tool calls, and requests `store=False` with encrypted reasoning state.
See the [official function-calling documentation](https://developers.openai.com/api/docs/guides/function-calling).
Tests mock the provider boundary and never make paid API calls.

## Activity and data handling

Every registry dispatch—including unknown tools, bad arguments and denied
operations—requires an existing task and emits `tool_started`, then
`tool_completed` or `tool_failed`. Events include:

- Host-generated invocation ID and task ID.
- Registered tool name, status, start/completion timestamps and elapsed milliseconds.
- A summary consisting only of counts, exit code, timeout/truncation flags, or a
  fixed failure description; a terminal failure also includes an error code.

Unknown tool names are logged as `unregistered`, because arbitrary model-provided
names can themselves contain secrets. Model call IDs, arguments, filenames, file
contents, stdout/stderr, exception strings and reasoning are not logged. Failure
to write the start event prevents execution. Audit storage is now SQLite-backed and survives application restarts. See
[persistence.md](persistence.md) for conversations, explicit memories and schema details.

Allowed tool output is sent to OpenAI as context and the final model answer is
stored in the task's `response`; these are distinct from the metadata-only activity
log. Existing user goals remain in task records. Path policy is not content-based
secret detection: sensitive data should not be placed in otherwise allowed source
files. The agent never receives protected file contents through the tools.

## Verification and next milestone

Run `.venv/bin/python -m pytest -q` and `node --test tests/ui.test.cjs`.
The tests cover existing APIs/tools/UI, schemas, real safe-tool dispatch, scripted
model/tool exchanges, adversarial requests, limits, logging, provider failures,
and the Responses adapter. Bubblewrap may fail closed on restricted hosts; there
is still no fallback to running repository tests on the host.

Next: conversation selection and explicit memory management, then background execution with cancellation and
global time/cost limits, plus a verified sandbox deployment with memory/process/disk
quotas. Continue keeping edits disabled; any later editing milestone should start
with reviewable proposals and explicit approval.

Explicit self-editing is available only through the separate
[candidate workflow](self-edit.md). It does not expand this registry or the `/task`
agent permissions, and never applies candidate changes to the main checkout.
