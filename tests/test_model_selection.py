from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deepseek_mcp import server
from deepseek_mcp.config import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    Config,
)
from deepseek_mcp.execution_profile import CODING_PROFILE, READONLY_PROFILE


class ModelSelectionTests(unittest.TestCase):
    def _config(self, workspace: Path) -> Config:
        return Config("sk-test", workspace, allowed_tools=["Read"])

    def test_public_tools_expose_no_model_or_reasoning_override(self) -> None:
        for tool in (
            server.delegate_to_deepseek,
            server.delegate_to_deepseek_readonly,
            server.start_deepseek,
            server.start_deepseek_readonly,
        ):
            parameters = inspect.signature(tool).parameters
            self.assertNotIn("model", parameters)
            self.assertNotIn("reasoning_effort", parameters)
            self.assertNotIn("reasoning_depth", parameters)
            self.assertEqual(list(parameters), ["task", "context"])

    def test_config_defaults_are_model_and_reasoning_owned_by_user(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "deepseek-v4-flash")
        self.assertEqual(DEFAULT_REASONING_EFFORT, "low")

    def test_load_config_uses_config_owned_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir))
            config.model = "custom-provider-model"
            config.reasoning_effort = "max"
            with (
                patch.object(server.Config, "load", return_value=config),
                patch.object(
                    server, "configure_delegation", side_effect=lambda cfg, _profile: cfg
                ),
            ):
                loaded = server._load_config(CODING_PROFILE)

        self.assertIs(loaded, config)
        self.assertEqual(loaded.model, "custom-provider-model")
        self.assertEqual(loaded.reasoning_effort, "max")

    def test_load_config_ignores_any_caller_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(Path(tmpdir))
            config.model = "config-only"
            config.reasoning_effort = "none"
            with (
                patch.object(server.Config, "load", return_value=config),
                patch.object(
                    server, "configure_delegation", side_effect=lambda cfg, _profile: cfg
                ),
            ):
                loaded = server._load_config(READONLY_PROFILE)

        self.assertEqual(loaded.model, "config-only")
        self.assertEqual(loaded.reasoning_effort, "none")

    def test_background_job_receives_config_owned_selection_once(self) -> None:
        manager = Mock()
        manager.start.return_value = {"job_id": "job-1", "status": "running"}
        config = Mock(model="config-model", reasoning_effort="high")
        with (
            patch.object(server, "job_manager", manager),
            patch.object(server, "_load_config", return_value=config) as load_config,
        ):
            payload = server.start_deepseek("hard task")

        self.assertIn('"ok": true', payload)
        load_config.assert_called_once_with(CODING_PROFILE)
        manager.start.assert_called_once_with("hard task", "", config)

    def test_sync_delegation_prepares_config_owned_selection(self) -> None:
        config = Mock(model="config-model", reasoning_effort="high")
        with patch.object(server, "_load_config", return_value=config) as load_config:
            prepared, full_task = server._prepare_sync_request(
                "task", "context", CODING_PROFILE
            )

        self.assertIs(prepared, config)
        self.assertIn("context", full_task)
        load_config.assert_called_once_with(CODING_PROFILE)


if __name__ == "__main__":
    unittest.main()
