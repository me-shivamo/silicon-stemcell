import tempfile
import unittest
from pathlib import Path
from unittest import mock

import update


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SystemUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths = {
            "DOTENV_FILE": update.DOTENV_FILE,
            "ENV_PY_FILE": update.ENV_PY_FILE,
            "GLASS_CONFIG_FILE": update.GLASS_CONFIG_FILE,
            "SILICON_CONFIG_FILE": update.SILICON_CONFIG_FILE,
            "SILICON_INFO_FILE": update.SILICON_INFO_FILE,
            "UPDATE_STATE_FILE": update.UPDATE_STATE_FILE,
        }
        update.DOTENV_FILE = self.root / ".env"
        update.ENV_PY_FILE = self.root / "env.py"
        update.GLASS_CONFIG_FILE = self.root / ".glass.json"
        update.SILICON_CONFIG_FILE = self.root / "silicon.json"
        update.SILICON_INFO_FILE = self.root / "silicon.info"
        update.UPDATE_STATE_FILE = self.root / "state" / "system_update.json"

    def tearDown(self):
        for key, value in self.old_paths.items():
            setattr(update, key, value)
        self.tmp.cleanup()

    def test_update_mismatch_notifies_head_once_per_version(self):
        update.SILICON_INFO_FILE.write_text('{"version": "1.0"}\n', encoding="utf-8")
        latest = {
            "version_id": "1.1",
            "description": "new tools",
            "codebase_url": "https://glass.example/codebase.zip",
        }

        with mock.patch.object(update, "_fetch_latest_version", return_value=latest), mock.patch.object(
            update, "_head_manager_contact_id", return_value="carbon-a"
        ):
            first = update.check_for_system_update(now=4000)
            second = update.check_for_system_update(now=8000)

        self.assertIn("carbon-a", first)
        self.assertIn("updated version is: 1.1", first["carbon-a"])
        self.assertIn("new tools", first["carbon-a"])
        self.assertIn("https://glass.example/codebase.zip", first["carbon-a"])
        self.assertEqual(second, {})

    def test_fetch_latest_requests_and_stores_auth_key_when_missing(self):
        update.DOTENV_FILE.write_text("GLASS_SERVER_URL=https://glass.example\n", encoding="utf-8")
        update.ENV_PY_FILE.write_text('GLASS_API_KEY = ""\n', encoding="utf-8")
        update.SILICON_CONFIG_FILE.write_text('{"silicon_id": "si-1"}\n', encoding="utf-8")

        post_response = FakeResponse(201, {"auth_key": "scs_live_new"})
        get_response = FakeResponse(200, {"version_id": "1.1", "codebase_url": "https://code.zip"})
        with mock.patch.object(update.requests, "post", return_value=post_response) as post, mock.patch.object(
            update.requests, "get", return_value=get_response
        ) as get:
            latest = update._fetch_latest_version()

        self.assertEqual(latest["version_id"], "1.1")
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["silicon_id"], "si-1")
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Silicon-Key": "scs_live_new"})
        self.assertIn("SILICON_UPDATE_AUTH_KEY=scs_live_new", update.DOTENV_FILE.read_text(encoding="utf-8"))
        self.assertIn('GLASS_API_KEY = "scs_live_new"', update.ENV_PY_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
