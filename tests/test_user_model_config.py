from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deepseek_mcp.config import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    PROVIDER_DEFAULT_REASONING_EFFORT,
    REASONING_EFFORT_OPTIONS,
    Config,
)


class UserModelConfigTests(unittest.TestCase):
    def _load_from_data(
        self, data: dict, *, environment: dict[str, str] | None = None
    ) -> Config:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with (
                patch("deepseek_mcp.config._load_data", return_value=data),
                patch("deepseek_mcp.config._load_api_key", return_value="sk-test"),
                patch("deepseek_mcp.config._load_workspace", return_value=workspace),
                patch.dict(os.environ, environment or {}, clear=True),
            ):
                return Config.load()

    def test_model_and_reasoning_depth_have_user_owned_defaults(self) -> None:
        config = self._load_from_data({})
        self.assertEqual(config.model, DEFAULT_MODEL)
        self.assertEqual(DEFAULT_MODEL, "deepseek-v4-flash")
        self.assertEqual(config.reasoning_effort, DEFAULT_REASONING_EFFORT)
        self.assertEqual(DEFAULT_REASONING_EFFORT, "low")
        self.assertEqual(
            REASONING_EFFORT_OPTIONS,
            ("provider-default", "none", "low", "high", "max"),
        )

    def test_generic_endpoint_defaults_to_provider_default_without_config(self) -> None:
        config = self._load_from_data({"base_url": "http://127.0.0.1:8080/v1"})
        self.assertEqual(config.reasoning_effort, PROVIDER_DEFAULT_REASONING_EFFORT)

    def test_explicit_depth_applies_to_generic_endpoint(self) -> None:
        config = self._load_from_data(
            {"base_url": "http://127.0.0.1:8080/v1", "reasoning_effort": "high"}
        )
        self.assertEqual(config.reasoning_effort, "high")

    def test_config_owns_model_and_reasoning_depth(self) -> None:
        config = self._load_from_data(
            {"model": "vendor-model-v9", "reasoning_effort": "max"}
        )
        self.assertEqual(config.model, "vendor-model-v9")
        self.assertEqual(config.reasoning_effort, "max")

    def test_environment_fallback_applies_when_config_keys_are_missing(self) -> None:
        config = self._load_from_data(
            {},
            environment={
                "DEEPSEEK_MODEL": "env-model",
                "DEEPSEEK_REASONING_EFFORT": "low",
            },
        )
        self.assertEqual(config.model, "env-model")
        self.assertEqual(config.reasoning_effort, "low")

    def test_config_keys_win_over_environment_fallback(self) -> None:
        config = self._load_from_data(
            {"model": "config-model", "reasoning_effort": "none"},
            environment={
                "DEEPSEEK_MODEL": "env-model",
                "DEEPSEEK_REASONING_EFFORT": "max",
            },
        )
        self.assertEqual(config.model, "config-model")
        self.assertEqual(config.reasoning_effort, "none")

    def test_invalid_environment_values_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_MODEL"):
            self._load_from_data({}, environment={"DEEPSEEK_MODEL": " bad "})
        with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_REASONING_EFFORT"):
            self._load_from_data(
                {}, environment={"DEEPSEEK_REASONING_EFFORT": "ultra"}
            )

    def test_invalid_config_values_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "model"):
            self._load_from_data({"model": ""})
        with self.assertRaisesRegex(RuntimeError, "reasoning_effort"):
            self._load_from_data({"reasoning_effort": "ultra"})

    def test_deprecated_flash_slot_is_used_when_new_model_key_is_absent(self) -> None:
        config = self._load_from_data(
            {
                "flash": "qwen3.8-27b-q3",
                "pro": "unsloth/Qwen...",
                "flash_reasoning_effort": "provider-default",
            }
        )
        self.assertEqual(config.model, "qwen3.8-27b-q3")
        self.assertEqual(config.reasoning_effort, "provider-default")

    def test_new_keys_win_over_deprecated_flash_slot(self) -> None:
        config = self._load_from_data(
            {
                "model": "active-model",
                "reasoning_effort": "low",
                "flash": "legacy-flash",
                "flash_reasoning_effort": "max",
            }
        )
        self.assertEqual(config.model, "active-model")
        self.assertEqual(config.reasoning_effort, "low")

    def test_deprecated_keys_are_still_validated(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "pro"):
            self._load_from_data({"model": "active", "pro": " pro "})
        with self.assertRaisesRegex(RuntimeError, "pro_reasoning_effort"):
            self._load_from_data({"model": "active", "pro_reasoning_effort": "ultra"})

    def test_reasoning_effort_hint_field_is_runtime_irrelevant(self) -> None:
        config = self._load_from_data(
            {
                "_reasoning_effort_options": "documentation-only",
                "reasoning_effort": "low",
            }
        )
        self.assertEqual(config.reasoning_effort, "low")

    def test_provider_default_is_an_accepted_depth(self) -> None:
        config = self._load_from_data({"reasoning_effort": "provider-default"})
        self.assertEqual(config.reasoning_effort, PROVIDER_DEFAULT_REASONING_EFFORT)


if __name__ == "__main__":
    unittest.main()
