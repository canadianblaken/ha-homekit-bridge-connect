"""Textual TUI for Phase 1 (read-only inventory + matching)."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from .core import ReconcileSession
from .matcher import Correlation
from .models import PlannedRename, is_segment_entity
from .phase2 import normalize_room
from .scope import active_chars, ha_kind

UNASSIGNED = "— Unassigned (no area) —"

# sentinel: no area selected at all (distinct from the None "Unassigned" bucket)
_NO_AREA_SELECTED = object()


HELP_TEXT = """[b]haconnect — help[/b]

Every HomeKit write goes through [b]dry-run → your confirm → verify[/b]. Nothing changes \
without you confirming it.
Legend:  [green]required[/green] = you must do it   ·   [yellow]suggested[/yellow] = optional / recommended

[b]Navigation[/b]
  • Left list = HA areas · right table = the devices in the highlighted area.
  • [green]↑/↓[/green] move the highlight · [green]Tab[/green] switches panes · [green]Enter[/green] selects.

[b]The HK column[/b]   (alignment with the HomeKit BRIDGE — the room that actually sticks)
  [green]✓ aligned[/green]     on its HA area's bridge — correct & durable.
  [bold yellow]⇄ on Lila[/bold yellow]    wrong bridge — looks right now, but will drift to that room. [b]m[/b] fixes it.
  [red]✗ no bridge[/red]   not exposed by any bridge. [b]m[/b] adds it to its HA-area bridge.
  [dim]— no HA area[/dim]  entity has no HA area (nothing to compare).
  A leading dim [dim]?[/dim] = no saved HA↔HomeKit mapping yet.

[b]a — Add device[/b]   (find a device and match it; auto-detecting)
  1. [green]Actuate ONE device[/green] — flip the physical switch, or toggle it in the HA/Hue app.
     [dim](Highlighting its name in this list does NOT turn it on — use [b]t[/b] for that.)[/dim]
  2. It listens continuously and lists what fired on each side (no 'Correlate' button to time).
  3. [green]Clean 1-to-1[/green] → a match proposal pops up → [green]Confirm[/green] saves it to mappings.json.
       • If the HomeKit room ≠ the HA area, you also get [yellow]Confirm + move[/yellow].
  4. One HomeKit accessory, no HA match → [yellow]Move to room…[/yellow] (pick the room yourself).
  5. Two+ accessories at once (a dimmer/controller) → [yellow]Name group[/yellow] → '<base> 1 … N'.
  [yellow]Reset[/yellow] clears and watches fresh · [yellow]Close[/yellow] exits.

[b]t — Toggle[/b]   (activate the highlighted device)
  [green]Highlight a device[/green] in the table, press [b]t[/b] → it turns on/off via HA.
  Great for "which physical thing is this row?" and for testing.

[b]space — Mark[/b] · [b]m — Move[/b]   (the durable room fix)
  [green]space[/green] marks the highlighted device (● appears) — mark as many as you like.
  [b]m[/b] → pick a target room → it moves the device(s) onto that room's HA→HomeKit
  [b]bridge[/b] (so the room sticks), via dry-run → [green]confirm[/green] → apply + reload (no HA restart).
  [yellow]No marks?[/yellow] m moves just the highlighted row. Works on any device — no toggling, no UUID needed.

[b]i — Identify[/b]   (passive: see what fired — no match, no writes)
  Actuate anything and watch the live HA + HomeKit log.
  [yellow]Sensors[/yellow] button reveals motion / contact / occupancy.

[b]p — Assign rooms[/b]   (set the HomeKit room for confirmed matches)
  Steps: re-read → dry-run → [green]confirm[/green] → apply → verify. 'Apply ALL' unlocks after one verified.
  [red]Note for your setup:[/red] HomeKit re-asserts each accessory's room from its [b]bridge[/b], so this
  can [b]revert[/b]. The durable fix is a bridge move (Move — see Planned, below).

[b]e — Rename[/b]   (set the HomeKit name to the HA friendly name)
  Steps: re-read → dry-run → [green]confirm[/green] → apply → verify.
  [yellow]Tip:[/yellow] a bridge move resets names — so rename [b]after[/b] moving.

[b]s — Segments[/b]   show/hide LED-strip segment sub-entities (hidden by default — [yellow]leave off[/yellow] unless needed).
[b]n — Sensors[/b]    include door/motion/water sensors (off by default — [yellow]turn on only to identify a sensor[/yellow]).
[b]r — Refresh[/b]    re-read HA + HomeKit inventory.
[b]h — Help[/b]       this screen.
[b]q — Quit[/b]       exit.

[b]p — Assign rooms[/b] vs [b]m — Move[/b]:  use [b]m[/b] (Move) for a durable fix — it changes the
  bridge. [b]p[/b] only sets the HomeKit room and will revert in this setup.

[dim]Press Esc or Close to return.[/dim]"""


class HelpScreen(ModalScreen):
    """Scrollable help: every key, its steps, and required vs suggested."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            with VerticalScroll(id="help-body"):
                yield Static(HELP_TEXT, id="help-text")
            yield Button("Close", variant="primary", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class ProposalScreen(ModalScreen):
    """Confirm a single HA<->HomeKit match."""

    def __init__(self, session: ReconcileSession, corr: Correlation):
        super().__init__()
        self.session = session
        self.corr = corr

    def _entity(self):
        return next((e for e in self.session.entities
                     if e.entity_id == self.corr.ha_entity), None)

    def _mismatch(self) -> tuple[str, str] | None:
        """(ha_area, current_hk_room) if the rooms differ, else None."""
        ent = self._entity()
        acc = self.corr.hk_accessory or {}
        area = ent.area_name if ent else None
        room = acc.get("room")
        if area and normalize_room(room) != normalize_room(area):
            return area, room or "(none)"
        return None

    def compose(self) -> ComposeResult:
        eid = self.corr.ha_entity
        acc = self.corr.hk_accessory or {}
        ent = self._entity()
        friendly = ent.friendly_name if ent else eid
        room = acc.get("room") or "?"
        lines = (
            "Propose match:\n\n"
            f"  HA      [b]{eid}[/b]  ({friendly})\n"
            f"  HomeKit [b]{acc.get('name', '?')}[/b]\n"
            f"          {acc.get('id', '?')}\n"
            f"          currently in room: [b]{room}[/b]\n\n"
            "Confirm this mapping?"
        )
        mismatch = self._mismatch()
        with Vertical(id="proposal-box"):
            yield Static(lines, id="proposal-text")
            if mismatch:
                area, cur = mismatch
                yield Static(
                    f"[yellow]Wrong room:[/yellow] HomeKit '{cur}' ≠ HA area '{area}'.",
                    id="proposal-mismatch")
            yield Static(" ", id="proposal-status")
            with Horizontal(id="proposal-buttons"):
                yield Button("Confirm", variant="success", id="confirm")
                if mismatch:
                    yield Button(f"Confirm + move → {mismatch[0]}",
                                 variant="warning", id="confirm_move")
                yield Button("Cancel", variant="error", id="cancel")

    def _save(self) -> dict:
        acc = self.corr.hk_accessory or {}
        return self.session.confirm_match(
            self.corr.ha_entity, self.corr.hk_uuid,
            fallback_name=acc.get("name"), fallback_room=acc.get("room"))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(self._save())
        elif event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "confirm_move":
            self.run_worker(self._confirm_and_move(), exclusive=True)

    def _set_status(self, msg: str) -> None:
        self.query_one("#proposal-status", Static).update(msg)

    async def _confirm_and_move(self) -> None:
        mapping = self._save()  # record the mapping first
        self._set_status("Re-reading device_map…")
        try:
            p = await self.session.plan_for_entity(self.corr.ha_entity)
        except Exception as e:
            self._set_status(f"[red]Plan failed:[/red] {e}")
            return
        if p is None:
            self._set_status("[green]Already in the right room.[/green]")
            self.dismiss(mapping)
            return
        try:
            dr = await self.session.assign_dry_run([p])
        except Exception as e:
            self._set_status(f"[red]Dry-run failed:[/red] {e}")
            return
        d = (dr.get("details") or [{}])[0]
        note = "  [yellow](creates new room)[/yellow]" if p.creates_room else ""
        ok = await self.app.push_screen_wait(ConfirmScreen(
            "Move room — REAL write",
            f"  {p.hk_name}\n  {p.current_room or '(none)'} → [b]{p.target_room}[/b]{note}\n"
            f"  [dim]dry-run: {d.get('status')}[/dim]\n\n[b]Writes to HomeKit.[/b]",
            confirm_label=f"Move to {p.target_room}", confirm_variant="error"))
        if not ok:
            self._set_status("Mapping saved; move cancelled.")
            self.dismiss(mapping)
            return
        try:
            await self.session.assign_apply([p])
            results = await self.session.verify_assignment([p])
        except Exception as e:
            self._set_status(f"[red]Apply/verify failed:[/red] {e}")
            return
        _, actual, okv = results[0]
        if okv:
            self.app.notify(f"Moved {p.hk_name} → {actual}", title="Room fixed")
        else:
            self.app.notify(f"Move not verified (now {actual!r})", severity="error")
        self.dismiss(mapping)


class ConfirmScreen(ModalScreen[bool]):
    """Generic yes/no confirmation. Returns True on confirm."""

    def __init__(self, title: str, body: str, confirm_label: str = "Apply",
                 confirm_variant: str = "warning"):
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label
        self._confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="proposal-box"):
            yield Static(f"[b]{self._title}[/b]\n\n{self._body}", id="proposal-text")
            with Horizontal(id="proposal-buttons"):
                yield Button(self._confirm_label, variant=self._confirm_variant, id="confirm")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class AssignScreen(ModalScreen):
    """Phase 2: assign HomeKit rooms to match HA areas. All writes are gated:
    dry-run preview -> explicit confirm -> apply -> verify. Batch stays locked
    until one real assignment has been applied and verified."""

    plan = None  # class-level default so the attribute always exists

    def __init__(self, session: ReconcileSession):
        super().__init__()
        self.session = session
        self.plan = None

    def compose(self) -> ComposeResult:
        with Vertical(id="assign-box"):
            yield Static("[b]Phase 2 — assign HomeKit rooms[/b]", id="assign-title")
            yield DataTable(id="assign-table", cursor_type="row", zebra_stripes=True)
            yield Static("Loading plan…", id="assign-status")
            with Horizontal(id="assign-buttons"):
                yield Button("Re-plan", id="replan")
                yield Button("Dry-run all", id="dryrun")
                yield Button("Apply ONE", variant="warning", id="apply_one")
                yield Button("Apply ALL", variant="error", id="apply_all", disabled=True)
                yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        t = self.query_one("#assign-table", DataTable)
        t.add_columns("Device", "current room", "→ target (HA area)", "note")
        self.run_worker(self._reload(), exclusive=True)

    def _status(self, msg: str) -> None:
        self.query_one("#assign-status", Static).update(msg)

    async def _reload(self) -> None:
        self._status("Re-reading device_map…")
        try:
            self.plan = await self.session.build_plan()
        except Exception as e:
            self._status(f"[red]Plan failed:[/red] {e}")
            return
        t = self.query_one("#assign-table", DataTable)
        t.clear()
        for p in self.plan.assignments:
            note = p.note or ""
            t.add_row(p.hk_name, p.current_room or "(none)", p.target_room,
                      f"[yellow]{note}[/yellow]" if note else "")
        for p in self.plan.problems:
            t.add_row(p.hk_name or p.entity_id, p.current_room or "?", "—",
                      f"[red]skip: {p.note}[/red]")
        n = len(self.plan.assignments)
        self.query_one("#apply_one", Button).disabled = n == 0
        self.query_one("#apply_all", Button).disabled = not (
            self.session.batch_unlocked and n > 0
        )
        gate = "" if self.session.batch_unlocked else "  [dim](Apply ALL unlocks after one verified apply)[/dim]"
        self._status(
            f"{n} to assign · {self.plan.aligned} already aligned · "
            f"{len(self.plan.problems)} skipped{gate}"
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.dismiss()
        elif bid == "replan":
            self.run_worker(self._reload(), exclusive=True)
        elif bid == "dryrun":
            self.run_worker(self._dryrun(), exclusive=True)
        elif bid == "apply_one":
            self.run_worker(self._apply(one=True), exclusive=True)
        elif bid == "apply_all":
            self.run_worker(self._apply(one=False), exclusive=True)

    def _summarize(self, dr: dict) -> str:
        rows = []
        for d in dr.get("details", []):
            rows.append(f"  • {d.get('accessory')} → {d.get('room')}  [{d.get('status')}]")
        nf = dr.get("not_found") or []
        out = "\n".join(rows) if rows else "  (nothing)"
        if nf:
            out += f"\n  [red]not_found: {nf}[/red]"
        return out

    async def _dryrun(self) -> None:
        act = self.plan.assignments if self.plan else []
        if not act:
            self._status("Nothing to dry-run.")
            return
        self._status("Running dry-run…")
        try:
            dr = await self.session.assign_dry_run(act)
        except Exception as e:
            self._status(f"[red]Dry-run failed:[/red] {e}")
            return
        await self.app.push_screen_wait(
            ConfirmScreen(
                "Dry-run preview (no changes made)",
                self._summarize(dr) + "\n\n[dim]This was a preview only.[/dim]",
                confirm_label="OK", confirm_variant="primary",
            )
        )
        self._status(f"Dry-run: would assign {dr.get('assigned')}, skip {dr.get('skipped')}.")

    async def _apply(self, one: bool) -> None:
        act = self.plan.assignments if self.plan else []
        if not act:
            self._status("Nothing to apply.")
            return
        targets = act[:1] if one else list(act)
        # Always show the dry-run for exactly these targets first.
        try:
            dr = await self.session.assign_dry_run(targets)
        except Exception as e:
            self._status(f"[red]Dry-run failed:[/red] {e}")
            return
        body = (
            self._summarize(dr)
            + f"\n\n[b]This will WRITE to HomeKit[/b] ({len(targets)} accessory"
            + ("ies" if len(targets) != 1 else "y") + ")."
        )
        ok = await self.app.push_screen_wait(
            ConfirmScreen("Apply room assignment — REAL write", body,
                          confirm_label="Apply for real", confirm_variant="error")
        )
        if not ok:
            self._status("Cancelled — nothing written.")
            return
        self._status("Applying…")
        try:
            await self.session.assign_apply(targets)
            results = await self.session.verify_assignment(targets)
        except Exception as e:
            self._status(f"[red]Apply/verify failed:[/red] {e}")
            return
        good = [p for p, _, okv in results if okv]
        bad = [(p, actual) for p, actual, okv in results if not okv]
        if good and one:
            self.session.batch_unlocked = True
        msg = f"[green]Verified {len(good)}/{len(targets)} assigned.[/green]"
        if bad:
            msg += "  [red]Failed: " + ", ".join(
                f"{p.hk_name} (now {actual!r})" for p, actual in bad
            ) + "[/red]"
        self.app.refresh_devices()  # type: ignore[attr-defined]
        await self._reload()
        self._status(msg)


class RenameScreen(ModalScreen):
    """Phase 3 (optional): rename HomeKit accessories to match the HA friendly
    name. Same gating as Phase 2: dry-run -> confirm -> apply -> verify, with
    batch locked until one rename is applied and verified."""

    plan = None

    def __init__(self, session: ReconcileSession):
        super().__init__()
        self.session = session
        self.plan = None

    def compose(self) -> ComposeResult:
        with Vertical(id="assign-box"):
            yield Static("[b]Phase 3 — rename HomeKit to match HA[/b]", id="assign-title")
            yield DataTable(id="rename-table", cursor_type="row", zebra_stripes=True)
            yield Static("Loading plan…", id="rename-status")
            with Horizontal(id="assign-buttons"):
                yield Button("Re-plan", id="replan")
                yield Button("Dry-run all", id="dryrun")
                yield Button("Apply ONE", variant="warning", id="apply_one")
                yield Button("Apply ALL", variant="error", id="apply_all", disabled=True)
                yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        t = self.query_one("#rename-table", DataTable)
        t.add_columns("current HomeKit name", "→ target (HA friendly name)", "note")
        self.run_worker(self._reload(), exclusive=True)

    def _status(self, msg: str) -> None:
        self.query_one("#rename-status", Static).update(msg)

    async def _reload(self) -> None:
        self._status("Re-reading device_map…")
        try:
            self.plan = await self.session.build_rename_plan()
        except Exception as e:
            self._status(f"[red]Plan failed:[/red] {e}")
            return
        t = self.query_one("#rename-table", DataTable)
        t.clear()
        for p in self.plan.renames:
            t.add_row(p.current_name or "(none)", p.target_name, "")
        for p in self.plan.problems:
            t.add_row(p.current_name or p.entity_id, "—", f"[red]skip: {p.note}[/red]")
        n = len(self.plan.renames)
        self.query_one("#apply_one", Button).disabled = n == 0
        self.query_one("#apply_all", Button).disabled = not (
            self.session.rename_batch_unlocked and n > 0)
        gate = "" if self.session.rename_batch_unlocked else \
            "  [dim](Apply ALL unlocks after one verified rename)[/dim]"
        self._status(
            f"{n} to rename · {self.plan.aligned} already matching · "
            f"{len(self.plan.problems)} skipped{gate}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.dismiss()
        elif bid == "replan":
            self.run_worker(self._reload(), exclusive=True)
        elif bid == "dryrun":
            self.run_worker(self._dryrun(), exclusive=True)
        elif bid == "apply_one":
            self.run_worker(self._apply(one=True), exclusive=True)
        elif bid == "apply_all":
            self.run_worker(self._apply(one=False), exclusive=True)

    def _summarize(self, results: list[dict]) -> str:
        rows = [f"  • '{d.get('old_name')}' → '{d.get('new_name')}'" for d in results]
        return "\n".join(rows) if rows else "  (nothing)"

    async def _dryrun(self) -> None:
        act = self.plan.renames if self.plan else []
        if not act:
            self._status("Nothing to dry-run.")
            return
        self._status("Running dry-run…")
        try:
            dr = await self.session.rename_dry_run(act)
        except Exception as e:
            self._status(f"[red]Dry-run failed:[/red] {e}")
            return
        await self.app.push_screen_wait(ConfirmScreen(
            "Dry-run preview (no changes made)",
            self._summarize(dr) + "\n\n[dim]This was a preview only.[/dim]",
            confirm_label="OK", confirm_variant="primary"))
        self._status(f"Dry-run previewed {len(dr)} rename(s).")

    async def _apply(self, one: bool) -> None:
        act = self.plan.renames if self.plan else []
        if not act:
            self._status("Nothing to apply.")
            return
        targets = act[:1] if one else list(act)
        try:
            dr = await self.session.rename_dry_run(targets)
        except Exception as e:
            self._status(f"[red]Dry-run failed:[/red] {e}")
            return
        body = (self._summarize(dr)
                + f"\n\n[b]This will WRITE to HomeKit[/b] ({len(targets)} rename"
                + ("s" if len(targets) != 1 else "") + ").")
        ok = await self.app.push_screen_wait(ConfirmScreen(
            "Apply rename — REAL write", body,
            confirm_label="Rename for real", confirm_variant="error"))
        if not ok:
            self._status("Cancelled — nothing written.")
            return
        self._status("Renaming…")
        try:
            await self.session.rename_apply(targets)
            results = await self.session.verify_rename(targets)
        except Exception as e:
            self._status(f"[red]Apply/verify failed:[/red] {e}")
            return
        good = [p for p, _, okv in results if okv]
        bad = [(p, actual) for p, actual, okv in results if not okv]
        if good and one:
            self.session.rename_batch_unlocked = True
        msg = f"[green]Verified {len(good)}/{len(targets)} renamed.[/green]"
        if bad:
            msg += "  [red]Failed: " + ", ".join(
                f"{p.target_name} (now {actual!r})" for p, actual in bad) + "[/red]"
        await self._reload()
        self._status(msg)


class GroupNameScreen(ModalScreen):
    """Suggestion when a controller fires multiple accessories at once: name them
    as a numbered group (<base> 1 … <base> N). Sidesteps 1:1 correlation —
    grouped devices can't be told apart, so a shared base + number is the fix.
    Renames the HomeKit accessories (gated: dry-run -> confirm -> verify)."""

    def __init__(self, session: ReconcileSession, accessories: list[dict], base: str):
        super().__init__()
        self.session = session
        self.accessories = accessories
        self._base = base

    def compose(self) -> ComposeResult:
        n = len(self.accessories)
        names = ", ".join(a.get("name", "?") for a in self.accessories)
        with Vertical(id="group-box"):
            yield Static(
                f"[b]Group detected[/b] — {n} accessories changed together "
                "(looks like one controller).",
                id="group-head",
            )
            yield Static(f"[dim]Currently:[/dim] {names}", id="group-current")
            yield Static("Base name — they'll become '<base> 1' … '<base> "
                         f"{n}' (exact preview shown before any write):")
            yield Input(value=self._base, id="base", placeholder="base name")
            yield Static("[dim]Apply runs a dry-run, then asks you to confirm.[/dim]",
                         id="group-msg")
            with Horizontal(id="group-buttons"):
                yield Button(f"Apply rename ×{n}", variant="warning", id="apply")
                yield Button("Cancel", variant="primary", id="cancel")

    def _base_val(self) -> str:
        return (self.query_one("#base", Input).value or "").strip() or self._base

    def _plans(self) -> list[PlannedRename]:
        b = self._base_val()
        return [
            PlannedRename("", a.get("id"), a.get("name"), f"{b} {i + 1}", True)
            for i, a in enumerate(self.accessories)
        ]

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self.run_worker(self._apply(), exclusive=True)

    def _msg(self, m: str) -> None:
        self.query_one("#group-msg", Static).update(m)

    async def _apply(self) -> None:
        plans = self._plans()
        try:
            dr = await self.session.rename_dry_run(plans)
        except Exception as e:
            self._msg(f"[red]Dry-run failed:[/red] {e}")
            return
        body = "\n".join(
            f"  • '{d.get('old_name')}' → '{d.get('new_name')}'" for d in dr)
        ok = await self.app.push_screen_wait(ConfirmScreen(
            "Apply group rename — REAL write",
            body + f"\n\n[b]Writes {len(plans)} HomeKit names.[/b]",
            confirm_label="Rename all", confirm_variant="error"))
        if not ok:
            self._msg("Cancelled — nothing written.")
            return
        try:
            await self.session.rename_apply(plans)
            results = await self.session.verify_rename(plans)
        except Exception as e:
            self._msg(f"[red]Apply/verify failed:[/red] {e}")
            return
        good = sum(1 for _, _, okv in results if okv)
        self.dismiss({"renamed": good, "total": len(plans)})


class RoomPickerScreen(ModalScreen):
    """Pick a target room (HA area). Returns the area name, or None."""

    def __init__(self, session: ReconcileSession, title: str):
        super().__init__()
        self.session = session
        self._title = title
        self._names: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(self._title, id="picker-title")
            yield ListView(id="picker-list")
            with Horizontal(id="picker-buttons"):
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        lv = self.query_one("#picker-list", ListView)
        self._names = [a.name for a in self.session.areas]
        for n in self._names:
            lv.append(ListItem(Label(n)))
        if self._names:
            lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and idx < len(self._names):
            self.dismiss(self._names[idx])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class MoveScreen(ModalScreen):
    """Move device(s) to the right HomeKit bridge (the durable fix). Defaults to
    moving each device onto ITS OWN HA-area bridge; offers a 'pick a room' override
    for sending everything to one room. plan → confirm → apply + reload."""

    def __init__(self, session: ReconcileSession, entity_ids: list[str]):
        super().__init__()
        self.session = session
        self.entity_ids = entity_ids
        self._plan = None
        self._bridges = None
        self._target_label = None

    def compose(self) -> ComposeResult:
        names = ", ".join(self.session._friendly_of(e) for e in self.entity_ids)
        with Vertical(id="assign-box"):
            yield Static(f"[b]Move {len(self.entity_ids)} device(s)[/b] to the right "
                         "HomeKit bridge (the durable fix)", id="assign-title")
            yield Static(f"[dim]{names}[/dim]", id="move-devices")
            yield Static("Planning…", id="move-plan")
            with Horizontal(id="assign-buttons"):
                yield Button("Apply move", variant="error", id="apply", disabled=True)
                yield Button("Pick a room instead…", id="pick")
                yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        self.run_worker(self._auto(), exclusive=True)

    def _set(self, msg: str) -> None:
        self.query_one("#move-plan", Static).update(msg)

    async def _read_bridges(self) -> None:
        self._set("Reading bridges… (a few seconds)")
        self._bridges = await self.session.bridge_snapshot()

    async def _auto(self) -> None:
        """Default: each device → its own HA area's bridge."""
        try:
            await self._read_bridges()
            self._plan = self.session.plan_move_auto(self.entity_ids, self._bridges)
        except Exception as e:
            self._set(f"[red]Plan failed:[/red] {e}")
            return
        self._target_label = "each device's HA area"
        self._show_plan("Fix each device onto its HA-area bridge:")

    async def _pick(self) -> None:
        area = await self.app.push_screen_wait(
            RoomPickerScreen(self.session, "Send ALL selected to which room?"))
        if not area:
            return
        try:
            if self._bridges is None:
                await self._read_bridges()
            items, _ = self.session.plan_move(self.entity_ids, area, self._bridges)
        except Exception as e:
            self._set(f"[red]Plan failed:[/red] {e}")
            return
        self._plan = items
        self._target_label = area
        self._show_plan(f"Send all to [b]{area}[/b]:")

    def _show_plan(self, header: str) -> None:
        lines = [header]
        for it in self._plan:
            if it.status == "move":
                lines.append(f"  [green]move[/green]  {it.friendly}: "
                             f"{it.source_title or '(none)'} → {it.target_title}")
            elif it.status == "already":
                lines.append(f"  [dim]ok[/dim]  {it.friendly} (already on {it.target_title})")
            else:
                lines.append(f"  [red]skip[/red]  {it.friendly}: {it.note}")
        n = sum(1 for it in self._plan if it.status == "move")
        if n:
            lines.append(f"\n[b]{n}[/b] to move — press [b]Apply move[/b].")
        elif all(it.status == "no_target" for it in self._plan):
            missing = {it.note.removeprefix("no bridge maps to ") for it in self._plan
                       if it.status == "no_target"}
            lines.append(
                f"\n[red]No HomeKit bridge exists for: {', '.join(sorted(missing))}[/red]\n"
                "Create one in HA → Settings → Devices & Services → Add Integration → "
                "HomeKit Bridge, then refresh and try again.")
        else:
            lines.append("\n[dim]Nothing to move.[/dim]")
        self._set("\n".join(lines))
        self.query_one("#apply", Button).disabled = n == 0

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "close":
            self.dismiss(None)
        elif bid == "pick":
            self.run_worker(self._pick(), exclusive=True)
        elif bid == "apply":
            self.run_worker(self._apply(), exclusive=True)

    async def _apply(self) -> None:
        if not self._plan:
            return
        n = sum(1 for it in self._plan if it.status == "move")
        if not n:
            return
        ok = await self.app.push_screen_wait(ConfirmScreen(
            "Move bridges — REAL write",
            f"Move [b]{n}[/b] device(s) → [b]{self._target_label}[/b].\n"
            "[dim]Edits HA bridge config + reloads those bridges (no HA restart).\n"
            "HomeKit re-publishes in ~30s; UUIDs change (we key on entity_id).[/dim]",
            confirm_label="Move", confirm_variant="error"))
        if not ok:
            self._set("Cancelled — nothing written.")
            return
        self._set("Applying — editing bridges + reloading…")
        try:
            changed = await self.session.apply_move(self._plan, self._bridges)
        except Exception as e:
            self._set(f"[red]Apply failed:[/red] {e}")
            return
        self.app.notify(
            f"Moved {n} device(s). Reloaded {len(changed)} bridge(s); "
            "HomeKit re-publishes shortly.", title="Bridge move", timeout=6)
        self.dismiss(True)


class ListenScreen(ModalScreen):
    """Auto-listening add flow: actuate one device and it detects what fired on
    both sides automatically. A clean 1:1 auto-proposes a match (with room move).
    Otherwise it shows what was found and offers 'Move to room…' (single HomeKit
    accessory) or 'Name group' (a controller firing several at once)."""

    def __init__(self, session: ReconcileSession):
        super().__init__()
        self.session = session
        self._timer = None
        self._busy = False
        self._proposed = False
        self._seen_ha: set = set()
        self._seen_hk: set = set()
        self._last: Correlation | None = None

    def compose(self) -> ComposeResult:
        scope = "lights, switches, fans, outlets, buttons" + (
            ", + sensors" if self.session.include_sensors else "")
        with Vertical(id="listen-box"):
            yield Static(
                "[b]Add a device[/b] — actuate ONE device (physically or in the "
                f"HA/Hue app); I'll detect it automatically.\n[dim]In scope: {scope}[/dim]",
                id="listen-head")
            yield RichLog(id="listen-log", markup=True, wrap=False, max_lines=200)
            yield Static("Starting…", id="listen-status")
            with Horizontal(id="listen-buttons"):
                yield Button("Reset", id="reset")
                yield Button("Move to room…", id="move", disabled=True)
                yield Button("Name group", id="group", disabled=True)
                yield Button("Close", variant="primary", id="cancel")

    async def on_mount(self) -> None:
        await self.session.start_listen()
        self._timer = self.set_interval(1.5, self._poll)
        self._set_status("[green]Listening[/green] — actuate one device.")

    def _set_status(self, msg: str) -> None:
        self.query_one("#listen-status", Static).update(msg)

    def _friendly(self, eid: str) -> str:
        e = next((x for x in self.session.entities if x.entity_id == eid), None)
        return e.friendly_name if e else eid

    async def _poll(self) -> None:
        if self._busy or self._proposed or self.app.screen is not self:
            return
        self._busy = True
        try:
            try:
                corr = await self.session.correlate_now()
            except Exception as e:
                self._set_status(f"[red]Error reading HomeKit events:[/red] {e}")
                return
            self._last = corr
            log = self.query_one("#listen-log", RichLog)
            for eid in corr.ha_entities:
                if eid not in self._seen_ha:
                    self._seen_ha.add(eid)
                    log.write(f"[cyan]HA[/cyan] {self._friendly(eid)}  ([dim]{eid}[/dim])")
            for uid, acc in corr.hk_accessories.items():
                if uid not in self._seen_hk:
                    self._seen_hk.add(uid)
                    log.write(f"[magenta]HK[/magenta] {acc.get('name', '?')}  "
                              f"([dim]{acc.get('room')}[/dim])")
            nhk = len(corr.hk_accessories)
            self.query_one("#move", Button).disabled = nhk != 1
            self.query_one("#group", Button).disabled = nhk < 2
            if corr.ok and not self._proposed:
                self._proposed = True
                self.app.push_screen(ProposalScreen(self.session, corr), self._after_proposal)
                return
            self._set_status(self._summary(corr))
        finally:
            self._busy = False

    def _summary(self, corr: Correlation) -> str:
        nha, nhk = len(corr.ha_entities), len(corr.hk_accessories)
        if nha == 0 and nhk == 0:
            hint = "" if self.session.include_sensors else " · sensors off"
            return f"[green]Listening[/green] — actuate one device{hint}."
        msg = f"Detected — HA: {nha} · HomeKit: {nhk}."
        if nhk == 1 and nha != 1:
            msg += "  [yellow]No clean HA match — use 'Move to room…'.[/yellow]"
        elif nhk >= 2:
            msg += "  [yellow]Controller group? — 'Name group'.[/yellow]"
        return msg

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel":
            self.dismiss(None)
        elif bid == "reset":
            await self._fresh()
            self._set_status("[green]Reset.[/green] Actuate one device.")
        elif bid == "move":
            self.run_worker(self._move(), exclusive=True)
        elif bid == "group":
            corr = self._last
            if corr and len(corr.hk_accessories) >= 2:
                self.app.push_screen(
                    GroupNameScreen(self.session, list(corr.hk_accessories.values()),
                                    self._suggest_base(corr)),
                    self._after_group)

    async def _fresh(self) -> None:
        await self.session.start_listen()
        self._seen_ha.clear()
        self._seen_hk.clear()
        self._proposed = False
        self.query_one("#listen-log", RichLog).clear()
        self.query_one("#move", Button).disabled = True
        self.query_one("#group", Button).disabled = True

    async def _move(self) -> None:
        corr = self._last
        if not corr or len(corr.hk_accessories) != 1:
            self._set_status("Need exactly one HomeKit accessory detected to move.")
            return
        uid, acc = next(iter(corr.hk_accessories.items()))
        area = await self.app.push_screen_wait(RoomPickerScreen(
            self.session, f"Move '{acc.get('name')}' (now in {acc.get('room')}) to:"))
        if not area:
            return
        self._set_status("Re-reading device_map…")
        try:
            plan = await self.session.assign_uuid_plan(uid, area)
        except Exception as e:
            self._set_status(f"[red]Plan failed:[/red] {e}")
            return
        if normalize_room(plan.current_room) == normalize_room(plan.target_room):
            self._set_status(f"[green]Already in {plan.target_room}.[/green]")
            return
        try:
            dr = await self.session.assign_dry_run([plan])
        except Exception as e:
            self._set_status(f"[red]Dry-run failed:[/red] {e}")
            return
        d = (dr.get("details") or [{}])[0]
        note = "  [yellow](creates new room)[/yellow]" if plan.creates_room else ""
        ok = await self.app.push_screen_wait(ConfirmScreen(
            "Move room — REAL write",
            f"  {plan.hk_name}\n  {plan.current_room or '(none)'} → [b]{plan.target_room}[/b]{note}\n"
            f"  [dim]dry-run: {d.get('status')}[/dim]\n\n[b]Writes to HomeKit.[/b]",
            confirm_label=f"Move to {plan.target_room}", confirm_variant="error"))
        if not ok:
            self._set_status("Move cancelled.")
            return
        try:
            await self.session.assign_apply([plan])
            results = await self.session.verify_assignment([plan])
        except Exception as e:
            self._set_status(f"[red]Apply/verify failed:[/red] {e}")
            return
        _, actual, okv = results[0]
        if okv:
            self.app.notify(f"Moved {plan.hk_name} → {actual}", title="Room fixed")
            self.app.refresh_devices()  # type: ignore[attr-defined]
            self._set_status(f"[green]Moved to {actual}.[/green]")
        else:
            self._set_status(f"[red]Move not verified (now {actual!r}).[/red]")

    def _suggest_base(self, corr: Correlation) -> str:
        areas = set()
        for eid in corr.ha_entities:
            e = next((x for x in self.session.entities if x.entity_id == eid), None)
            if e and e.area_name:
                areas.add(e.area_name)
        if len(areas) == 1:
            return next(iter(areas))
        rooms = {a.get("room") for a in corr.hk_accessories.values() if a.get("room")}
        if len(rooms) == 1:
            return next(iter(rooms))
        return "Group"

    def _after_group(self, result) -> None:
        if result:
            self.session.stop_listen()
            self.app.notify(f"Renamed {result['renamed']}/{result['total']} as a group.",
                            title="Group named")
            self.app.refresh_devices()  # type: ignore[attr-defined]
            self.dismiss(None)

    def _after_proposal(self, mapping) -> None:
        if mapping:
            self.session.stop_listen()
            self.app.notify(f"Saved: {mapping['entity_id']} ↔ {mapping['hk_name']}",
                            title="Mapping confirmed")
            self.app.refresh_devices()  # type: ignore[attr-defined]
            self.dismiss(mapping)
        else:
            # cancelled — clear and keep listening (worker keeps _proposed True until reset)
            self.run_worker(self._after_cancel(), exclusive=True)

    async def _after_cancel(self) -> None:
        await self._fresh()
        self._set_status("[green]Listening[/green] — actuate one device.")

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.session.stop_listen()


class IdentifyScreen(ModalScreen):
    """Passive identify: actuate any device and see what fired on both sides.
    No matching, no writes — just a live readout of in-scope changes."""

    def __init__(self, session: ReconcileSession):
        super().__init__()
        self.session = session
        self._timer = None
        self._ha_idx = 0
        self._hk_since = None
        self._seen: set = set()
        self._count = 0
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="identify-box"):
            yield Static(
                "[b]Identify[/b] — actuate any device; I'll show what fired. "
                "[dim](no match, no writes)[/dim]",
                id="identify-head",
            )
            yield RichLog(id="identify-log", markup=True, wrap=False, max_lines=500)
            yield Static("Starting…", id="identify-status")
            with Horizontal(id="identify-buttons"):
                yield Button("Clear", id="clear")
                yield Button(self._sensors_label(), id="sensors")
                yield Button("Close", variant="primary", id="close")

    def _sensors_label(self) -> str:
        return f"Sensors: {'on' if self.session.include_sensors else 'off'}"

    async def on_mount(self) -> None:
        await self.session.start_listen()
        self._ha_idx = 0
        self._hk_since = self.session.listen_since
        self._timer = self.set_interval(1.0, self._tick)
        self._set_status("[green]Listening[/green] — actuate a device.")

    def _set_status(self, msg: str) -> None:
        self.query_one("#identify-status", Static).update(msg)

    def _friendly(self, eid: str) -> str:
        for e in self.session.entities:
            if e.entity_id == eid:
                return e.friendly_name
        return eid

    def _ha_line(self, d: dict) -> str | None:
        eid = d.get("entity_id", "")
        kind = ha_kind(eid)
        if kind is None:
            return None
        if kind == "sensor" and not self.session.include_sensors:
            return None
        if eid.startswith("light.") and is_segment_entity(eid):
            return None
        new = d.get("new_state")
        old = d.get("old_state")
        if new is None:
            return None
        o = old.get("state") if old else None
        n = new.get("state")
        ob = (old.get("attributes", {}) if old else {}).get("brightness")
        nb = new.get("attributes", {}).get("brightness")
        if o == n and ob == nb:
            return None
        chg = f"{o} → {n}" if o != n else f"brightness {ob} → {nb}"
        t = (new.get("last_changed") or "")[11:19]
        return f"{t} [cyan]HA[/cyan] {self._friendly(eid):<22.22} {eid}  [b]{chg}[/b]"

    def _hk_line(self, ev: dict) -> str | None:
        if ev.get("characteristic") not in active_chars(self.session.include_sensors):
            return None
        acc = ev.get("accessory", {})
        t = (ev.get("timestamp") or "")[11:19]
        return (f"{t} [magenta]HK[/magenta] {acc.get('name', ''):<22.22} "
                f"{ev.get('characteristic')} {ev.get('previous_value')} → "
                f"[b]{ev.get('value')}[/b]  ([dim]{acc.get('room')}[/dim])")

    async def _tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            log = self.query_one("#identify-log", RichLog)
            buf = self.session.ha_buffer()
            for d in buf[self._ha_idx:]:
                line = self._ha_line(d)
                if line:
                    log.write(line)
                    self._count += 1
            self._ha_idx = len(buf)
            try:
                evs = await self.session.hk.events(
                    since=self._hk_since, etype="characteristic_change", limit=100)
            except Exception:
                evs = []
            for ev in reversed(evs):  # API returns newest-first; log oldest-first
                key = (ev.get("timestamp"), ev.get("accessory", {}).get("id"),
                       ev.get("characteristic"), ev.get("value"))
                if key in self._seen:
                    continue
                self._seen.add(key)
                ts = ev.get("timestamp")
                if ts and (self._hk_since is None or ts > self._hk_since):
                    self._hk_since = ts
                line = self._hk_line(ev)
                if line:
                    log.write(line)
                    self._count += 1
            self._set_status(f"[green]Listening[/green] — {self._count} changes seen. "
                             "Actuate a device.")
        finally:
            self._busy = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "clear":
            self.query_one("#identify-log", RichLog).clear()
            self._count = 0
        elif event.button.id == "sensors":
            self.session.include_sensors = not self.session.include_sensors
            self.query_one("#sensors", Button).label = self._sensors_label()
            state = "on" if self.session.include_sensors else "off"
            self.query_one("#identify-log", RichLog).write(
                f"[yellow]— sensors {state} —[/yellow]")
        else:
            self.dismiss()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.session.stop_listen()


class ReconcileApp(App):
    CSS = """
    #body { height: 1fr; }
    #areas { width: 34; border: round $primary; }
    #right { width: 1fr; }
    #devices { height: 1fr; border: round $primary; }
    #status { height: auto; padding: 0 1; color: $text-muted; }
    #proposal-box {
        width: 74; height: auto; border: thick $primary; padding: 1 2;
        background: $surface;
    }
    #listen-box {
        width: 100; height: 28; border: thick $primary; padding: 1 2;
        background: $surface;
    }
    #picker-box {
        width: 54; height: 26; border: thick $primary; padding: 1 2;
        background: $surface;
    }
    #listen-log, #picker-list { height: 1fr; }
    #assign-box {
        width: 92; height: 32; border: thick $primary; padding: 1 2;
        background: $surface;
    }
    #identify-box {
        width: 100; height: 30; border: thick $primary; padding: 1 2;
        background: $surface;
    }
    #group-box {
        width: 76; height: auto; border: thick $primary; padding: 1 2;
        background: $surface;
    }
    #group-current, #group-msg { height: auto; padding: 1 0; }
    #help-box {
        width: 100; height: 40; border: thick $primary; padding: 1 2;
        background: $surface;
    }
    #help-body { height: 1fr; }
    #assign-table, #identify-log { height: 1fr; }
    #listen-buttons, #proposal-buttons, #assign-buttons, #identify-buttons,
    #group-buttons {
        height: auto; padding-top: 1;
    }
    Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("h", "help", "Help"),
        Binding("a", "add_light", "Add device"),
        Binding("t", "toggle_device", "Toggle"),
        Binding("space", "toggle_select", "Mark"),
        Binding("m", "move", "Move"),
        Binding("i", "identify", "Identify"),
        Binding("p", "assign_rooms", "Assign rooms"),
        Binding("e", "rename", "Rename"),
        Binding("s", "toggle_segments", "Segments"),
        Binding("n", "toggle_sensors", "Sensors"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, session: ReconcileSession):
        super().__init__()
        self.session = session
        self.failed: str | None = None
        self._area_index: list[str | None] = []
        self._row_entities: list[str] = []  # entity_id per device-table row
        self._selected: set[str] = set()    # entity_ids marked for Move

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield ListView(id="areas")
            with Vertical(id="right"):
                yield DataTable(id="devices", cursor_type="row", zebra_stripes=True)
                yield Static("Connecting…", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#devices", DataTable)
        table.add_columns("✓", "Name", "entity_id", "Type", "HK")
        try:
            await self.session.connect()
            await self.session.load_inventory()
        except Exception as e:
            self.failed = str(e)
            self.query_one("#status", Static).update(f"[red]Startup failed:[/red] {e}")
            return
        self._populate_areas()
        self._update_status()
        # Load bridge membership in the background; badges fill in once ready.
        self.run_worker(self._load_bridges_bg(), exclusive=False)

    async def _load_bridges_bg(self) -> None:
        try:
            await self.session.load_bridges()
        except Exception:
            pass
        self.refresh_devices()
        self._update_status()

    async def on_unmount(self) -> None:
        # Close HA ws + MCP session within the app task (same-task teardown).
        try:
            await self.session.close()
        except Exception:
            pass

    def _populate_areas(self) -> None:
        lv = self.query_one("#areas", ListView)
        prev = lv.index
        lv.clear()
        self._area_index = []
        for area in self.session.areas:
            n = len(self.session.entities_in_area(area.area_id))
            self._area_index.append(area.area_id)
            lv.append(ListItem(Label(f"{area.name}  ({n})")))
        n_un = len(self.session.entities_in_area(None))
        if n_un:
            self._area_index.append(None)
            lv.append(ListItem(Label(f"{UNASSIGNED}  ({n_un})")))
        if self._area_index:
            lv.index = prev if (prev is not None and prev < len(self._area_index)) else 0
            self.refresh_devices()

    def _current_area_id(self):
        lv = self.query_one("#areas", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._area_index):
            return _NO_AREA_SELECTED
        return self._area_index[idx]

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self.refresh_devices()

    def refresh_devices(self) -> None:
        table = self.query_one("#devices", DataTable)
        prev = table.cursor_row
        table.clear()
        self._row_entities = []
        area_id = self._current_area_id()
        if area_id is _NO_AREA_SELECTED:
            return
        for e in self.session.entities_in_area(area_id):
            typ = e.domain if e.kind == "actuator" else f"{e.domain}*"
            mark = "[cyan]●[/cyan]" if e.entity_id in self._selected else ""
            table.add_row(mark, e.friendly_name, e.entity_id, typ, self._hk_badge(e))
            self._row_entities.append(e.entity_id)
        if self._row_entities and prev is not None:
            table.move_cursor(row=min(prev, len(self._row_entities) - 1))

    def _hk_badge(self, e) -> str:
        """Durable HA↔HomeKit alignment badge: compares the device's HA area to
        the area of the bridge that exposes it (the room that actually sticks)."""
        s = self.session
        prefix = "" if s.match_for(e.entity_id) else "[dim]?[/dim] "
        if not s.bridges_loaded:
            return prefix + "[dim]…[/dim]"
        barea = s.bridge_area_of(e.entity_id)
        ha = e.area_name
        if ha is None:
            cell = "[dim]— no HA area[/dim]"
            return prefix + (cell + f"[dim] (on {barea})[/dim]" if barea else cell)
        if barea is None:
            return prefix + "[red]✗ no bridge[/red]"
        if normalize_room(ha) == normalize_room(barea):
            return prefix + "[green]✓ aligned[/green]"
        return prefix + f"[bold yellow]⇄ on {barea}[/bold yellow]"

    def _current_entity(self) -> str | None:
        table = self.query_one("#devices", DataTable)
        row = table.cursor_row
        if row is None or row >= len(self._row_entities):
            return None
        return self._row_entities[row]

    def _update_status(self) -> None:
        s = self.session
        acc = (s.hk_status or {}).get("accessories", "?")
        seg_state = "on" if s.show_segments else f"off ({s.segment_count()} hidden)"
        sen_state = "on" if s.include_sensors else f"off ({s.sensor_count()} hidden)"
        self.query_one("#status", Static).update(
            f"HA {s.ha_version} · {s.shown_count()} shown · {len(s.areas)} areas  |  "
            f"HomeKit · {acc} accessories · {len(s.hk_accessories)} mapped-candidates  |  "
            f"{len(s.store.all())} saved · {len(self._selected)} marked\n"
            f"[b]h[/b]=help  [b]a[/b]=add  [b]t[/b]=toggle  [b]space[/b]=mark  [b]m[/b]=move  "
            f"[b]i[/b]=identify  [b]e[/b]=rename  [b]s[/b]=seg:{seg_state}  [b]n[/b]=sens:{sen_state}  "
            f"[b]r[/b]=refresh  [b]q[/b]=quit"
        )

    async def action_toggle_segments(self) -> None:
        if self.failed:
            return
        self.session.show_segments = not self.session.show_segments
        self._populate_areas()
        self._update_status()

    async def action_toggle_sensors(self) -> None:
        if self.failed:
            return
        self.session.include_sensors = not self.session.include_sensors
        self._populate_areas()
        self._update_status()

    async def action_add_light(self) -> None:
        if self.failed:
            self.notify("Not connected — cannot add.", severity="error")
            return
        self.push_screen(ListenScreen(self.session))

    async def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_toggle_select(self) -> None:
        if self.failed:
            return
        eid = self._current_entity()
        if not eid:
            return
        self._selected.discard(eid) if eid in self._selected else self._selected.add(eid)
        self.refresh_devices()
        self._update_status()

    async def action_move(self) -> None:
        if self.failed:
            self.notify("Not connected — cannot move.", severity="error")
            return
        targets = sorted(self._selected) or (
            [self._current_entity()] if self._current_entity() else [])
        if not targets:
            self.notify("Mark device(s) with space, or highlight one.", severity="warning")
            return
        self.push_screen(MoveScreen(self.session, targets), self._after_move)

    def _after_move(self, result) -> None:
        self._selected.clear()
        self.refresh_devices()
        self._update_status()
        if result:
            # bridge membership changed → refresh the alignment badges
            self.run_worker(self._load_bridges_bg(), exclusive=False)

    async def action_toggle_device(self) -> None:
        if self.failed:
            return
        eid = self._current_entity()
        if not eid:
            self.notify("No device highlighted.", severity="warning")
            return
        try:
            await self.session.toggle_entity(eid)
            self.notify(f"Toggled {eid}", timeout=2)
        except Exception as e:
            self.notify(f"Toggle failed: {e}", severity="error")

    async def action_identify(self) -> None:
        if self.failed:
            self.notify("Not connected — cannot identify.", severity="error")
            return
        self.push_screen(IdentifyScreen(self.session))

    async def action_assign_rooms(self) -> None:
        if self.failed:
            self.notify("Not connected — cannot assign.", severity="error")
            return
        self.push_screen(AssignScreen(self.session))

    async def action_rename(self) -> None:
        if self.failed:
            self.notify("Not connected — cannot rename.", severity="error")
            return
        self.push_screen(RenameScreen(self.session))

    async def action_refresh(self) -> None:
        if self.failed:
            return
        self.query_one("#status", Static).update("Refreshing inventory…")
        try:
            await self.session.load_inventory()
        except Exception as e:
            self.query_one("#status", Static).update(f"[red]Refresh failed:[/red] {e}")
            return
        self._populate_areas()
        self._update_status()
        self.run_worker(self._load_bridges_bg(), exclusive=False)
