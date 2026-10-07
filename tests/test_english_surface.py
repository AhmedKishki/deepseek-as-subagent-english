"""English fork instructions and fresh installer defaults."""
import json
from pathlib import Path
import re
import subprocess
import unittest

from deepseek_mcp.agent_loop import SYSTEM_PROMPT_TEMPLATE


ROOT = Path(__file__).resolve().parents[1]


class EnglishSurfaceTests(unittest.TestCase):
    def test_installers_override_permissive_inherited_umask(self):
        for path in ("install.sh", "adapters/codex/install.sh"):
            with self.subTest(installer=path):
                source = (ROOT / path).read_text()
                prologue = source.split("PROJECT_ROOT=", 1)[0]
                result = subprocess.run(
                    ["bash", "-c", "umask 0002\n" + prologue + "\numask"],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(int(result.stdout.strip(), 8), 0o077)

    def test_subagent_prompt_requires_english_and_one_job(self):
        self.assertIn("Communicate with the parent orchestrator in English", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("one assigned job", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("sequential instructions", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("explicitly requested deliverables", SYSTEM_PROMPT_TEMPLATE)

    def test_installers_default_to_user_owned_flash_low(self):
        for path in ("install.sh", "adapters/codex/install.sh"):
            with self.subTest(installer=path):
                source = (ROOT / path).read_text()
                match = re.search(r'\{\n  "api_key":.*?\n\}', source, re.DOTALL)
                self.assertIsNotNone(match)
                config = json.loads(match.group())
                self.assertEqual(config["model"], "deepseek-v4-flash")
                self.assertEqual(config["reasoning_effort"], "low")
                self.assertNotIn("flash", config)
                self.assertNotIn("pro", config)

    def test_skill_assigns_distinct_jobs_and_explains_parallelism_boundary(self):
        skill = (ROOT / "skills/delegate-to-deepseek/SKILL.md").read_text()
        self.assertIn("each subagent one clear, distinct job", skill)
        self.assertIn("numbered, sequential instructions", skill)
        self.assertIn("one execution per canonical workspace", skill)

    def test_host_instructions_lead_with_task_fit_and_keep_recovery_contract(self):
        from deepseek_mcp.host_instructions import HOST_INSTRUCTIONS

        prefix = HOST_INSTRUCTIONS[:512]
        self.assertTrue(HOST_INSTRUCTIONS.startswith("Task fit"))
        self.assertIn("get_deepseek_recovery", prefix)
        self.assertIn("acknowledge_deepseek_mutations", prefix)

    def test_codex_instructions_do_not_overclaim_bash_journaling(self):
        codex = (ROOT / "adapters/codex/instructions.md").read_text()
        self.assertNotIn("Every file mutation is durably journaled", codex)
        self.assertIn("Write/Edit/NotebookEdit mutations are durably journaled", codex)
        self.assertIn("Bash changes are not transaction-journaled", codex)


if __name__ == "__main__":
    unittest.main()
