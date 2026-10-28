# Security model

`deepseek-as-subagent` treats model-generated tool calls as untrusted input. The default coding API enables workspace-scoped file tools and bounded Bash so a new installation can complete coding tasks. The readonly APIs expose only Read, Glob, and Grep.

## Data sent to DeepSeek

Delegated task/context text, model messages, and outputs of files/tools chosen by DeepSeek are sent to the configured OpenAI-compatible endpoint. Do not delegate secrets or data that endpoint is not permitted to receive. The default endpoint is `https://api.deepseek.com`; non-loopback HTTP is rejected.

Trusted-host Bash receives no provider credential, proxy variable, or host `HOME`; its `HOME` is the workspace. It runs with the local user's permissions, so it is not an OS sandbox. Logs redact file contents, edit strings, and commands; usage records contain counts and timings rather than task text.

Execution receipts distinguish worker prose from server-produced notices and bounded tool evidence. Evidence contains workspace-relative source identities, query/command/output hashes, counts, exit/timeout outcomes, and completeness flags—not raw commands, file excerpts, stdout/stderr, or task/context text. It is returned to the requesting host, not added to persistent logs or a remote analytics service. Hashes can reveal equality and are not encryption. Receipts do not establish semantic correctness, capture a stable workspace snapshot, or audit all data egress; missing observations are reported as unknown. A model's completion or a command's zero exit must not substitute for host acceptance checks.

Provider calls run in an isolated Python child with private stdio pipes. The credential is sent over that private input pipe, never command-line arguments or inherited environment. Workspace cwd/PYTHONPATH/user-site and `.pth` processing are excluded from helper imports. File-mutation configurations fail closed when the runtime or installed package is inside the delegated workspace.

On POSIX, the server and provider child disable core dumps before reading credentials. Linux provider children also have a 1 GiB hard address-space limit; macOS relies on the platform-independent decoded-response cap. Windows processes set WER's `NOHEAP` flag before accepting work.

Configuration reads are capped at 1 MiB, reject duplicate keys, and reject unsupported keys. POSIX config directories/files must be private (`0700` / `0600`). Windows real API keys must be supplied by `DEEPSEEK_API_KEY`, not persisted in `config.json`.

## Capabilities and Bash execution

Coding APIs freeze this tool set before the model starts:

```json
{"allowed_tools":["Read","Write","Edit","Bash","Glob","Grep","NotebookEdit"]}
```

Readonly APIs freeze only `Read`, `Glob`, and `Grep`. Steering and model tool requests cannot switch either profile. Path tools resolve targets under the configured workspace, reject outward symlinks, and exclude host agent/config and VCS control paths.

Coding Bash runs only through the existing `tool_process` / `tool_child` boundary. The trusted-host executor has workspace cwd, disabled stdin, bounded stdout/stderr, timeout, command checks, process-tree cleanup, and credential isolation. It supports macOS, Linux, and Windows.

Bash is intentionally outside the crash-safe mutation journal. Shell commands may modify, rename, delete, or generate workspace files directly, and those changes are not represented by `get_deepseek_recovery`. The journal/recovery guarantee applies to mutations committed through Write, Edit, and NotebookEdit. After an interrupted coding run in which Bash may have executed, the host must inspect the workspace independently before continuing or retrying work.

## External MCP servers (opt-in)

The subagent connects to no external MCP server by default and never inherits the
orchestrator's MCP configuration. Opt-in servers are read-only and are declared
in a separate private file (`~/.deepseek-mcp/external-mcp.json`, path overridable
by `DEEPSEEK_EXTERNAL_MCP_CONFIG`), loaded per delegation. The permissions file
must be outside the delegated workspace so file tools cannot alter their grants.
POSIX config
directories/files must be private (`0700`/`0600`); reads are capped at 512 KiB and
reject duplicate or unsupported keys.

Every server and tool is explicitly listed. The host publishes a namespaced
schema (`ext__<server>__<tool>`) whose `input_schema` is server-owned. Names
carrying a Git/VCS identifier and explicit repository-write tool names are
refused; this is defense in depth, not an exhaustive Git-alias filter, and the
local read-only allowlist remains authoritative. A tool allowlist and input schema
are not remote resource ACLs: use read-only, resource-scoped service credentials,
and bind any filesystem endpoint to the worker's checkout. Approved queries must
stay within the assigned remote scope and never bypass local file boundaries.
Only HTTPS streamable HTTP is
accepted, plus plain HTTP for loopback hosts. There is no stdio transport and no
gateway mode.

Remote tool annotations are **not** a security guarantee. The local
`read_only: true` declaration is authoritative, and the isolated child
additionally requires `readOnlyHint` to be exactly true and `destructiveHint` not
to be true, failing closed otherwise. The child pages `tools/list` under a
bounded page count with cursor-cycle refusal, so a configured tool need not be on
the first page. A remote server may still be malicious or return misleading data;
external output is returned to the model as explicitly marked untrusted data,
never as server or agent instructions, and it is never acceptance evidence.

Credentials are never stored in the external config and are never forwarded
automatically. A server may declare header environment-variable names; the host
resolves those variables and passes the values only over the child's private
stdin pipe. The model never sees the URL, headers, or credentials, and per-call
children receive a minimal environment without `HOME`, `PYTHONPATH`, or proxy
variables. The HTTP client disables ambient environment trust and redirect
following.

The child requests uncompressed bodies (`Accept-Encoding: identity`), refuses any
other `Content-Encoding` before decoding, and bounds the raw wire body, so a
compressed body cannot expand past the response cap. It applies the same
Linux address-space/core-dump hardening as the provider child.

Each call runs in an isolated, cancellable child with a monotonic deadline,
bounded request/output caps, and process-group cleanup on cancellation or parent
loss. It inherits the workspace execution lease descriptor like a local tool
subprocess, so it cannot outlive the delegation lease. External calls are bounded
per run and count against the normal tool-call budget. The lease coordinates the
local worker, not remote resources: external side effects are not covered by
local file locks or the mutation journal, are outside `get_deepseek_recovery`,
and must be assessed by the host independently.

## Concurrency, recovery, and lifecycle

The host-only `deepseek-worktree` CLI creates separate branches/checkouts below
`<orchestrator-workspace>/.deepseek-as-subagent/run-<random>/` and binds each
`serve` process to that random directory itself. Host-only manifests and hooks
live separately under `.deepseek-as-subagent/.state/<same-random-name>/`.
Parent and instance directories are private;
creation requires Git ignore rules. File tools reject controller storage below
their assigned root, while a worker inside it can access its own checkout only.
Manifests and hooks stay outside that assigned checkout.
Managed worktree mode keeps Bash available but injects a server-owned policy
fixing the worker to its checkout and branch, forbidding access outside them, and
forbidding Git in any form, including read-only commands and Git tools through
MCP; the host runs integration and commits. This is a trust boundary, not an
OS-enforced restriction: the worker is trusted to follow the policy. Bash runs
on the trusted host; checkout/branch restrictions are instructions, not shell
confinement. File-tool path
confinement still applies, including exclusion of `.git`. Worktrees share Git
metadata and do not isolate arbitrary host processes. This mode is not an OS
sandbox and does not protect against a compromised runtime or another process
running as the same user. Lifecycle manifests are host-owned coordination
records, not signed security attestations.

An OS-backed lease keyed by workspace filesystem identity prevents concurrent delegation against the same workspace. Different workspaces run independently. The lease is released after the active execution and its child processes finish. The lease coordinates DeepSeek MCP executions only; it cannot stop the host agent, an IDE, the user, or another local process from editing the same files. While a coding background job is running, the host should steer, query, or cancel that job instead of independently mutating the same workspace, then resume host-side edits after the job reaches a terminal state.

Before `Write`, `Edit`, or `NotebookEdit` publishes an atomic replacement, the tool child persists a private mutation intent. `get_deepseek_recovery` audits records and the host must verify files then call `acknowledge_deepseek_mutations` with exact IDs. New mutation-capable delegation fails closed while records remain unacknowledged.

Each delegated run is wall-clock bounded to 18,000 seconds by default and 172,800 seconds maximum. Provider HTTP calls run in fresh private-pipe children and are capped at 180 seconds. Cancellation wakes retry backoff and terminates in-flight provider or local-tool subprocesses.

## Installation supply chain

Install from a reviewed tag or commit. Supported installers and CI use the same hash-locked `requirements.lock`; project installation disables dependency resolution and build isolation. Installers stage and validate a fresh generation before replacing registration, so failures preserve the active runtime.

The registry/introspection `Dockerfile` is a separate distribution artifact. It is not an execution sandbox and pins its base image and dependency lock.

## Reporting vulnerabilities

Do not include credentials or private workspace content in a public issue. Use the repository's private security-reporting channel when available.
