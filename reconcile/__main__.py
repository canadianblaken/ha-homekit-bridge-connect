"""Entry point.

  python -m reconcile             # launch the TUI (Phase 1)
  python -m reconcile --selftest  # headless: connect, load inventory, print summary
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import ConfigError, load_config
from .core import ReconcileSession


async def _selftest() -> int:
    cfg = load_config()
    session = ReconcileSession(cfg)
    print("Connecting to HA + HomeKit…")
    await session.connect()
    print(f"  HA connected (version {session.ha_version})")
    print(f"  HomeKit ready: {session.hk_status.get('ready')}, "
          f"{session.hk_status.get('accessories')} accessories")
    await session.load_inventory()
    seg, sen = session.segment_count(), session.sensor_count()
    print(f"\nHA: {len(session.areas)} areas, {len(session.entities)} in-scope entities")
    print(f"    actuators shown; {seg} strip segments hidden; "
          f"{sen} sensors hidden (opt-in)")
    print(f"HomeKit: {len(session.hk_accessories)} accessories")
    print(f"Saved mappings: {len(session.store.all())}\n")

    def dump(area_id, header):
        ents = session.entities_in_area(area_id)
        if not ents:
            return
        print(f"[{header}]  ({len(ents)})")
        for e in ents:
            m = session.match_for(e.entity_id)
            tag = (f"✓ {m['hk_name']} ({m.get('room_at_match')})" if m else "— unmatched")
            print(f"   {e.friendly_name:<30} {e.domain:<8} {e.entity_id:<40} {tag}")

    for area in session.areas:
        dump(area.area_id, area.name)
    dump(None, "no area")

    await session.close()
    return 0


async def _plan() -> int:
    cfg = load_config()
    session = ReconcileSession(cfg)
    await session.connect()
    await session.load_inventory()
    print("Building Phase 2 plan (re-reading device_map)…\n")
    plan = await session.build_plan()
    print(f"To assign : {len(plan.assignments)}")
    print(f"Aligned   : {plan.aligned}")
    print(f"Skipped   : {len(plan.problems)}\n")
    for p in plan.assignments:
        flag = "  [creates new room]" if p.creates_room else ""
        print(f"  {p.hk_name:<28} {p.current_room or '(none)':<18} -> {p.target_room}{flag}")
    for p in plan.problems:
        print(f"  [skip] {p.hk_name or p.entity_id:<28} {p.note}")
    if plan.assignments:
        print("\nDry-run (no writes):")
        dr = await session.assign_dry_run(plan.assignments)
        print(f"  assigned={dr.get('assigned')} skipped={dr.get('skipped')} "
              f"not_found={dr.get('not_found')}")
        for d in dr.get("details", []):
            print(f"    • {d.get('accessory')} → {d.get('room')}  [{d.get('status')}]")
    await session.close()
    return 0


async def _rename_plan() -> int:
    cfg = load_config()
    session = ReconcileSession(cfg)
    await session.connect()
    await session.load_inventory()
    print("Building Phase 3 rename plan (re-reading device_map)…\n")
    plan = await session.build_rename_plan()
    print(f"To rename : {len(plan.renames)}")
    print(f"Matching  : {plan.aligned}")
    print(f"Skipped   : {len(plan.problems)}\n")
    for p in plan.renames:
        print(f"  '{p.current_name}'  ->  '{p.target_name}'")
    for p in plan.problems:
        print(f"  [skip] {p.current_name or p.entity_id}: {p.note}")
    if plan.renames:
        print("\nDry-run (no writes):")
        for d in await session.rename_dry_run(plan.renames):
            print(f"    • '{d.get('old_name')}' → '{d.get('new_name')}'")
    await session.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="haconnect")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="interactive setup wizard (writes config.toml + .env)")
    parser.add_argument("--selftest", action="store_true",
                        help="headless connect + inventory dump (no TUI)")
    parser.add_argument("--plan", action="store_true",
                        help="headless Phase 2 plan + dry-run preview (no writes)")
    parser.add_argument("--rename-plan", action="store_true",
                        help="headless Phase 3 rename plan + dry-run preview (no writes)")
    args = parser.parse_args()

    if args.cmd == "setup":
        from pathlib import Path
        from .setup import run_setup
        try:
            return run_setup(Path("."))
        except KeyboardInterrupt:
            print("\nAborted.")
            return 0

    try:
        if args.selftest:
            return asyncio.run(_selftest())
        if args.plan:
            return asyncio.run(_plan())
        if args.rename_plan:
            return asyncio.run(_rename_plan())
        # default: TUI
        cfg = load_config()
        from .app import ReconcileApp
        ReconcileApp(ReconcileSession(cfg)).run()
        return 0
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
