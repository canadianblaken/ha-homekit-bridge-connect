"""Interactive setup wizard — probes HA + HomeKit proxy, writes config.toml and .env."""
from __future__ import annotations

import asyncio
import getpass
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import websockets

_DEFAULT_WS = "ws://homeassistant.local:8123/api/websocket"
_DEFAULT_HK = "http://localhost:8788/sse"

_BANNER = """\
haconnect needs two things:
  1. Home Assistant  — admin long-lived access token + WebSocket URL
  2. HomeKit proxy   — HomeClaw is the reference implementation
                       Any proxy exposing the homekit_* MCP tools works.
"""


def _ws_to_rest(ws_url: str) -> str:
    p = urlparse(ws_url)
    scheme = "https" if p.scheme == "wss" else "http"
    return f"{scheme}://{p.netloc}"


async def _probe_ha(ws_url: str, token: str) -> tuple[str, list[str]]:
    """Connect to HA, return (ha_version, [bridge_title, ...])."""
    ws = await websockets.connect(ws_url, max_size=4 * 1024 * 1024, open_timeout=10)
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("type") != "auth_required":
            raise ValueError(f"unexpected greeting: {hello!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        res = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if res.get("type") != "auth_ok":
            raise ValueError(res.get("message") or repr(res))
        ha_version = res.get("ha_version", "?")
    finally:
        await ws.close()

    bridges: list[str] = []
    try:
        rest = _ws_to_rest(ws_url)
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{rest}/api/config/config_entries/entry",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    entries = await resp.json()
                    bridges = [
                        e.get("title") or e.get("entry_id", "?")
                        for e in entries
                        if isinstance(e, dict) and e.get("domain") == "homekit"
                    ]
    except Exception:
        pass  # bridge list is informational; don't fail setup over it

    return ha_version, bridges


async def _probe_hk(sse_url: str) -> bool:
    """Return True if the HomeKit proxy responds to an HTTP request."""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                sse_url,
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"Accept": "text/event-stream"},
            ) as resp:
                return resp.status < 500
    except Exception:
        return False


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{label}{suffix}: ").strip()
    except EOFError:
        print("\nAborted.")
        sys.exit(0)
    return val or default


def run_setup(base_dir: Path = Path(".")) -> int:
    print(_BANNER)

    config_path = base_dir / "config.toml"
    env_path = base_dir / ".env"
    if config_path.exists():
        yn = input(f"{config_path} already exists. Overwrite? [y/N]: ").strip().lower()
        if yn != "y":
            print("Aborted.")
            return 0
        print()

    ws_url = _prompt("HA WebSocket URL", _DEFAULT_WS)
    try:
        token = getpass.getpass("HA admin token (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 0
    if not token:
        print("Token cannot be empty.", file=sys.stderr)
        return 1

    print("\nConnecting to HA…", end=" ", flush=True)
    ha_ok = True
    bridges: list[str] = []
    try:
        ha_version, bridges = asyncio.run(_probe_ha(ws_url, token))
        print(f"✓  (Home Assistant {ha_version})")
    except Exception as e:
        print(f"✗  ({e})")
        print(
            "  Could not reach HA — verify the URL and token, "
            "then run 'haconnect setup' again.",
            file=sys.stderr,
        )
        ha_ok = False

    if ha_ok:
        if bridges:
            print(f"\nFound {len(bridges)} HomeKit bridge(s):")
            for b in bridges:
                print(f"  • {b}")
            print(
                "These will be discovered at runtime — no further config needed.\n"
                "(If this list looks wrong, fix your HA HomeKit integration first.)"
            )
        else:
            print(
                "\nNo per-room HomeKit bridges found.\n"
                "The Move feature requires one bridge per HA area.\n"
                "Add-on integrations → HomeKit → create one bridge per room."
            )
        print()

    hk_url = _prompt("HomeKit proxy SSE URL", _DEFAULT_HK)

    print("\nTesting HomeKit proxy…", end=" ", flush=True)
    hk_ok = asyncio.run(_probe_hk(hk_url))
    if hk_ok:
        print("✓")
    else:
        print("✗  (not reachable)")
        print(
            "  HomeClaw (or your HomeKit proxy) must be running before you use haconnect.\n"
            "  The config will be saved — start the proxy and run 'haconnect' when ready.",
            file=sys.stderr,
        )

    config_path.write_text(
        "# haconnect configuration.\n"
        "# The HA token lives in .env, never here.\n"
        "\n"
        "[ha]\n"
        f'ws_url = "{ws_url}"\n'
        "\n"
        "[homekit]\n"
        f'sse_url = "{hk_url}"\n'
        "# Optional. Leave blank to use the proxy's default home.\n"
        'home_id = ""\n'
    )
    env_path.write_text(f"HA_TOKEN={token}\n")

    print(f"\nWrote {config_path} and {env_path}.")
    print("Run: haconnect")
    return 0
