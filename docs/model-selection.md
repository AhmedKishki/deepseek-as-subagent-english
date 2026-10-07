# DeepSeek model and reasoning selection

Model choice and reasoning depth are owned by user configuration, not by the calling orchestrator. The public delegation API takes no `model` argument and no reasoning argument. Every delegation uses the same configured model and depth.

The active provider model and reasoning depth live in `~/.deepseek-mcp/config.json` and can be changed at any time:

```json
{
  "model": "deepseek-v4-flash",
  "reasoning_effort": "low"
}
```

- `model` is any provider model ID the endpoint accepts. Change it when DeepSeek publishes a new revision, or when a compatible endpoint exposes a different name, without touching the MCP tool API.
- `reasoning_effort` is one of `provider-default`, `none`, `low`, `high`, or `max`. `provider-default` sends no reasoning controls; `none` disables DeepSeek thinking; the other values enable thinking at that effort.

Both values are validated when the configuration loads. An invalid model ID or an unknown effort value fails closed before any provider request.

## Environment fallback

If a config key is absent, deepseek-mcp falls back to the matching environment variable and then to its built-in default:

- `model`: config `model` → `DEEPSEEK_MODEL` → `deepseek-v4-flash`
- `reasoning_effort`: config `reasoning_effort` → `DEEPSEEK_REASONING_EFFORT` → endpoint default. The endpoint default is `low` for the official DeepSeek endpoint and `provider-default` for any other OpenAI-compatible endpoint, so an old config that never set a depth does not start sending an effort value a gateway might reject. The default model is DeepSeek Flash at low reasoning.

Environment values are validated exactly like config values.

## Background jobs

A background job keeps the configured model, reasoning depth, and capability frozen when it starts. Steering messages do not change them for a running job. To change the model or depth, edit the config and start a new job.

## Deprecated Flash/Pro keys

Earlier releases exposed `flash` and `pro` routing slots and let the orchestrator pick one per delegation. That host-side selection is removed. For upgrade compatibility the old keys are still accepted but deprecated:

- Active model: `model` → `flash` → `DEEPSEEK_MODEL` → default.
- Active depth: `reasoning_effort` → `flash_reasoning_effort` → `DEEPSEEK_REASONING_EFFORT` → default.

`pro` and `pro_reasoning_effort` are validated but ignored. Migrate to `model` and `reasoning_effort` and remove the old keys.
