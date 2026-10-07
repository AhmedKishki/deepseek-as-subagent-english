---
description: Explicitly delegate to the DeepSeek sub-agent, bypassing Claude's automatic decision. Usage — /ds <task description>
---

# /ds — Delegate to DeepSeek

**Force** the task that follows to be delegated to DeepSeek, bypassing Claude's automatic decision ("delegate / do not delegate"). `/ds` always calls the full coding API; tasks that are strictly static file analysis should be handled by the main Agent via `delegate_to_deepseek_readonly`.

The model and reasoning depth come from the user's configuration only (`~/.deepseek-mcp/config.json`: `model`, `reasoning_effort`, or the `DEEPSEEK_MODEL` / `DEEPSEEK_REASONING_EFFORT` environment fallbacks). The orchestrator never selects or overrides either, and the delegation tools expose no model or reasoning argument.

## What you must do

1. Prepare `task` and `context` according to the `delegate-to-deepseek` skill:
   - Use Glob / LS to collect the file paths involved
   - Summarize project conventions (naming rules, output schema, boundaries)
   - State the success criteria
   - Do not pass a model or reasoning value; the user's configuration selects them

2. Call the `mcp__deepseek__delegate_to_deepseek` tool, passing the user's request as the task:

```
User input: $ARGUMENTS
```

3. After the tool returns, you **must verify**:
   - Read sampled artifact files
   - Check counts / schema sanity
   - On failure, follow the skill's fallback strategy

## What you must not do

- ❌ Do not ask the user "are you sure you want to delegate?" before calling — typing `/ds` is already an explicit instruction
- ❌ Do not pass a model or reasoning override, or otherwise try to choose the provider model — the tools do not accept one and the user's configuration decides
- ❌ Do not give up when the tool returns ERROR — retry or take over per the skill's fallback strategy
- ❌ Do not put API keys / credentials into task / context
