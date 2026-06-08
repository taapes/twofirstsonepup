# FPL Draft Keeper League

A public website for a Fantasy Premier League **Draft Keeper** league. It syncs
data from the official FPL Draft API and layers on custom league rules (keepers,
waivers, trades, drafts, injury list, cups, anti-tanking). The system runs
year-round and must be able to reconstruct league state for any gameweek.

Full feature spec and database schema live in `docs/requirements.md` — read it
before any non-trivial work.

## Stack

- **Backend:** FastAPI (Python) — REST API, admin sync endpoints, business rules
- **ORM:** SQLAlchemy — declarative models
- **Migrations:** Alembic — `alembic revision --autogenerate -m "..."` then `alembic upgrade head`
- **Database:** PostgreSQL, hosted on **Neon** (managed, free tier, Oregon /
  AWS us-west-2 to sit near Render). Chosen over Render Postgres because Render's
  *free* DB self-deletes after 90 days — fatal for a year-round historical app.
  SSL required (`?sslmode=require`). Use Neon's **direct** connection string for
  Alembic migrations; the pooled (`-pooler`) endpoint is fine for app runtime.
- **Python:** pinned to **3.13** via `.python-version` (read by both `uv`
  locally and Render). 3.14 has no wheels for the pinned pydantic/fastapi stack.
- **Hosting:** **Render** (auto-deploys from GitHub; runs `uvicorn main:app --host 0.0.0.0 --port $PORT`)
- **Scheduled sync:** **GitHub Actions** cron → hits `POST /admin/sync` (we deliberately avoid paid Render cron)
- **Repo:** GitHub (`twofirstsonepup`)
- **Frontend:** **FastAPI-served Jinja2 templates** (decided in step 3). Server-
  rendered HTML from the same app (`templates/`), reading the same precomputed
  query layer (`services.py`) as the JSON API. Revisit React only if the UI
  outgrows server rendering.

## Commands

<!-- Fill in / correct as the project solidifies -->
- Local setup: `uv venv --python 3.13 .venv && uv pip install -r requirements.txt`
- Dev server: `uvicorn main:app --reload`
- New migration: `alembic revision --autogenerate -m "<message>"`
- Apply migrations: `alembic upgrade head`
- Dev deps (tests + local proxy workaround): `uv pip install -r requirements-dev.txt`
- Tests: `pytest` (rules engine unit tests in `tests/`)
- Env vars: Neon DB URL + sync secret live in env (Render dashboard / local `.env`, never committed)

## Architecture: Pull -> Normalize -> Store -> Serve

We do NOT serve live FPL API calls to the frontend. The flow is:

1. GitHub Actions cron calls `POST /admin/sync` (protected endpoint).
2. Sync pulls from the FPL Draft API.
3. Normalize into our schema.
4. Apply league business rules.
5. Store results in Postgres.
6. API serves **precomputed** responses (e.g. `GET /v1/leagues/{id}/home`, `/v1/standings`, `/v1/rosters`).

Why: fast frontend, historical reconstruction, rule enforcement, easier
debugging, resilience to FPL API outages. Preserve this pattern — don't add
live FPL calls into request handlers.

## Code layout & admin-write pattern

- `services.py` — read query helpers + rule-enforcing write ops, shared by the
  API and homepage (never call the FPL API here).
- `rules.py` — pure, testable rule functions; raises `RuleViolation` on illegal
  admin actions.
- `api.py` — public read-only `/v1` router. `admin.py` — commissioner write
  router under `/admin`, guarded by `require_admin` (`auth.py`).
- **Admin writes** (e.g. injury list place/return) require the `X-Auth-Token`
  header == `SYNC_AUTH_TOKEN`. Endpoints resolve the league, call a `services`
  function that enforces rules, and map `RuleViolation` -> HTTP 400. Reuse this
  pattern for keepers/trades/cups.

## The two-truths boundary (keep sacred)

- **FPL canonical truth** (from the official API, treat as source of truth):
  player IDs, scores, transactions, standings, rosters, gameweek info.
- **League custom truth** (ours, our tables, our rules): keeper eligibility,
  IL logic, discovery draft, anti-tanking, ineligible players, cup structure,
  commissioner data, draft/trade/waiver history.

Manager identity: `managers.name` is the **FPL team name** (synced; changes
year to year). `managers.display_name` is the **person** (e.g. "Kevin T") — a
league-custom field sync never overwrites, and the **stable identity** for
historical/manager-centric views. Use `Manager.display` (display_name or name)
for all manager labels; services already do.

League logic must never corrupt synced canonical data. Custom state lives in its
own tables alongside, not by mutating FPL-sourced rows.

## FPL Draft API endpoints in use

`/bootstrap-static`, `/league/{league_id}/details`, `/event/{gw}/live`,
`/entry/{team_id}/event/{gw}`, `/draft/league/{league_id}/trades`.

## Schema

Full schema in `docs/requirements.md`. Core tables: `leagues`, `managers`,
`players`, `gameweeks`, `rosters`, `transactions`, `trades`, `injury_list`,
`keeper_exceptions`, `draft_picks`, `draft_lottery`, `gameweek_points`,
`tournaments`, `tournament_matches`, `commissioner_alerts`.

PK convention: most tables use UUID PKs; `gameweeks.id` is the GW number (1-38).
Keep models consistent with this — reconcile any integer-vs-UUID mismatches
before generating migrations, since Alembic encodes whatever the models say.
Prefer DB-level foreign keys.

## Build order

Build the data layer before logic, and logic before polish:

1. **Schema + Alembic migrations** — translate the spec tables into SQLAlchemy
   models and an initial migration.
2. **FPL sync** (`/admin/sync`) — pull/normalize/store canonical data first.
   Everything depends on having real data.
3. **Read-only serve endpoints + minimal homepage** — standings, IL tracker,
   infractions. Proves the pipeline end to end.
4. **Business rules engine + admin** — keepers, waivers, trades, drafts, cups.
   The genuinely hard part; build last on a solid foundation, with tests.

## League rules that are easy to get wrong (the actual hard part)

The rules engine — not the infrastructure — is where the difficulty lives.
Write tests for these. They are custom and non-obvious:

- **Keepers:** 15-man rosters; up to 5 keepers/season (6 if a discovery keeper
  applies). Max 4 years of keeper eligibility — track the clock per player.
  Waiver keepers capped at 2 (from 2025 on). Traded players KEEP keeper history;
  dropped players LOSE keeper eligibility.
  *Phase 1 (done):* eligibility is **derived**, not manually entered — roster
  continuity across GW snapshots determines drops (a gap not covered by the IL
  or a trade = dropped → clock resets); acquisition (draft/trade/waiver) and
  keeper-years come from roster history + synced trades (`sync_trades`, from the
  FPL `/draft/.../trades` feed) + Option-B `keeper_seeds` (commissioner-entered
  prior years for players already kept entering 25/26). `rules.keeper_*`,
  `services.get_keepers`, `GET /v1/.../keepers`, `POST /admin/.../keeper-seeds`.
  CAVEAT: derivation needs IL data to explain roster gaps; 25/26 has no IL
  records in our system, so legitimate IL absences look like drops — the
  25/26→26/27 report needs commissioner review for gap cases. Accurate going
  forward. *Phase 2 (done):* keeper SELECTION submission + cap validation
  (`rules.validate_keeper_selection`, `services.submit_keepers`,
  `POST /admin/.../keepers`, `GET /v1/.../keeper-selections/{year}`,
  `keeper_selections` table) — enforces ≤5 keepers (+1 with a discovery keeper),
  ≤2 waiver-acquired (discovery excluded), all eligible; replaces the prior
  submission for that season. *Phase 3 (TODO):* main draft (lottery-weighted R1,
  reverse-standings R2+) and discovery draft (snake, Sept), which also produces
  the discovery (6th) keeper that raises the cap.
- **Waivers vs. free agency:** Waiver period = start of a GW until 24h before the
  next GW. Final 24h before GW start = free agency. Enforce limits/eligibility.
- **Player eligibility:** Player added to FPL *after* the league draft date is
  ineligible (`players.is_eligible = false`). Surface in the ineligible report.
- **Injury list:** One IL player per manager. Minimum 4-GW stay. Replacement must
  be same position. Returns after GW38 or via waiver. Admin-managed.
- **Anti-tanking:** Flag a manager when >=3 of their ROSTERED players (the whole
  15-man squad, not just the XI) record 0 minutes in each of >=3 CONSECUTIVE
  gameweeks. Across-gameweek rule, players may differ week to week. Thresholds
  are constants in `rules.py`. (Whole-squad scope was chosen deliberately even
  though it flags most of the league — see [[anti-tanking-whole-squad-choice]].)
  Show infractions on homepage and admin panel.
- **Trades:** Allowed only end of GW38 -> Jan 31. Player-for-player,
  pick-for-player, or pick-for-pick. Conditions free-text initially. Trades
  update keeper clocks and the draft board.
- **Discovery draft:** Snake, 2 picks/manager, held in September. If a picked
  player joins the PL during the year they become a bonus (6th) keeper — only
  one bonus keeper allowed.
- **Main draft:** Lottery mechanics are OUT of the app — the commissioner sets
  the round-1 order (`POST /admin/.../draft/order`, stored in `draft_lottery`).
  Rounds 2+ = reverse standings. Keepers are FREE: a manager makes 15−keepers
  picks (holds slots in rounds 1..(15−K)). The board is computed on read
  (`services.get_draft_board`, `GET /v1/.../draft/{year}`) from order + keeper
  counts + pick trades, so it reflects trades live. **Pick trades** (draft AND
  discovery picks) and ad-hoc player trades are commissioner-entered (not in the
  FPL feed): `POST /admin/.../draft/trade-pick|trade-player`; a pick trade
  reassigns the (season, type, round, original-owner) slot's owner. Selections
  recorded live via `.../draft/record-pick`. *TODO:* discovery draft order
  (snake) + homepage board view.
- **Cups:** Cup (top 6) and Pup Cup (bottom 4 + Cup losers), start after GW28,
  each round spans 2 GWs, admin sets GWs per round. Auto-score 2-week totals.
  Implemented as auto-generated single-elim brackets seeded from H2H standings
  through GW28: Cup = top-2 byes (QF 3v6/4v5 -> SF vs seeds 1,2 -> Final); the
  two Cup QF losers feed the Pup Cup SFs (Pup play-in 7v10/8v9 first). Ties break
  to the better seed. Admin: `POST /admin/.../cups/generate` then
  `.../cups/score-round` per round (gw1, gw2). See `services.generate_cups` /
  `score_cup_round`.
- **Payouts:** Config-driven (`rules.PAYOUT_STRUCTURE`), auto-calculated from
  final standings + cup results. Base pot = entry_fee × managers (25/26 $125;
  rises 26/27 $150, 27/28 $175, 28/29 $200). Pct of pot: League 1st 40%, 2nd
  15%, 3rd 5%; Cup 1st 25%, 2nd 10%, 3rd 5%. Pup Cup winner flat $150. Last-place
  fine ($125) + other fines added to League 1st; last place shown owing it.
  Cup 3rd place comes from a 3rd-place playoff (the two SF losers, scored in the
  final's GWs). `GET /v1/.../payouts`, `services.get_payouts`. Weekly entry and
  team-sale clause are separate pools, not in this calc yet.

## Testing ahead of the season & data quality

Three layers protect the live data when testing before the draft:

1. **Neon test branch (true isolation — preferred).** In the Neon console, branch
   `main` (instant copy-on-write of all data). Point a local run or a separate
   Render service at the branch's connection string and set `APP_ENV=test` (shows
   a TEST banner site-wide so it's never mistaken for prod). Test freely; reset or
   delete the branch when done. Prod is untouched.
2. **Snapshot/restore (`snapshot.py`).** `save` dumps the whole app DB to a JSON
   file; `restore` reloads it exactly — revert fake drafts/trades on the live DB
   if you test there. `snapshots/` is gitignored.
3. **Editing lock.** `leagues.writes_locked` (toggled at `/admin/health`) freezes
   public picks/trades; the logged-in commissioner can still write. Use it to
   keep data clean outside the live-draft window.

**Data-quality aids:** idempotent upsert sync; the two-truths boundary (sync never
overwrites custom data); trade reconciliation (site+FPL dedupe); the standings
audit log; `GET /admin/health` runs integrity checks (roster sizes, standings
coverage, unseeded keepers, malformed pick trades). Public writes are
unauthenticated by design — no per-user accountability yet.

## Working style

- Work in scoped chunks, one feature area per session — never the whole app.
- Propose a plan before writing code on anything non-trivial.
- Keep changes reviewable; the human commits between pieces.
- When you settle a convention or finalize a command, update this file.
