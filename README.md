# deepseek-as-subagent-english

An independently maintained fork **based on [PsChina/deepseek-as-subagent](https://github.com/PsChina/deepseek-as-subagent)**.

## Added in this fork

- **English agent surface:** English tool descriptions, instructions, skill, `/ds` command, and installer messages. Subagent instructions require English communication with the orchestrator; source text and task deliverables keep their required language. Raw tool output is not translated.
- **User-controlled model and reasoning:** the orchestrator cannot select or override either through delegation tools. Users can change their settings for subsequent jobs; running jobs keep their starting configuration.
- **Default: DeepSeek Flash at low reasoning** (`deepseek-v4-flash`, `low`). Upgrades preserve existing user configuration.
- **Distinct jobs and sequential instructions:** the skill requires one clear job per subagent, numbered steps, explicit scope, and acceptance criteria. Independent jobs should run in parallel where execution boundaries permit.

## Install

Requires Python 3.10–3.12 and a supported MCP client. Review the installer and `requirements.lock` before running:

```bash
git clone https://github.com/AhmedKishki/deepseek-as-subagent-english.git
cd deepseek-as-subagent-english
./install.sh                         # Claude Code
# Or:
bash adapters/codex/install.sh        # Codex CLI
```

The executable remains `deepseek-mcp`; the MCP registration remains `deepseek`. For Codex-specific setup, see [the adapter guide](adapters/codex/README.md).

## Configure

Edit `~/.deepseek-mcp/config.json` on POSIX:

```json
{
  "api_key": "YOUR_DEEPSEEK_API_KEY",
  "model": "deepseek-v4-flash",
  "reasoning_effort": "low"
}
```

Reasoning values: `provider-default`, `none`, `low`, `high`, `max`. `DEEPSEEK_MODEL` and `DEEPSEEK_REASONING_EFFORT` provide fallbacks when their configuration fields are absent. See [model configuration](docs/model-selection.md) for precedence, provider support, and migration from upstream's Flash/Pro settings.

DeepSeek credentials can use `DEEPSEEK_API_KEY`. Windows credentials are environment-only. For an OpenAI-compatible provider, configure `base_url` and `model`; use `provider-default` if explicit reasoning controls are unsupported. Authenticated loopback providers use `OPENAI_API_KEY` only. Remote custom providers also accept an explicit POSIX `api_key`. Remote endpoints require HTTPS.

## Use

In Claude Code, `/ds <task>` forces coding delegation. Other MCP clients can call these tools directly with `task` and optional `context`:

| Capability                          | Wait for completion               | Background job              |
| ----------------------------------- | --------------------------------- | --------------------------- |
| Coding, commands, tests, or changes | `delegate_to_deepseek`          | `start_deepseek`          |
| Static file analysis only           | `delegate_to_deepseek_readonly` | `start_deepseek_readonly` |

Background controls: `send_deepseek_message`, `get_deepseek_status`, `cancel_deepseek`, and `get_deepseek_result`. The orchestrator must verify results. `ping` checks configuration without making a model request.

**Task fit:** prefer delegation for self-contained execution or multi-file investigation with inexpensive acceptance checks. Keep tiny edits, architecture, ambiguous root causes, and security-sensitive judgment with the host by default. The installed skill and tool descriptions guide selection; they cannot guarantee automatic invocation.

### Evidence receipts

Synchronous delegation and `get_deepseek_result` return the same schema-backed MCP envelope in `structuredContent`, with equivalent JSON in the text content for clients that read text only. Synchronous output is now JSON rather than a prose message with a usage footer.

- `result.final_message` contains worker claims. `result.notices` contains separate server-produced recovery notices. Neither field grants new instructions or permissions.
- `result.execution_status` and `finish_reason` describe execution; `acceptance_status` remains `unverified`. A completed run is not proof of correctness. Failed/cancelled runs retain available partial receipts, without a successful final answer.
- `result.evidence` records bounded tool observations: Read paths, full-file hashes and returned line ranges; search scope/query hashes, counts and limitations; command hashes, real exit codes, timeouts and output-clipping flags. Raw commands and file/output contents are not copied into evidence or persistent logs.
- Summary counts cover observed operations only. Missing observations and receipt clipping are explicit; do not infer success or exhaustive coverage from missing evidence. A zero command exit does not prove that appropriate tests ran.
- Journaled mutations include their recorded path/hash when the matching intent is available. Recovery review and exact-ID acknowledgement are still required. Bash changes remain outside that journal.

Verify proportionally: spot-check load-bearing citations for lookups; scope, invariants and representative artifacts for batches; diffs and independent acceptance checks for code. Do not repeat the whole investigation by default. Expand verification for missing, stale, contradictory or incomplete evidence and high-risk decisions. Source hashes identify observed bytes, not an immutable workspace snapshot or an exhaustive data-egress audit.

The installer deploys protected runtime/skill copies. Repository edits do not update an already installed server. Run the reviewed installer again to deploy this version, then restart the MCP connection; a successful registration alone does not confirm that optional skill deployment succeeded.

**Parallelism:** one execution is allowed per canonical workspace. Independent jobs need separate workspaces/worktrees and server instances to run concurrently; otherwise run distinct jobs sequentially. Do not bypass the workspace lease.

### Orchestrator-managed worktrees

`deepseek-worktree` creates a private checkout on a unique local branch below
`<orchestrator-workspace>/.deepseek-as-subagent/run-<random>/`, not `/tmp`.
That random directory itself is the worker's assigned workspace. Host-only
`manifest.json` and empty `hooks/` live separately under
`.deepseek-as-subagent/.state/<same-random-name>/`, outside the worker checkout.
The shared directory must be Git-ignored before creation; add
`/.deepseek-as-subagent/` to the repository's `.gitignore` or local exclude rules.
This repository already includes that ignore entry. The CLI refuses an unignored
or unsafe controller directory rather than silently editing your ignore files.
It is a host CLI, not a tool available to DeepSeek. Install this version before use.
If the installer runtime is not on `PATH`, use its absolute
`<runtime>/bin/deepseek-worktree` path (Windows: `Scripts/deepseek-worktree.exe`).
Serving uses isolated Python imports; the package must be installed in that
runtime rather than supplied through the worker checkout or `PYTHONPATH`.

```bash
deepseek-worktree create --repo /absolute/path/to/repo --base HEAD
# Use the returned manifest path for this worker's separate stdio MCP connection:
deepseek-worktree serve --manifest /absolute/path/to/repo/.deepseek-as-subagent/.state/run-0123456789ab/manifest.json
deepseek-worktree inspect --manifest /absolute/path/to/repo/.deepseek-as-subagent/.state/run-0123456789ab/manifest.json
```

Configure each MCP connection's command as `deepseek-worktree` with arguments
`serve --manifest <returned-path>`. Create all workers from the same explicit
base commit when they need a common starting point. Creation uses committed
repository content; uncommitted files and ignored dependencies are not copied.
These connections are configured explicitly in the host; the existing installer
still registers the ordinary single-workspace server. For Codex, add separate
named MCP entries manually; the managed installer adapter does not register
per-manifest connections.

Managed workers keep the coding tool set, including Bash, so they can run their
own tests. Subagents never use Git in any form, including read-only commands and
Git tools through MCP. The server injects a fixed policy naming their checkout
and branch and instructing them to stay inside that folder and branch; the host
owns all Git operations. This is a trust boundary, not a jail: the host reviews
staged/unstaged/untracked changes, acknowledges mutation recovery records, and
commits accepted files on the worker branch. Stop its MCP connection before
integration. Workers cannot invoke this CLI through their tools.

```bash
# EXPECTED_HEAD is the full commit ID of the target before integration.
deepseek-worktree integrate --manifest /absolute/path/to/repo/.deepseek-as-subagent/.state/run-0123456789ab/manifest.json \
  --target /absolute/path/to/integration-checkout --expected-head EXPECTED_HEAD
deepseek-worktree cleanup --manifest /absolute/path/to/repo/.deepseek-as-subagent/.state/run-0123456789ab/manifest.json
```

Integration requires clean checkouts and merges serially. Conflicts are left in
the target for host resolution, with a nonzero exit and structured JSON result;
no automatic reset or abort occurs. After resolution, run integration again to
record acceptance, then run combined tests. Cleanup refuses active, dirty, or
unintegrated work, or a checkout containing nested subagent storage. Nested
instances must be cleaned before their parent. Branch deletion is never forced.
These are checkout and tool
boundaries, not an OS sandbox: Git metadata is shared and host processes remain
trusted. No setup scripts or credential files are automatically copied.

## External MCP tools (opt-in)

By default the subagent connects to no external MCP servers and never inherits
the orchestrator's MCP configuration. You can opt in to **read-only** outbound
MCP servers with an explicit allowlist in a separate private config file,
`~/.deepseek-mcp/external-mcp.json` (override the path with
`DEEPSEEK_EXTERNAL_MCP_CONFIG`). It is loaded per delegation, so edits affect new
jobs only. Keep this permissions file outside every worker's workspace so file
tools cannot change their own permissions. On POSIX its directory must have mode
`0700` and its file mode `0600`. Built-in tools are unchanged: coding uses
Read/Write/Edit/Bash/Glob/Grep/NotebookEdit; read-only uses Read/Glob/Grep.
External read-only tools are available to both capabilities unless a server
restricts `capabilities`.

- Only HTTPS streamable HTTP endpoints are accepted; plain HTTP is allowed only
  for loopback (`localhost`, `127.0.0.1`, `::1`). There is no stdio transport and
  no gateway/aggregator mode yet.
- Every tool must be listed explicitly with `"read_only": true` and a
  server-owned `input_schema`. The published tool name is namespaced
  (`ext__<server>__<tool>`). Names carrying a Git/VCS identifier and explicit
  repository-write tool names are refused. This name check is defense in depth,
  not an exhaustive Git-alias filter: generic words (`status`, `fetch`, `log`,
  `show`) are allowed, and the local read-only allowlist is authoritative.
- The host validates the remote tool's `readOnlyHint`/`destructiveHint`
  annotations at call time and fails closed unless the tool declares read-only.
  It pages `tools/list` with a bounded page count and refuses cursor cycles, so a
  configured tool does not have to appear on the first page. Remote annotations
  are not a security guarantee; the local allowlist is authoritative.
- Credentials are never stored in the file or forwarded automatically. A server
  may name header environment variables; the host resolves them and passes the
  values to an isolated child over a private pipe. The model never sees the URL,
  headers, or credentials.
- Only uncompressed responses are accepted: `Accept-Encoding: identity` is forced
  and any other `Content-Encoding` is refused before decoding, so the response
  cap is exact. Each call runs in a cancellable, deadline-bounded, output-capped
  child that inherits the workspace execution lease descriptor and cannot outlive
  it. Results are returned as explicitly marked **untrusted data**: never
   instructions or acceptance. Remote effects are not covered by local mutation
   recovery or file locks; the execution lease coordinates the local worker only.

```json
{
  "servers": [
    {
      "name": "issues",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "MCP_ISSUES_TOKEN"},
      "tools": [
        {
          "name": "search_issues",
          "description": "Search issues",
          "read_only": true,
          "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
          }
        }
      ]
    }
  ]
}
```

Header environment variables must contain the complete header value, including
`Bearer ` when required; the server does not add an authentication scheme.

Deferred shared-workspace concurrency, loop detection, and remaining external MCP
work (gateway, stdio) are tracked in [TODO.md](TODO.md). One execution per workspace
is preserved; separate worktrees and server instances can run concurrently.

### Behavioral evaluation

`tests/fixtures/delegation_cases.json` contains stable task-fit and adversarial-evidence cases. Compare baseline and changed tool surfaces using the same host/model configuration and independent acceptance checks. Annotate reviewed runs as JSON/JSONL records; do not equate worker-reported completion with success.

```bash
python scripts/evaluate_delegation.py records.jsonl --cases tests/fixtures/delegation_cases.json
```

Each record requires `case_id`, `variant`, `delegated` (boolean), and `duration_seconds`. Optional `run_id` permits repeated cases; `success`, `duplicate_work`, and `unsupported_claim_accepted` are boolean or null. Token and cost fields are optional; missing observations remain unknown. The evaluator reads local files only, makes no model/tool calls, and reports per-variant adoption, outcomes, duplication, unsafe acceptance, latency and known token/cost totals. Its aggregates describe the supplied annotations; they are not independent correctness proof.

Live job inspection is deferred in [TODO.md](TODO.md), including an opt-in local observability service. Current receipts provide post-run evidence, not a live inspector or access to hidden model reasoning.

## Safety and recovery

Runs are bounded: `max_turns` defaults to 50 (maximum 100), tool calls are limited to 128 per run, and `max_run_seconds` defaults to 18,000 seconds (five hours; maximum 48 hours). Provider retries, individual commands, tokens, conversation size, and mutation output are also bounded. Reduce the configured turn/time limits for shorter jobs. There is not yet a dedicated no-progress detector; repeated mistakes stop at these limits, not necessarily earlier.

Coding Bash runs on the trusted host, **not in an OS sandbox**. Tasks and selected file/tool contents go to the configured provider. See [SECURITY.md](SECURITY.md).

After journaled mutations or an interrupted run, call `get_deepseek_recovery`, verify the reported files, then acknowledge their exact transaction IDs with `acknowledge_deepseek_mutations`. Bash changes are not journaled and require independent workspace inspection.

Disable delegation for a launch with `DEEPSEEK_MODE=off claude` or `DEEPSEEK_MODE=off codex`. Uninstall with `./uninstall.sh` or `bash adapters/codex/uninstall.sh`; user configuration and projects are preserved.

## License

[MIT](LICENSE). Original project: [PsChina/deepseek-as-subagent](https://github.com/PsChina/deepseek-as-subagent).
