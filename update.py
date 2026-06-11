"""Hourly silicon system update checks + the brain-driven apply.

The updater does not mutate the codebase mechanically. It fetches the latest
Glass release metadata, compares it with ``silicon.info``, and when the local
version is behind it spawns a dedicated, detached **update brain** (its own
claude session, no permission prompts — the same way the silicon itself is
initiated) which diffs the codebases, reads the release description, applies
the update, and bumps ``silicon.info`` to the exact new version.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
DOTENV_FILE = PROJECT_ROOT / ".env"
ENV_PY_FILE = PROJECT_ROOT / "env.py"
GLASS_CONFIG_FILE = PROJECT_ROOT / ".glass.json"
SILICON_CONFIG_FILE = PROJECT_ROOT / "silicon.json"
SILICON_INFO_FILE = PROJECT_ROOT / "silicon.info"
UPDATE_STATE_FILE = PROJECT_ROOT / "core" / "interface_state" / "system_update.json"

DEFAULT_GLASS_SERVER_URL = "https://glass.teamofsilicons.com"
UPDATE_CHECK_INTERVAL_SECONDS = 60 * 60
UPDATE_AUTH_PASSWORD = os.environ.get(
    "SILICON_UPDATE_AUTH_PASSWORD",
    "silicon-update-shared-password-v1",
)
LATEST_PATH = "/api/v1/silicon-version/latest"
AUTH_KEY_PATH = "/api/v1/silicon-version/auth-key"
REQUEST_TIMEOUT = 30


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_dotenv(path: Path = DOTENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def _read_env_py(path: Path = ENV_PY_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    pattern = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(['\"])(.*?)\2\s*$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw)
        if match:
            values[match.group(1)] = match.group(3)
    return values


def _upsert_key_value(path: Path, key: str, value: str, *, python_string: bool = False) -> None:
    if python_string:
        rendered = f"{key} = {json.dumps(value)}"
    else:
        rendered = f"{key}={value}"

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    out: list[str] = []
    for line in lines:
        if pattern.match(line):
            out.append(rendered)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(rendered)

    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _persist_auth_key(auth_key: str) -> None:
    if not auth_key:
        return
    _upsert_key_value(DOTENV_FILE, "SILICON_UPDATE_AUTH_KEY", auth_key)
    _upsert_key_value(DOTENV_FILE, "GLASS_API_KEY", auth_key)
    if ENV_PY_FILE.exists():
        _upsert_key_value(ENV_PY_FILE, "GLASS_API_KEY", auth_key, python_string=True)


def _glass_config() -> dict[str, Any]:
    return _read_json(GLASS_CONFIG_FILE, {})


def _silicon_config() -> dict[str, Any]:
    return _read_json(SILICON_CONFIG_FILE, {})


def _server_url() -> str:
    dotenv = _read_dotenv()
    glass = _glass_config()
    silicon = _silicon_config()
    nested_glass = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    return (
        os.environ.get("GLASS_SERVER_URL")
        or dotenv.get("GLASS_SERVER_URL")
        or glass.get("server_url")
        or nested_glass.get("server_url")
        or DEFAULT_GLASS_SERVER_URL
    ).rstrip("/")


def _auth_key() -> str:
    dotenv = _read_dotenv()
    env_py = _read_env_py()
    glass = _glass_config()
    silicon = _silicon_config()
    nested_glass = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    for value in (
        os.environ.get("SILICON_UPDATE_AUTH_KEY"),
        os.environ.get("GLASS_API_KEY"),
        dotenv.get("SILICON_UPDATE_AUTH_KEY"),
        dotenv.get("GLASS_API_KEY"),
        env_py.get("SILICON_UPDATE_AUTH_KEY"),
        env_py.get("GLASS_API_KEY"),
        glass.get("api_key"),
        glass.get("silicon_api_key"),
        nested_glass.get("api_key"),
        nested_glass.get("silicon_api_key"),
    ):
        if value:
            return str(value).strip()
    return ""


def _identity_payload() -> dict[str, str]:
    dotenv = _read_dotenv()
    glass = _glass_config()
    silicon = _silicon_config()
    nested_glass = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    payload: dict[str, str] = {}
    candidates = {
        "silicon_id": (
            os.environ.get("SILICON_ID"),
            dotenv.get("SILICON_ID"),
            glass.get("silicon_id"),
            nested_glass.get("silicon_id"),
            silicon.get("silicon_id"),
        ),
        "silicon_username": (
            os.environ.get("SILICON_USERNAME"),
            dotenv.get("SILICON_USERNAME"),
            glass.get("silicon_username"),
            nested_glass.get("silicon_username"),
        ),
        "address": (
            os.environ.get("SILICON_ADDRESS"),
            dotenv.get("SILICON_ADDRESS"),
            glass.get("address"),
            nested_glass.get("address"),
            silicon.get("address"),
        ),
        "name": (
            os.environ.get("SILICON_NAME"),
            dotenv.get("SILICON_NAME"),
            silicon.get("name"),
        ),
    }
    for key, values in candidates.items():
        for value in values:
            if value:
                payload[key] = str(value).strip()
                break
    return payload


def _request_auth_key() -> str:
    payload = {"password": UPDATE_AUTH_PASSWORD}
    payload.update(_identity_payload())
    if not any(payload.get(k) for k in ("silicon_id", "silicon_username", "address", "name")):
        return ""

    response = requests.post(
        _server_url() + AUTH_KEY_PATH,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code not in {200, 201}:
        response.raise_for_status()
    body = response.json()
    auth_key = str(body.get("auth_key") or body.get("plaintext") or "").strip()
    _persist_auth_key(auth_key)
    return auth_key


def _fetch_latest_version() -> dict[str, Any] | None:
    auth_key = _auth_key() or _request_auth_key()
    if not auth_key:
        return None

    def do_get(key: str):
        return requests.get(
            _server_url() + LATEST_PATH,
            headers={"X-Silicon-Key": key},
            timeout=REQUEST_TIMEOUT,
        )

    response = do_get(auth_key)
    if response.status_code in {401, 403}:
        auth_key = _request_auth_key()
        if not auth_key:
            return None
        response = do_get(auth_key)

    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else None


def _local_version() -> str:
    info = _read_json(SILICON_INFO_FILE, {})
    return str(info.get("version") or "").strip() if isinstance(info, dict) else ""


def _latest_version_id(latest: dict[str, Any]) -> str:
    return str(latest.get("version_id") or latest.get("version") or "").strip()


def _update_message(latest: dict[str, Any], latest_version_number: str) -> str:
    update_description = str(latest.get("description") or "").strip()
    codebase_link = str(
        latest.get("codebase_url") or latest.get("codebase_link") or latest.get("download_url") or ""
    ).strip()
    return (
        "There has been a silicon system update. The updated version is: "
        f"{latest_version_number}. Your current version is in silicon.info.\n\n"
        "Just do a simple diff between the codebases and refer to the description "
        "below to understand the update, then update yourself to the latest version.\n\n"
        "The description of the update is:\n"
        f"{update_description}\n\n"
        "And the latest codebase is:\n"
        f"{codebase_link}\n\n"
        "Download it, diff it against your current files, and apply the changes — "
        "preserving your own memory, prompts, env.py, .glass.json, and local state. "
        "Make all the required decisions yourself. Once the update is successfully "
        "done, update the version number in the silicon.info file to exactly "
        f"{latest_version_number}.\n"
    )


def _apply_in_progress(state: dict[str, Any]) -> bool:
    """True when a previously spawned update brain is still alive."""
    pid = int(state.get("apply_pid") or 0)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _spawn_update_brain() -> int:
    """Launch ``update.py apply`` fully detached, so the update brain survives
    even if the silicon restarts itself mid-update. Returns the pid."""
    import subprocess
    import sys

    log_path = PROJECT_ROOT / "core" / "interface_state" / "system_update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "update.py"), "apply"],
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return proc.pid


def check_for_system_update(now: float | None = None) -> dict[str, str]:
    """Check Glass for a newer release; spawn the update brain when behind.

    Returns {} always — the update no longer rides a contact manager session;
    it runs in its own detached brain with no permission prompts.
    """
    now = time.time() if now is None else now
    state = _read_json(UPDATE_STATE_FILE, {"version": 1})
    last_checked = float(state.get("last_checked_at") or 0)
    if now - last_checked < UPDATE_CHECK_INTERVAL_SECONDS:
        return {}

    state["last_checked_at"] = now
    _write_json(UPDATE_STATE_FILE, state)

    try:
        latest = _fetch_latest_version()
    except Exception as exc:
        state["last_error"] = str(exc)
        _write_json(UPDATE_STATE_FILE, state)
        print(f"[Update] Error checking silicon version: {exc}", flush=True)
        return {}

    local_version = _local_version()
    if not latest:
        state.update({"local_version": local_version, "latest_seen_version": "", "last_error": ""})
        _write_json(UPDATE_STATE_FILE, state)
        return {}

    latest_version = _latest_version_id(latest)
    state.update({"local_version": local_version, "latest_seen_version": latest_version, "last_error": ""})

    if not latest_version or latest_version == local_version:
        state["last_triggered_version"] = ""
        _write_json(UPDATE_STATE_FILE, state)
        return {}

    already_triggered = state.get("last_triggered_version") or state.get("last_notified_version")
    if already_triggered == latest_version or _apply_in_progress(state):
        _write_json(UPDATE_STATE_FILE, state)
        return {}

    state["last_triggered_version"] = latest_version
    state["apply_pid"] = _spawn_update_brain()
    _write_json(UPDATE_STATE_FILE, state)
    print(
        f"[Update] {local_version or '?'} → {latest_version}: update brain spawned "
        f"(pid {state['apply_pid']})",
        flush=True,
    )
    return {}


def trigger_system_update_check(*, force: bool = True) -> dict[str, str]:
    """Run the same update check on demand for CLI-triggered checks."""
    now = time.time() + UPDATE_CHECK_INTERVAL_SECONDS if force else None
    return check_for_system_update(now=now)


# ---------------------------------------------------------------------------
# The update brain — a dedicated claude session that manages the whole update.
# Runs in its own detached process (see _spawn_update_brain), exactly like the
# silicon is initiated: no permission prompts, its own session, full autonomy.
# ---------------------------------------------------------------------------
def _claude_cmd() -> str:
    import platform
    import shutil as _shutil

    if platform.system() == "Windows":
        return _shutil.which("claude") or _shutil.which("claude.cmd") or "claude"
    return "claude"


def _run_update_brain_once(cmd: list[str], message: str) -> int:
    import subprocess

    proc = subprocess.run(
        cmd,
        input=message,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=2 * 60 * 60,
    )
    return proc.returncode


def apply_update() -> int:
    """Fetch the latest release and hand the whole update to the update brain."""
    import uuid

    from prompts.DNA import get_update_prompt

    latest = _fetch_latest_version()
    if not latest:
        print("[Update] No published version to apply.", flush=True)
        return 0
    latest_version = _latest_version_id(latest)
    local_version = _local_version()
    if not latest_version or latest_version == local_version:
        print(f"[Update] Already on {local_version or 'unversioned'} — nothing to apply.", flush=True)
        return 0

    message = _update_message(latest, latest_version)
    prompt_file = PROJECT_ROOT / "sessions" / "system_update_prompt.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(get_update_prompt(), encoding="utf-8")

    state = _read_json(UPDATE_STATE_FILE, {"version": 1})
    session_id = str(state.get("brain_session_id") or "").strip()
    base = [
        _claude_cmd(), "-p",
        "--system-prompt-file", str(prompt_file),
        "--dangerously-skip-permissions",
    ]

    print(f"[Update] Applying {local_version or '?'} → {latest_version} via update brain…", flush=True)
    rc = -1
    if session_id:
        rc = _run_update_brain_once(base + ["--resume", session_id], message)
    if rc != 0:
        session_id = str(uuid.uuid4())
        state["brain_session_id"] = session_id
        _write_json(UPDATE_STATE_FILE, state)
        rc = _run_update_brain_once(base + ["--session-id", session_id], message)

    after = _local_version()
    state = _read_json(UPDATE_STATE_FILE, {"version": 1})
    state.update({"apply_pid": 0, "local_version": after, "last_apply_rc": rc})
    _write_json(UPDATE_STATE_FILE, state)
    if after == latest_version:
        print(f"[Update] Done — now on {after}.", flush=True)
    else:
        print(
            f"[Update] Brain finished (rc={rc}) but silicon.info reports "
            f"{after or 'unversioned'} (expected {latest_version}).",
            flush=True,
        )
    return 0 if after == latest_version else (rc or 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Silicon system update check / apply.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["check", "apply"],
        default="check",
        help="check: compare versions and spawn the update brain if behind (default). "
        "apply: run the update brain in this process.",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Respect the hourly throttle instead of forcing the check.",
    )
    args = parser.parse_args(argv)
    if args.command == "apply":
        return apply_update()
    result = trigger_system_update_check(force=not args.no_force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
