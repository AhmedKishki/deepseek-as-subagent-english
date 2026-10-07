"""Stable MCP host instructions, separated to keep the server entrypoint small."""

HOST_INSTRUCTIONS = """
Respond to the user in English. All delegation tasks, context, and summaries are
handled in English.

Recovery records cover Write/Edit/NotebookEdit commits; trusted-host Bash changes
are not transaction-journaled. After any result with reported mutations—or
cancellation, disconnection, or restart—call `get_deepseek_recovery`, verify the
reported files, then call `acknowledge_deepseek_mutations(transaction_ids)` with
the exact reviewed IDs. If coding Bash may have run before an interruption,
inspect the workspace independently before continuing.
After every delegation, verify delegated output against the requested acceptance
criteria before relying on it.

API reference:
- `ping()`: check that the MCP server is available.
- `delegate_to_deepseek(task, context="")`: wait for full coding to finish; it
  cannot be steered, queried, or cancelled while running.
- `delegate_to_deepseek_readonly(task, context="")`: wait for file-only analysis
  with Read/Glob/Grep; it has no Bash or mutation tools and cannot be controlled
  while running.
- `start_deepseek(task, context="")` / `start_deepseek_readonly(task, context="")`:
  start a coding or read-only background job and return `job_id`.
- `get_deepseek_status(job_id)`: read a background job's state.
- `send_deepseek_message(job_id, message)`: add or correct its task instruction.
- `cancel_deepseek(job_id)`: cancel a background job.
- `get_deepseek_result(job_id)`: return its final result, or not-ready state.
- `get_deepseek_recovery()`: list unacknowledged mutations from coding work.
- `acknowledge_deepseek_mutations(transaction_ids)`: acknowledge exact reviewed IDs.
`task` states the goal and acceptance criteria; optional `context` supplies paths,
constraints, and project conventions. `job_id` comes from `start_*`.

Model and reasoning depth are user-owned:
- The delegation API takes no model or reasoning argument. The provider model ID
  and reasoning depth come only from the user's `~/.deepseek-mcp/config.json`
  (`model`, `reasoning_effort`), with `DEEPSEEK_MODEL` and
  `DEEPSEEK_REASONING_EFFORT` as environment fallbacks. Do not ask the user to
  pick a model per task and do not pass provider model IDs or effort values.
- A background job keeps the configured model, reasoning depth, and capability
  frozen at start; steering cannot change them.

Selection: use `delegate_*` when the host can wait for completion. Use `start_*`
when it needs steering, status, or cancellation. Use readonly only for pure
reading/search/review of existing files or text when no task step needs a command;
use coding for everything else or uncertainty. The API freezes the capability for
the job. Steering cannot enable Bash or mutation tools: if a readonly job later
needs either, cancel or finish it and create a new coding job.

Granularity: give each subagent one clear, distinct job with one outcome, explicit
scope, and independent acceptance criteria. Write numbered, sequential
instructions; do not bundle unrelated jobs into one delegation, and do not expect
the subagent to infer order or boundaries. Reading, implementing, and testing the
same change are sequential steps, not parallel jobs. True parallelism requires
separate workspaces and MCP server instances; never bypass the
one-execution-per-workspace lease.

DeepSeek cannot see host chat or project instructions; pass needed context
explicitly. One OS lease permits one DeepSeek execution per canonical workspace,
but it cannot prevent the host, IDE, or other processes from editing that workspace.
While a coding background job is running, the host should steer, query, or cancel
that job instead of independently mutating the same workspace; resume host-side
edits after the job reaches a terminal state. Background jobs/results are
process-local. New delegation fails closed while recovery records remain.
""".strip()
