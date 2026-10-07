# Future work

The current single-subagent execution model is sufficient for now. These items are deferred, not implemented. Keep existing permissions, workspace leases, resource limits, and mutation recovery intact until a replacement is implemented and tested.

## Concurrent subagents

- [ ] Add a bounded multi-job scheduler with a user-configured concurrency limit, per-job status, steering, cancellation, and results. Keep the model and reasoning depth user-controlled.
- [ ] Give each subagent one distinct job, clear sequential instructions, explicit read/write scopes, and acceptance criteria. Enforce permissions server-side; acquiring a lock must never grant additional permissions.
- [ ] Manage shared-read/exclusive-write locks in the server around file-tool operations, with bounded waiting, cancellation, fair scheduling, and reliable cleanup on failure. Tool subprocesses are short-lived and must not own persistent coordination state.
- [ ] Track the file version each job read and reject edits based on stale versions. Existing atomic mutation checks protect the mutation itself, not the earlier Read → reasoning → Edit interval.
- [ ] Use stable canonical-path lock identities and handle aliases safely. Inode-only locking is insufficient because atomic edits replace inodes.
- [ ] Make mutation journaling and recovery concurrency-aware before relaxing the whole-workspace execution lease. Preserve fail-closed handling of uncertain mutations and prevent one job from acknowledging another job's records.
- [ ] Disable Bash for shared-workspace concurrent jobs initially. Preserve command execution through a separate exclusive-workspace or isolated-worktree mode; file-tool locks cannot govern arbitrary shell access.
- [ ] Keep non-overlapping write ownership as the default. Document that locks coordinate participating server operations, not editors, the orchestrator, external processes, or separate uncoordinated servers.
- [ ] Test concurrent reads, competing writes, stale edits, path aliases, lock starvation, cancellation, worker crashes, recovery, and external changes before enabling concurrency.

### Kilo reference

Borrow Kilo's concurrent job lifecycle, permission rules, keyed operation locks, and loop guards. Its agent messages are advisory, its cooperating edit locks are process-local, and worktrees isolate checkouts rather than eliminate integration conflicts. Do not treat those mechanisms as a universal shared-workspace reader/writer lock or read-version guarantee.

Research baseline: [Kilo source at b1e7f34](https://github.com/Kilo-Org/kilocode/tree/b1e7f34a4ce9fac0519714116f06fb553e185c56). Relevant implementations: [edit semaphore](https://github.com/Kilo-Org/kilocode/blob/b1e7f34a4ce9fac0519714116f06fb553e185c56/packages/opencode/src/tool/edit.ts), [conditional file mutation](https://github.com/Kilo-Org/kilocode/blob/b1e7f34a4ce9fac0519714116f06fb553e185c56/packages/core/src/file-mutation.ts), and [session loop detection](https://github.com/Kilo-Org/kilocode/blob/b1e7f34a4ce9fac0519714116f06fb553e185c56/packages/opencode/src/session/processor.ts).

## Earlier detection of unproductive loops

- [ ] Detect repeated identical tool calls and consecutive malformed tool arguments, using Kilo's threshold-three guards as a starting point. Define bounded exceptions for legitimate repetition or steering.
- [ ] Stop unproductive runs with a clear result for the orchestrator rather than leaving non-interactive workers waiting for approval. Keep turn, tool-call, time, and resource budgets as independent backstops.

## Live observability and inspection

- [ ] Design an opt-in local inspection service with a small browser UI and a bounded event stream. Let the user inspect a running DeepSeek job rather than seeing only its final answer. Evaluate an MCP-owned loopback HTTP service or a separate observer process; neither may create a second executor or bypass workspace leases.
- [ ] Record server-observed lifecycle events with job IDs, ordered sequence numbers, timestamps, model/effort configuration, turns, tool categories, command exit/timeout outcomes, limits, retries, failures, and recovery state. Show elapsed time and remaining budgets so active work can be distinguished from a stalled run. Label provider-reported usage separately from host-observed facts.
- [ ] Keep observable actions, worker messages, and server evidence distinct. Do not present worker claims as verified results, or promise access to hidden model reasoning. Link the live history to the final evidence receipt and show gaps, dropped events, and reconnect/retention limits explicitly.
- [ ] Redact credentials, raw commands, file contents, task/context text, and sensitive paths by default. Any richer trace capture must require explicit user opt-in, bounded private storage, a retention policy, and deletion controls; never silently send traces to a remote analytics service.
- [ ] Bind locally, authenticate access, validate browser origins, and prevent the UI from reading arbitrary files or serving outward symlinks. Keep observation read-only initially; any later steering/cancellation must use the existing permission and job-control APIs, never new execution privileges.
- [ ] Test concurrent viewers, slow consumers/backpressure, reconnects, cancellation, worker/server crashes, bounded memory/disk use, and secret redaction before enabling the service. No inspector may expose provider credentials or weaken mutation recovery.

## External MCP servers

- [ ] Add user-configured outbound MCP connections for the subagent, with explicit server/tool allowlists, credential isolation, bounded timeouts, cancellation, and lifecycle management.
- [ ] Preserve read-only boundaries and define how remote side effects interact with permissions and concurrency. Do not assume external MCP tools participate in local file locks or mutation recovery.
