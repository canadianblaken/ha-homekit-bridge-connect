# haconnect

## The problem

If you use both Home Assistant and Apple HomeKit, your devices live in two separate name/room systems that drift apart over time. HA calls it "Office" and groups entities by area; HomeKit calls it "Home Office" and groups accessories by room — and after any device swap, rename, or migration they're out of sync. Fixing this manually means toggling devices one by one to figure out which UUID is which, then hunting through the HomeKit app to reassign rooms and rename accessories — tedious, error-prone, and easy to get wrong when names are similar or accessories are mislabelled.

`haconnect` solves this by letting you physically actuate a device once and automatically correlating the HA state change with the HomeKit accessory that responded. This matters when names don't match or dozens of accessories look similar — you don't have to trust that names align, you just touch the device and the tool figures out which UUID it is. Confirmed matches are saved, and the tool then applies room assignments and renames back to HomeKit in gated, dry-run-first steps — nothing is written until you explicitly confirm.

---

Reconcile Home Assistant **areas** with HomeKit **rooms**, via a Textual TUI.
Built in gated phases.

- **Phase 1 (this build): read-only.** Inventory HA devices by area, inventory
  HomeKit accessories, and match them by actuating a physical device and
  correlating one delta per side. Confirmed matches persist to `mappings.json`.
  No writes to HA or HomeKit.

  **Scope:** *actuators* (on/off-able: `light`, `switch`, `fan`, `input_boolean`,
  `button`, `lock`, `cover`) are included by default. *Sensors* (`binary_sensor`:
  door/motion/water/…) are opt-in (press `n`). LED-strip `*_segment_NNN`
  sub-entities are hidden by default (press `s`). Auxiliary `config`/`diagnostic`
  entities (e.g. Hue "Automation:" toggles) are always filtered out.
- **Phase 2 (this build): room assignment (writes, gated).** For each confirmed
  match whose HomeKit room ≠ HA area, stage `{room: HA area, uuid}`. Re-reads
  device_map, runs `assign_rooms` dry-run, shows the diff, applies only on
  explicit confirm, then re-reads device_map to verify. `Apply ALL` stays locked
  until one real assignment is applied and verified. Press `p` in the TUI, or
  preview headlessly with `--plan` (no writes).
- **Phase 3 (this build): optional rename (writes, gated, off by default).** For
  each confirmed match whose HomeKit name ≠ HA friendly name, stage a rename to
  the HA friendly name. Same dry-run → confirm → apply → verify, batch-locked
  until one rename is verified. Entities with no real friendly name are skipped.
  Press `m` in the TUI, or preview with `--rename-plan` (no writes). Nothing is
  renamed unless you open this screen and confirm.

## Setup

```bash
pip install haconnect        # or: pip install -e . from a cloned repo
haconnect setup              # interactive wizard — writes config.toml + .env
```

`config.toml` holds non-secret endpoints; the HA token lives only in `.env`
(or the `HA_TOKEN` environment variable, which takes precedence).

## Run

```bash
haconnect                    # the TUI
haconnect --selftest         # headless: connect + print inventory/matches
```

### TUI keys
- `h` — help (every key, its steps, required vs suggested)
- `a` — add a device (auto-detects when you actuate one; proposes match / move / group)
- `t` — toggle/activate the highlighted device (to find or test it)
- `space` — mark device(s) · `m` — **Move** to a room (durable: changes the HA→HomeKit bridge)
- `i` — identify (passive: actuate anything, see what fired; no match, no writes)
- `p` — assign HomeKit rooms (sets HomeKit room only — **reverts** in a bridge-per-room setup; prefer `m`)
- `e` — rename HomeKit to match HA friendly name (dry-run → confirm → verify)
- `s` — show/hide LED-strip segment sub-entities
- `n` — include/exclude sensors (door/motion/water)
- `r` — refresh inventory
- `q` — quit

## How matching works

"Add a device" starts listening immediately and **auto-detects** — actuate one
device (physically or in the HA/Hue app) and it polls both sides continuously
(absorbing HomeKit-bridge lag), logging what fired as it arrives:

- **HA**: in-scope entities whose state/brightness changed on the live
  `state_changed` stream.
- **HomeKit**: accessories with a control characteristic change
  (`power`/`brightness`/`color_temperature`/…) since listen start — sensor noise
  (motion, humidity, occupancy) is filtered out.

It reacts to what it finds:
- **Clean 1:1** → auto-proposes `HA light.x ↔ HomeKit 'Name' (UUID, room Y)`;
  Confirm saves it.
- **One HomeKit accessory, no clean HA match** → **"Move to room…"** lets you pick
  the correct room and fixes it (gated dry-run → confirm → verify). Handy for a
  mis-roomed accessory (e.g. a kitchen light showing as "Sitting Room Light 2").
- **Reset** clears and watches fresh; **Close** exits.

If the matched accessory's **HomeKit room differs from its HA area**, the proposal
also offers **"Confirm + move → «area»"**: it saves the mapping, re-reads
device_map, and runs the room assignment for that one accessory right there
(dry-run → confirm → verify) — no need to visit the separate assign screen.

### Controller groups (e.g. a Lutron Aurora / Hue dimmer driving several bulbs)
If actuating one control changes **two or more** HomeKit accessories at once,
they can't be told apart by correlation — so instead of failing, the add flow
**suggests naming them as a numbered group**: `<base> 1` … `<base> N` (base
prefilled from the HA area). You confirm the base name; it renames the HomeKit
accessories via the same dry-run → confirm → verify path. This is the intended
fix for lights wired to one switch/dimmer.

## Notes
- HA connection is a direct websocket client using `HA_TOKEN` (not the HA MCP server).
- HomeKit goes through a HomeKit MCP proxy (`mcp` SDK, `sse_client`). HomeClaw is the reference implementation.
- `entity_id` is never modified; only the friendly-name override is ever written (Phase 3).
