"""One-off scan for likely misplaced / mis-named HomeKit accessories.

Heuristic, advisory only — no writes. Cross-references HomeKit accessories
(name + room) against HA entities (friendly name + area), plus an intrinsic
check (accessory name mentions a different room than it currently sits in).
"""
import asyncio
import re
from collections import defaultdict

from reconcile.config import load_config
from reconcile.core import ReconcileSession

ACTUATOR_CATS = {"lightbulb", "outlet", "switch", "fan", "lock"}


def norm(s):
    if not s:
        return ""
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


async def main():
    s = ReconcileSession(load_config())
    await s.connect()
    await s.load_inventory()

    # HA index: normalized friendly name -> list of (entity_id, area)
    ha_by_name = defaultdict(list)
    for e in s.entities:
        if e.is_segment:
            continue
        ha_by_name[norm(e.friendly_name)].append((e.entity_id, e.area_name))

    rooms = sorted({a.room for a in s.hk_accessories if a.room})
    room_norm = {r: norm(r) for r in rooms}

    accs = [a for a in s.hk_accessories if (a.category in ACTUATOR_CATS)]
    print(f"Scanning {len(accs)} HomeKit actuator accessories "
          f"against {len(s.entities)} HA entities, {len(rooms)} rooms.\n")

    misplaced_by_name = []   # intrinsic: name mentions a different room
    area_mismatch = []       # HA name-match but area != room  -> map + assign
    name_mismatch = []       # HA area-match but spelling/case differs -> rename
    no_ha = []
    dup_names = defaultdict(list)

    for a in accs:
        nn = norm(a.name)
        cur = norm(a.room)
        dup_names[nn].append(a)

        # (A) intrinsic: does the name contain a DIFFERENT room name?
        for r, rn in room_norm.items():
            if rn and rn != cur and rn in nn and len(rn) >= 4:
                misplaced_by_name.append((a, r))
                break

        # (B/C) HA name match
        matches = ha_by_name.get(nn, [])
        if matches:
            # any HA match whose area equals the HK room?
            areas = [area for _, area in matches if area]
            if areas and not any(norm(ar) == cur for ar in areas):
                # name matches HA, but HA area(s) differ from HK room
                area_mismatch.append((a, matches))
        else:
            no_ha.append(a)

    def show(title, items, fmt, limit=40):
        print(f"=== {title} ({len(items)}) ===")
        for it in items[:limit]:
            print("   " + fmt(it))
        if len(items) > limit:
            print(f"   … +{len(items)-limit} more")
        print()

    show("A) Name mentions a DIFFERENT room than it's in  (likely misplaced)",
         misplaced_by_name,
         lambda x: f"{x[0].name!r:<34} in [{x[0].room}]  — name says [{x[1]}]")

    show("B) HA name-match but HA area != HomeKit room  (map → assign candidates)",
         area_mismatch,
         lambda x: f"{x[0].name!r:<30} HK room [{x[0].room}]  vs HA area "
                   f"[{', '.join(sorted({ar for _,ar in x[1] if ar}))}]  "
                   f"({x[1][0][0]})")

    dups = {k: v for k, v in dup_names.items() if len(v) > 1}
    show("C) Duplicate HomeKit names (ambiguous — map by UUID)",
         sorted(dups.items()),
         lambda kv: f"{kv[1][0].name!r:<30} ×{len(kv[1])}  rooms: "
                    f"{sorted({x.room for x in kv[1]})}")

    show("D) HomeKit actuators with NO HA name match (informational)",
         no_ha, lambda a: f"{a.name!r:<34} [{a.room}]  ({a.category})", limit=60)

    await s.close()


asyncio.run(main())
