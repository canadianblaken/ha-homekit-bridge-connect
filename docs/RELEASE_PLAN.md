# haconnect — release / distribution plan

Goal: turn the working tool into something **shareable via git**, installable both
**standalone** and as a **Home Assistant add-on with a sidebar panel** — without
leaking secrets and without pretending it works for everyone.

This was written in two passes. The **Pass-2 review** section at the end lists the
gaps the second pass caught and how the plan changed; they're already folded in
above.

---

## 0. Decisions up front (need your input)

| Question | Default I'd pick | Why it matters |
|---|---|---|
| Repo name | `haconnect` | nicer than `ha-homekit-reconcile`; used everywhere |
| Public or private | **private first**, public later | a public repo's git history is forever — secret-scrub must be perfect before the first push |
| License | MIT | permissive, simplest for a hobby tool |
| Copyright name | _your name/handle_ | goes in LICENSE |
| GitHub owner | _your user/org_ | repo URL, add-on repo URL |
| Audience target | HomeClaw users (for now) | full general-HA support is a separate, larger effort |

---

## 1. The honest constraints (must be stated in the README)

1. **HomeKit side needs a proxy.** Editing Apple Home rooms/names and reading
   HomeKit state is done over an MCP-SSE proxy (today: **HomeClaw** on a Mac).
   HA cannot do this itself. → Hard requirement; most HA users don't have it.
2. **The Move / HK-alignment features assume one HomeKit bridge per room.** Users
   with a single bridge still get Add / Identify / Rename / room-set, but not the
   bridge-based Move. → Document; degrade gracefully (don't crash on 0/1 bridges).
3. **It runs with broad privileges** — an admin HA token (reads everything, can
   toggle devices, and **writes HA HomeKit-bridge config**). → State the trust model.

Decoupling move: define a **"HomeKit proxy contract"** (the MCP tools the tool
relies on: `homekit_status, homekit_device_map, homekit_rooms, homekit_events,
homekit_accessories, homekit_manage`). Then HomeClaw is *one implementation*, and
anyone can point haconnect at a compatible proxy. We do **not** bundle or
redistribute HomeClaw (unknown license) — we just require a compatible proxy URL.

---

## 2. Repository layout (one repo, two install paths)

```
haconnect/
├── reconcile/                 # the app (importable, runnable: python -m reconcile)
│   ├── __init__.py ... app.py, core.py, bridges.py, ...
│   └── serve.py               # `haconnect serve` → textual-serve (browser UI)
├── tests/                     # PURE-LOGIC unit tests (no network) for CI
│   ├── test_matcher.py
│   ├── test_phase2_phase3.py
│   └── test_bridge_plan.py
├── addon/                     # the Home Assistant add-on wrapper
│   ├── config.yaml            # ingress + sidebar panel + options schema
│   ├── Dockerfile
│   ├── run.sh
│   └── DOCS.md / icon.png
├── docs/
│   ├── RELEASE_PLAN.md        # this file
│   ├── ARCHITECTURE.md        # serial==entity_id, bridge-per-room, the move recipe
│   └── PROXY_CONTRACT.md      # the MCP tools haconnect expects
├── config.example.toml        # GENERIC placeholders only
├── pyproject.toml             # deps + `haconnect` console entry point
├── requirements.txt           # pinned, for the add-on Docker build
├── repository.yaml            # makes the repo an HA ADD-ON REPOSITORY
├── README.md                  # requirements FIRST, then quickstart, 3 install paths
├── LICENSE
├── CHANGELOG.md
└── .gitignore
```

One repo gives **both**: `git clone` → standalone, *and* "add this repo URL in HA
add-on store" → sidebar add-on.

---

## 3. Secrets & safety (do this BEFORE any commit — most important section)

The snapshot folder contains real secrets/user data. **Never** reuse it as the
git root as-is. Build the publish repo clean.

- **Never commit:** `.env` (real HA JWT), `mappings.json` (user data),
  `config.toml` (LAN IPs), `.venv/`, `__pycache__/`, `/tmp` dev artifacts.
- `.gitignore` MUST list: `.env`, `config.toml`, `mappings.json`, `mappings.tmp`,
  `.venv/`, `__pycache__/`, `*.pyc`, `*.bak`.
- **Genericize `config.example.toml`** → placeholder IPs (`ws://HA_HOST:8123/...`,
  `http://HOMEKIT_PROXY:8788/sse`), no real addresses.
- **Scrub for leaks before first commit** (gate the push on this):
  `git grep -nE '192\.168\.|eyJ|HA_TOKEN='` must return nothing tracked.
- **Pre-commit gate:** verify `git check-ignore .env config.toml mappings.json`
  lists all three; `git status` shows none of them staged.
- Public repos: assume the first push is **permanent**. If anything slips, the
  token must be **revoked in HA**, not just deleted from the repo.

---

## 4. Code changes needed (not just packaging)

1. **Console entry point** in `pyproject.toml`:
   `[project.scripts] haconnect = "reconcile.__main__:main"` → `pip install` gives a
   `haconnect` command. Fold `serve.py` in as `haconnect serve` and `scan.py` as
   `haconnect scan` (or `tools/`).
2. **Config: HomeKit proxy URL is required & named generically** (already is —
   `homekit.sse_url`). Add a clear error if unreachable.
3. **Add-on auth path:** support connecting to HA via the **Supervisor** instead of
   a pasted token — `ws://supervisor/core/websocket` + `SUPERVISOR_TOKEN` env, when
   `homeassistant_api: true`. Add `config.py` support for "token from env" and a
   supervisor WS URL. (Bridge edits use the REST options flow — that also works via
   `http://supervisor/core/api`.) **Verify the options-flow + config-entry reload
   endpoints are reachable through the supervisor proxy** (spike).
4. **Graceful with 0/1 bridges:** if `bridge_snapshot()` finds no per-room bridges,
   the HK column shows a neutral state and Move explains "single/!no bridge setup"
   instead of erroring.
5. **Extract pure-logic unit tests** (matcher, phase2/phase3 plans, `plan_move`,
   `_bridge_area` resolution) into `tests/` — these need **no** HA/HomeClaw and run
   in CI. Keep the live smoke-tests as a separate, manual `scripts/` set.

---

## 5. The HA add-on (sidebar) — design + the one real risk

- `addon/config.yaml`: `ingress: true`, `panel_icon: mdi:home-sync`,
  `panel_title: HAConnect`, `homeassistant_api: true`, `init: false`, an `options`
  schema for the **HomeKit proxy URL** (and optional HA URL/token override),
  `ports` none (ingress only).
- `Dockerfile`: from an HA base image, `pip install` the package + `textual-serve`.
- `run.sh`: launch `haconnect serve` bound to the ingress port.
- **RISK / SPIKE FIRST:** `textual-serve` must work **behind ingress** (served
  under `/api/hassio_ingress/<token>/…`, websockets proxied). Validate this early.
  If it doesn't behave under a sub-path, fall back to **ttyd** (a real web terminal
  running `python -m reconcile`) behind ingress — also gives the sidebar TUI.
- Distribute by adding `repository.yaml` at repo root → the repo *is* an add-on
  repository. Users: Settings → Add-ons → ⋮ → Repositories → paste the URL.

---

## 6. Docs

- **README** (order matters): 1) one-line what it is, 2) **Requirements** (HA +
  admin token, **a HomeKit MCP proxy like HomeClaw**, the bridge-per-room note),
  3) Quickstart standalone, 4) HA add-on install, 5) the safety/trust model,
  6) what each key does (link the in-app `h`).
- **docs/ARCHITECTURE.md**: the genuinely useful discoveries — `serial == entity_id`,
  bridge-per-room ⇒ room follows the bridge, the move recipe (options flow + reload,
  no restart, UUID changes), the 150-accessory bridge limit context.
- **docs/PROXY_CONTRACT.md**: the MCP tool surface haconnect needs (so it's not
  HomeClaw-only on paper).
- **LICENSE**, **CHANGELOG.md** (Keep-a-Changelog), optional CONTRIBUTING.

---

## 7. CI (GitHub Actions)

- `lint`: `ruff` (or flake8) + `python -m compileall reconcile`.
- `unit`: run `tests/` (pure logic, no network).
- `addon-build` (optional): the community **home-assistant/builder** action to
  confirm the add-on image builds for `amd64`/`aarch64`.
- No live HA/HomeClaw in CI — those stay as manual smoke tests.

---

## 8. Versioning & releases

- **SemVer**, start `0.1.0`. `CHANGELOG.md` per release.
- Add-on version in `addon/config.yaml` tracks the app version (HA shows updates
  when it bumps).
- Tag releases (`v0.1.0`) → GitHub Releases. Optional: screenshots / a short GIF of
  the TUI (the HK-alignment column is the money shot).

---

## 9. Runbook — "when you connect me to a git"

Order is deliberate (scrub before push):

1. **Create clean publish dir** from the snapshot, copying only safe files
   (exclude `.env`, `config.toml`, `mappings.json`, `.venv`, `__pycache__`).
2. Add `.gitignore`, genericize `config.example.toml`, add `LICENSE`,
   `pyproject` entry point, `requirements.txt`, `README` (requirements first),
   `docs/`, `repository.yaml`, `addon/` skeleton.
3. **Secret-scrub gate** (section 3) — must pass.
4. `git init`, `git add -A`, **review `git status`** (no secrets), first commit.
5. `git remote add origin <the URL you connect>`; `git branch -M main`;
   `git push -u origin main`. (Confirm public vs private first.)
6. Tag `v0.1.0`, draft a GitHub Release.
7. Then iterate: add-on spike (section 5), CI, docs polish.

I will **not** run `git add .` blindly or push without the scrub gate passing and
your confirmation of public/private.

---

## 10. Milestones (suggested order)

- **M0 — Repo foundation:** clean dir + gitignore + generic config + LICENSE +
  README(reqs first) + secret-scrub + init + push. *(Standalone already works.)*
- **M1 — Standalone polish:** console entry point, `requirements.txt`,
  ARCHITECTURE/PROXY_CONTRACT docs, extract unit tests, CI (lint+unit).
- **M2 — Generalize + add-on auth:** proxy-contract decoupling, graceful 0/1
  bridges, Supervisor-token auth path (+ spike that the options-flow works via the
  supervisor proxy).
- **M3 — HA add-on:** Dockerfile + config.yaml (ingress+panel) + run.sh +
  textual-serve-behind-ingress spike (ttyd fallback) + repository.yaml.
- **M4 — Release:** CHANGELOG, v0.1.0 tag/Release, screenshots; decide whether to
  invest in single-bridge general-HA support.

---

## Pass-2 review — gaps the second pass caught (now folded in above)

1. **Snapshot has real secrets.** Pass 1 said "gitignore them"; Pass 2: don't even
   git-init in a folder containing `.env`/token — build a **clean publish dir** and
   gate the push on an explicit secret-scrub. (§3, §9)
2. **HomeClaw coupling & unknown license.** Pass 2 added the **proxy-contract**
   decoupling and an explicit "we don't redistribute HomeClaw." (§1, §6)
3. **Add-on auth is a code change, not just config.** Pass 2 added the
   Supervisor-token WS path + a spike that the **options-flow / config-entry reload
   work through the supervisor proxy** (the whole Move feature depends on it). (§4.3)
4. **textual-serve behind ingress is unproven.** Pass 2 made it an **early spike**
   with a **ttyd fallback** rather than an assumption. (§5)
5. **Bridge-per-room is an assumption that can crash.** Pass 2 added graceful 0/1
   bridge handling. (§1, §4.4)
6. **CI can't use live HA/HomeClaw.** Pass 2 split **pure-logic unit tests** (CI)
   from live smoke tests (manual), and named which logic is unit-testable. (§4.5, §7)
7. **Security posture undocumented.** The tool writes HA config + toggles devices;
   Pass 2 requires stating the trust model in the README. (§1, §6)
8. **Public-history permanence.** Pass 2: private-first, revoke-token-if-leaked,
   never-commit-once. (§0, §3)
