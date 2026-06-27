import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glass_agent


class GlassAgentDependenciesTest(unittest.TestCase):
    def test_dependency_report_includes_pip_and_runtime_cli_versions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text("requests\nwebsockets>=12\n", encoding="utf-8")

            def latest_pypi(name):
                return {
                    "requests": ("2.0.0", ""),
                    "websockets": ("13.0", ""),
                    "silicon-cli": ("1.0.17", ""),
                }[name]

            def latest_npm(name):
                return {
                    "@anthropic-ai/claude-code": ("1.0.0", ""),
                    "@openai/codex": ("2.0.0", ""),
                    "silicon-browser": ("3.0.0", ""),
                    "@teamofsilicons/silicon-interface-cli": ("4.0.0", ""),
                }[name]

            with mock.patch.object(
                glass_agent, "_installed_python_version", side_effect=lambda name: {"requests": "1.0.0"}.get(name, "")
            ), mock.patch.object(
                glass_agent, "_latest_pypi_version", side_effect=latest_pypi
            ), mock.patch.object(
                glass_agent, "_npm_global_versions", return_value=({"@openai/codex": "2.0.0"}, "")
            ), mock.patch.object(
                glass_agent, "_version_from_command", return_value=""
            ), mock.patch.object(
                glass_agent, "_latest_npm_version", side_effect=latest_npm
            ), mock.patch.object(
                glass_agent, "_resolve_command", return_value=""
            ), mock.patch.object(
                glass_agent, "_command_identity", return_value=""
            ), mock.patch.object(
                glass_agent, "_latest_github_main", return_value=("main@abc123", "")
            ):
                report = glass_agent.dependency_report(root)

        by_name = {p["name"]: p for p in report["packages"]}
        self.assertEqual(by_name["requests"]["status"], "outdated")
        self.assertEqual(by_name["websockets"]["status"], "missing")
        self.assertEqual(by_name["@openai/codex"]["status"], "current")
        self.assertEqual(by_name["silicon-browser"]["status"], "missing")
        self.assertEqual(by_name["silicon-interface"]["package"], "@teamofsilicons/silicon-interface-cli")
        self.assertEqual(by_name["silicon"]["manager"], "script")
        self.assertEqual(by_name["glass"]["manager"], "script")
        self.assertEqual(report["summary"]["outdated"], 1)
        self.assertEqual(report["summary"]["missing"], 6)


if __name__ == "__main__":
    unittest.main()
