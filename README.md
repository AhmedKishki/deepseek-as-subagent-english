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

**Parallelism:** one execution is allowed per canonical workspace. Independent jobs need separate workspaces/worktrees and server instances to run concurrently; otherwise run distinct jobs sequentially. Do not bypass the workspace lease.

## External MCP tools

The subagent **does not currently connect to external MCP servers** or inherit the orchestrator's MCP tools. That requires a separate MCP-client integration. Coding tools are Read, Write, Edit, Bash, Glob, Grep, and NotebookEdit; read-only tools are Read, Glob, and Grep.

Deferred concurrency, loop-detection, and external MCP work is tracked in [TODO.md](TODO.md). The current single-subagent model remains unchanged.

## Safety and recovery

Runs are bounded: `max_turns` defaults to 50 (maximum 100), tool calls are limited to 128 per run, and `max_run_seconds` defaults to 18,000 seconds (five hours; maximum 48 hours). Provider retries, individual commands, tokens, conversation size, and mutation output are also bounded. Reduce the configured turn/time limits for shorter jobs. There is not yet a dedicated no-progress detector; repeated mistakes stop at these limits, not necessarily earlier.

Coding Bash runs on the trusted host, **not in an OS sandbox**. Tasks and selected file/tool contents go to the configured provider. See [SECURITY.md](SECURITY.md).

After journaled mutations or an interrupted run, call `get_deepseek_recovery`, verify the reported files, then acknowledge their exact transaction IDs with `acknowledge_deepseek_mutations`. Bash changes are not journaled and require independent workspace inspection.

Disable delegation for a launch with `DEEPSEEK_MODE=off claude` or `DEEPSEEK_MODE=off codex`. Uninstall with `./uninstall.sh` or `bash adapters/codex/uninstall.sh`; user configuration and projects are preserved.

## License

[MIT](LICENSE). Original project: [PsChina/deepseek-as-subagent](https://github.com/PsChina/deepseek-as-subagent).
