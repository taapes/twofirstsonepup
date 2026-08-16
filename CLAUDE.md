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
- Tests: `pytest`. Pure rule tests run anywhere; anything touching the DB needs
  `TEST_DATABASE_URL` (local Postgres recipe in `tests/conftest.py`) and **silently skips
  without it** — confirm a run says `passed`, not `skipped`, before trusting it.
- **Mutation testing: always run with `PYTHONDONTWRITEBYTECODE=1`.** The house style for
  verifying a test is to break the code, confirm the test fails, then restore the file. But
  `cp`-ing a source file back leaves `__pycache__` holding the **mutated** bytecode, and
  Python happily imports it — so mutations report "no bite" and the *baseline* goes red
  against a clean tree. The tell is a `grep` of the file disagreeing with what
  `python -c "import x; print(x.CONST)"` reports. `find . -name __pycache__ -not -path
  "./.venv/*" -exec rm -rf {} +` clears a tree already in that state.
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
`players`, `pl_teams`, `gameweeks`, `rosters`, `transactions`, `trades`, `injury_list`,
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

- **Goalie teams (from 2026):** goalkeepers are NOT drafted or kept individually.
  A manager drafts ONE Premier League club and owns every keeper at it, so a squad is
  **13 outfielders + 1 goalie team = 14 picks**, not 15. Governed by
  `leagues.goalie_team_mode` (`off | redraft | keeper`, default `off`) — per-season on
  purpose: `get_draft_board` regenerates its slots on EVERY read with no season
  parameter, so a global 15→14 would retroactively truncate every archived board.
  `rules.ROSTER_SIZE` stays 15; use `rules.draft_picks_per_manager(mode)` and
  `generate_draft_slots(..., picks_per_manager=...)`.
  **A club is `pl_teams`, keyed on FPL's PERMANENT team `code`** — `teams[].id` in
  bootstrap is the alphabetical 1-20 index WITHIN a season and is reassigned every
  August, the same trap as `players.fpl_id` one table over. Rows are never deleted; a
  relegated club keeps its row and loses `is_current_pl`. Populated by
  `sync._upsert_pl_teams` from the payload sync already fetched and used to discard.
  **Who owns a club is derived, never stored as a keeper list** (`goalie_team_keepers`,
  `goalie_team_owner`): you own whoever keeps for that club TODAY, so a January signing
  joins and a sale leaves with nothing to reconcile.
  Two rules live in `record_pick` (which still enforces no squad quotas — a sixth
  defender is legal, and a test pins that): a manager gets ONE club
  (`_team_unavailable_reason`), and a manager down to their last slot with no club must
  spend it on one (`_goalie_team_required_reason` — deliberately separate, because
  `_unavailable_reason` doubles as search's taken-oracle and a striker is not "taken"
  because you're out of slots). The second also guards `trade_pick`.
  Wire format is `team_code` (the permanent club code) alongside `player_fpl_id`,
  exactly one required. **Availability is keyed on `(kind, id)`, never `fpl_id` alone**
  — every club row carries `fpl_id: None`, which every DEPARTED player also matches.
  `search_players` needs `include_teams=True` when used as an availability ORACLE
  (`approve_queued_pick`) rather than as a search. And `get_draft_board` MUST render a
  label for a club pick: `next_open_pick` treats a falsy `player` as "on the clock", so
  a club that renders blank freezes the draft forever.
  Under `keeper` mode a club is one of the ≤5 keepers, on its own clock
  (`_derive_gk_team_keeper_status`) — a club has no `rosters` rows, so ownership is a
  discrete per-season fact and the history is keyed on the **FPL entry id**, not
  `managers.id` (one manager row per season). A trade transfers the clock and the label
  unchanged; relegation voids the keeper and returns the slot to the draft.
  `draftprep.Shape` carries the squad shape (`FPL_SHAPE` / `GOALIE_TEAM_SHAPE`,
  `shape_for(mode)`) so both eras stay simulable; clubs are RANKED
  (`goalie_team_values`) and never simulated — 20 clubs for 10 managers is no scarcity.
- **Keepers:** 15-man rosters; up to 5 keepers/season (6 if a discovery keeper
  applies). Max 4 years of keeper eligibility for a draft- or trade-acquired
  player (`rules.KEEPER_FRESH_DRAFT`); a waiver/FA-acquired player — including one
  dropped and re-acquired, which always relabels "waiver" regardless of original
  acquisition — gets only 3 (`rules.KEEPER_FRESH_WAIVER`) — track the clock per
  player. Waiver keepers capped at 2 (from 2025 on). Traded players KEEP keeper
  history; dropped players LOSE keeper eligibility.
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
  submission for that season.
  **Selections are PRIVATE until they lock** (`rules.keepers_revealed` = `keepers_locked`
  or the phase has left offseason; `enter_draft_phase` sets both, so the draft reveals
  them). Redacted in **services, not templates** — `/v1` is exempt from the login gate and
  has no viewer, so `_derive_keeper_status`'s `kept_for`/`kept_all` and `get_keepers` /
  `get_keeper_selections` / `search_players`' viewer args all **default to disclosing
  nothing**; routes opt in via `ui._viewer`. Two things that look like the gate but aren't:
  `keeper_candidates` builds `selected`/`discovery` from its own query, so `GET
  /keepers/candidates` is protected only by its `can_act_as` check; and
  `approve_queued_pick` passes `kept_all=True` as a **correctness** filter — without it the
  autodraft hands out another manager's keeper, since `record_pick` has no availability
  guard. The draft board's pick count (`15 − keepers`) still leaks the count, deliberately. *Phase 3 (TODO):* main draft (lottery-weighted R1,
  reverse-standings R2+) and discovery draft (snake, Sept), which also produces
  the discovery (6th) keeper that raises the cap.
- **Waivers vs. free agency:** Waiver period = start of a GW until 24h before the
  next GW. Final 24h before GW start = free agency. Enforce limits/eligibility.
- **Player eligibility:** Player added to FPL *after* the league draft date is
  ineligible (`players.is_eligible = false`). Surface in the ineligible report.
- **Injury list:** One IL player per manager. Minimum 4-GW stay. Replacement must
  be same position. Returns after GW38 or via waiver. **Manager self-service**
  (`POST /il/place|return|release`, gated by `can_act_as`; reuses `place_on_il`/
  `return_from_il`) on the My Team page, with an end-of-season "add back or release"
  prompt; admin can still act for anyone.
- **Anti-tanking:** Flag a manager when >=3 of their ROSTERED players (the whole
  15-man squad, not just the XI) record 0 minutes in each of >=3 CONSECUTIVE
  gameweeks. Across-gameweek rule, players may differ week to week. Thresholds
  are constants in `rules.py`. (Whole-squad scope was chosen deliberately even
  though it flags most of the league — see [[anti-tanking-whole-squad-choice]].)
  Show infractions on homepage and admin panel.
- **Trades:** Allowed only end of GW38 -> Jan 31. Player-for-player,
  pick-for-player, or pick-for-pick. Conditions free-text initially. Trades
  update keeper clocks and the draft board.
  **A commissioner-entered player trade moves the player via an OVERLAY ON READ**
  (`services.player_ownership`, the sibling of `pick_ownership`), never by writing
  `rosters` — those are FPL-canonical, and a fabricated row would be indistinguishable
  from a synced one to `get_transactions` / anti-tanking / `reconcile_absences`.
  Discriminator: `player_id` set, `pick_round`/`fpl_trade_id`/`event_gw` all NULL —
  a trade FPL processed is already in the snapshots, so overlaying it would move the
  player twice. It **self-retires**: `sync_trades` back-fills those fields when the feed
  confirms the move, exactly when the snapshot takes over — hence no phase gate.
  Applied only when `from_manager` is the current owner, so a typo'd direction fails
  closed (`/admin/health` → "site trades applied" surfaces the ones that didn't apply).
  **The acquisition label follows the player** — a waiver pickup traded to you still
  eats one of your two waiver keeper slots — and the clock arrives already capped by any
  drop the sender took, or trading out and back would launder the penalty away. Roster
  HISTORY (presence, drops, IL) stays keyed to the manager who actually rostered him.
  **A trade changes ownership and nothing else** — this holds for synced in-season trades
  too, via `rules.keeper_status(traded_from=...)`: the clock transfers unchanged and the
  label comes from the sender, chained the whole way through A->B->C. The sender must be
  evaluated as of the trade (`_dropped(..., upto=event_gw-1)`), or their empty roster tail
  reads as a drop and the receiver inherits a bogus `waiver`/capped clock. The receiver's
  OWN later drop still caps it. A submitted keeper for a player since traded away is **not**
  deleted and doesn't block the trade — it just stops counting
  (`effective_keeper_selections`), so the manager is one keeper short and gets the pick
  back. `trades.created_at` is the only reliable ordering (`date` is NULL on
  commissioner rows and the PK is a random uuid4); both ownership readers depend on it.
- **Discovery draft:** Snake, 2 picks/manager, held in September. If a picked
  player joins the PL during the year they become a bonus (6th) keeper — only
  one bonus keeper allowed. *Built:* `services.get_discovery_board` (2-round snake
  over reverse standings), `GET /discovery/{year}` + `/search` + `POST .../pick`
  (draft_type='discovery'), gated by the `discovery_open` phase flag.
- **Main draft:** Lottery mechanics are OUT of the app — the commissioner sets
  the round-1 order (`POST /admin/.../draft/order`, stored in `draft_lottery`).
  Rounds 2+ = reverse standings. Keepers are FREE: a manager makes 15−keepers
  picks (holds slots in rounds 1..(15−K)). The board is computed on read
  (`services.get_draft_board`, `GET /v1/.../draft/{year}`) from order + keeper
  counts + pick trades, so it reflects trades live. **Pick trades** (draft AND
  discovery picks) and ad-hoc player trades are commissioner-entered (not in the
  FPL feed): `POST /admin/.../draft/trade-pick|trade-player`; a pick trade
  reassigns the (season, type, round, original-owner) slot's owner. Selections
  recorded live via `.../draft/record-pick`; `record_pick` refuses a player who is
  already kept or already drafted (`_unavailable_reason`, mirroring `search_players`'
  `taken` logic). **That guard is NOT waived by `overwrite`** — that flag grants
  permission to replace a *slot*, not to take an unavailable player, and the live draft
  passes `overwrite=is_admin`. To reassign, fix the keeper selection or
  `delete_draft_pick` the conflicting pick first.
  **Reverse standings means the ADJUSTED standings** (`get_standings`, deltas from
  `standing_adjustments` applied and re-ranked) — a post-season deduction changes where
  a team finished, so it changes the order. `_reverse_standings_managers` used to sort
  the raw synced `Standing.rank`, which made the standings page and the draft board
  disagree. `get_payouts` had the same bug and now resolves league 1st/2nd/3rd and the
  last-place fine through `get_standings` too. **Three readers of the finishing order —
  the standings page, the draft order and the money — all go through `get_standings`;
  never sort `Standing.rank` directly.**
  **Order overrides** (`draft_order_override`): the commissioner can replace the derived
  order for rounds 2+ — `round IS NULL` is the base for every round from 2 on, a row with
  a round beats it for that round, and editing one position within a round is how a single
  slot gets reassigned. Round 1 is never expressible here; it keeps `draft_lottery`.
  Precedence in `rules.generate_draft_slots`: round override → base → reverse standings,
  with the keeper filter unchanged. An override may deliberately give someone two slots in
  a round, so the editor shows a per-manager pick count rather than blocking it.
  **`pick_number` is positional**, so any order change shifts what a number means:
  `get_draft_board` takes the owner of an already-picked slot from the stored
  `DraftPick.manager_id` (flagging `reassigned` when it disagrees with the computed
  owner), or a reorder would silently re-attribute completed picks.
- **Cups:** Cup (top 6) and Pup Cup (bottom 4 + the two Cup R1 losers) start at GW28,
  each round spans 2 GWs (admin sets GWs per round; **DGW = first game only is a manual
  admin score override** via `services.override_cup_match`). **Seeded from H2H standings
  through GW27.** Cup: seeds 1&2 bye → R1 3v6/4v5 → R2 **re-seeds** (seed 1 vs lowest
  remaining seed, 2 vs highest) → R3 Final **+ 3rd-place playoff** (SF losers). Pup:
  bottom-4 play-in R1, the two Cup R1 losers join at R2, R3 final. **Tiebreakers:** total
  goals → assists → clean sheets (team totals over the match, from `gameweek_points.
  team_*`) → better seed (`rules.match_winner`). Admin at `GET /admin/cups` (generate,
  score round, per-match override); public read-only at `GET /cups`.
  `services.generate_cups`/`score_cup_round`. Cup/Pup winnings need the final round
  scored (`get_payouts` sets `cups_pending` otherwise), with a **historical fallback**:
  past seasons with no live bracket resolve cup/pup winners from imported
  `season_history`.
- **Pupmunity Shield:** prior season's Cup winner vs Pup winner in GW1; $25 each → $50
  to the winner. `services.set_shield`/`score_shield`/`get_shield`
  (`prior_season_shield_participants` suggests the two by entry id); admin on `/admin/cups`.
- **Payouts:** Config-driven (`rules.PAYOUT_STRUCTURE`), auto from final standings +
  cup results. Base pot = entry_fee × managers (25/26 $125; rises 26/27 $150, 27/28
  $175, 28/29 $200). Pct of pot: League 1st 40%, 2nd 15%, 3rd 5%; Cup 1st 25%, 2nd 10%,
  3rd 5%. **Pup Cup winner = $25 × Pup entrants pool** (default 6 → $150). Pupmunity
  Shield $50 to winner. Last-place fine ($125) + fines added to League 1st. **Weekly
  pool** (auto): the highest `gameweek_points` total each GW wins $10 (split on ties,
  `services.weekly_winnings`); every manager pays a $42.18 annual entry. **Side pots**
  (team-sale clause, ad-hoc) are an admin ledger (`side_payouts`, on `/admin/standings`).
  Both fold into overall winnings via `compute_payouts`' `extra`. Each manager's `net`
  = payout − buy-in (overall winnings). `services.get_payouts`.
  The four position slots (league 1/2/3, last place) come from the **adjusted** standings,
  so a commissioner deduction moves the money — and because 1st also collects the fines
  pool, a change at the top moves more than its 40%. An exact (total, points_for) tie breaks
  alphabetically by display name, the same rule as the standings page and the draft order;
  a manager with no `Standing` row counts toward the pot and owes the buy-in but can neither
  win a slot nor be fined for last. Cup seeding is deliberately *not* on this path —
  `seed_managers` recomputes H2H from `Match` rows through GW27, so a later deduction can't
  retroactively reseed a bracket.
- **Scoreboard:** `GET /scoreboard` (`services.get_scoreboard`) — current-GW H2H live
  scores; 'Scores' nav link in-season.
- **Waiver window:** `services.waiver_window` surfaces waivers-vs-free-agency on
  `/transactions` (informational; add/drops happen in FPL).
- **Injury / International lists:** IL (same-position replacement, 4-GW min stay) and
  the **international list** (AFCON/Asia Cup: same-position replacement, no min stay; one
  replacement per absence; re-add when the nation is eliminated) both preserve keeper
  eligibility — their gameweeks are folded into the "covered" set in
  `_derive_keeper_status` so an absence never counts as a drop. Manager self-service on
  My Team (`/il/*`, `/intl/*`). **Goalkeepers are out of scope on both lists once goalie
  teams are on** (`_refuse_goalkeeper_list_move`): the same-position rule is
  unsatisfiable, since the only keepers you own are your own club's — and an injured club
  keeper needs no action anyway, because his backup plays and scores in the same slot.
- **Draft (live ops):** boards auto-refresh on all devices (7s poll on `_board.html` /
  `_discovery_board.html`); a unique slot constraint + `record_pick` guard block
  concurrent overwrites. Managers keep an **autodraft queue** (`draft_queue`, `+Q` in
  search); admin "approve queued pick" fills the on-the-clock slot from the absent
  manager's queue (main + discovery).
- **Transactions:** weekly add/drops at `GET /transactions`, derived from consecutive
  roster snapshots (`services.get_transactions`) since the FPL waiver feed isn't public.

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
coverage, unseeded keepers, malformed pick trades).

## League phase lifecycle & multi-season (the season state machine)

**Multi-season = one league row per season.** Each FPL Draft season is a new
`fpl_league_id` → a new `leagues` row; every child table FKs `league_id`, so seasons
are physically separate (no `season_year` columns, no clobbering). The **current**
season is the row with `leagues.is_current=True` (`services.current_league`, falling
back to the `FPL_DRAFT_LEAGUE_ID` env). Past seasons are older rows, browsable
read-only at `/seasons` + `/season/{fpl_league_id}` (reusing the league-arg read
services). The login session stores the stable FPL **entry_id**, so identity resolves
to whichever season's manager row is current.

**Phase** (`leagues.phase`): macro enum `offseason | draft | preseason | in_season`
(+ stored `discovery_open`, `discovery_done`, `phase_manual`). In-season sub-states
(post-trade-deadline Feb 1, cups GW28, discovery window) are **derived from the
date/GW**, never stored, so they can't drift. `rules.phase_features(...)` is the pure
phase→feature-flag map; `services.phase_context(db, league)` computes it (the single
source the routes + nav consult via `ui._feature_allowed` and the `_phase` template
context processor). Manual locks (`writes_locked`/`keepers_locked`) remain hard
overrides; admin always bypasses.

**Transitions:** time/GW ones auto-advance during `/admin/sync`
(`services.advance_phase_if_due` → pure `rules.next_phase`: GW38→offseason,
GW1→in_season, Oct 1→discovery) unless `phase_manual` pins it. Admin-confirmed ones
(on `/admin/health`): **Start draft** (`enter_draft_phase` — locks keepers),
**Close discovery** (`close_discovery`), manual set/pin. **Season rollover** at
`/admin/season` → `services.advance_season`: syncs the new FPL league id (sync is
parameterized by league id), carries identity (display_name + password_hash by
entry_id) + keeper seeds (years−1) forward, snapshots the player pool, flips
`is_current`, sets preseason.

**FPL league ids are NOT stable across seasons — a finished season must be frozen.**
FPL recycles the numeric league id, so once our season ends `/league/{id}/details`
can start returning a *completely different league*. This actually happened (Aug
2026: our 25/26 id `1754` became a Danish league, and three nightly syncs merged 12
foreign managers + 228 foreign fixtures into our season and overwrote our league
name / season year / GW calendar). Two guards, both in the sync path:
1. **`leagues.sync_locked`** — set automatically when the season ends
   (`advance_phase_if_due` on →offseason, and on the outgoing row in
   `advance_season`), toggleable at `/admin/health`. Every sync sub-task resolves
   its league through `sync._resolve_league`, which skips a frozen row before any
   HTTP call. `/admin/sync` also skips `flag_ineligible`/`reconcile_absences`.
2. **`rules.verify_league_feed`** (pure) — before writing to a league row that
   already has managers, the feed must name the same `season_year` and still
   contain ≥50% (`MIN_ENTRY_OVERLAP`) of the FPL `entry_id`s we know. Otherwise
   sync writes nothing and raises `sync.LeagueIdentityError` → `/admin/sync` 409
   (red cron) + a failed check on `/admin/health`. A league row with no managers
   yet is a fresh season and accepts anything.
Starting a new season therefore means a **rollover to the new league id**, never
re-pointing at the old one. `scripts/cleanup_recycled_league.py` is the one-off
repair for the 2026 incident (kept as the worked example).

**FPL also reassigns PLAYER element ids every season — same trap, second table.**
`players` is a single global table keyed on `fpl_id`, but element ids are per-season
(25/26 id 5 = Gabriel, 26/27 id 5 = J.Timber). `sync_players` upserts on `fpl_id`,
so pulling a new season's bootstrap rewrites each existing row's identity *in place*.
`rosters` reference `players.id`, so nothing looks broken structurally — every
historical squad just silently shows the wrong names/clubs. This is exactly what
happened alongside the league-id incident (570/841 rows rewritten; rosters and
gameweek_points were untouched). Guard: `sync_players` no-ops when **every** league
row is `sync_locked`, so a finished pool is never refreshed with a live season's ids.
Repair: `scripts/restore_player_identity.py` (restores from a snapshot by
`players.id`; season stats added after the snapshot are cleared, since showing
another player's numbers is worse than showing none).

**Season-scoped player identity (built).** Two pieces close this:
- **`players.code`** — FPL's *permanent* player id (Raya: per-season `id`=1, `code`=154561).
  `sync_players` matches on `code` first, so `players.id` permanently means one human and
  the 12 FK columns pointing at it (rosters, keeper seeds/selections, trades, IL, draft
  picks, v2 ledger) stay correct across seasons. `fpl_id` is now just "this season's
  element id": nullable, with a **partial** unique index (`WHERE fpl_id IS NOT NULL`) so
  departed players can release their slot. Sync runs in phases — decide who owns each
  incoming id, free every id whose holder isn't its new owner, then assign — because a
  straight swap otherwise violates that index mid-transaction. A not-yet-coded row is
  adopted only when **name AND position** both match; position alone can't tell Gabriel
  from J.Timber, who are both DEF.
- **`player_season`** (`league_id`, `player_id`, `fpl_id` + identity/stats) — per-season
  snapshot, refreshed by `sync_players` on every run while a league is current+unfrozen,
  then frozen in place. Read paths resolve through it via `services.season_identity`, so
  there is **one code path** for current and historical seasons (no `if sync_locked`
  branching). It carries both ids so it resolves either direction — UUID→identity for
  roster joins, and season `fpl_id`→player for the ids embedded in
  `gameweek_points.player_points` and v2 lineups.

Gotchas worth remembering: a `PlayerSeason` row's `.id` is the snapshot's own PK, **not** a
`players.id` — comparing it against a keeper/roster FK silently yields False. And
`snapshot_player_pool` must run *after* the first post-rollover sync, since `sync_players`
is gated on a league being current+unfrozen (`advance_season` no longer captures it).
Backfills: `scripts/backfill_player_code.py` (conservative; hard-stops rather than leave a
rostered/keeper player uncoded) and `scripts/capture_player_season.py`. 25/26 rich stats are
permanently NULL — they were cleared after the incident and can't be recovered.

**Point projections (external, not synced).** A draft is prepared while `players` is all
zeros, so the Players tab also shows an outside analyst's projected totals beside last
season's actuals. `player_projection` is keyed on `(season_year, player_id)` — **not
`league_id`** (projections are needed before `advance_season` creates that season's league
row) and **not `fpl_id`** (recycled ids would re-point every row at a different human).
Points-per-million is derived on read, never stored. Read path:
`services.projection_season_year` (the newest imported year — reading it off the data, not
`league.season_year + 1`, which blanks the whole tab the moment `is_current` flips) →
`projection_index` → `proj_*` keys on `player_portal`. **`proj_price` is already £m, unlike
`players.price` (tenths) — don't divide it.** Import via `scripts/import_projections.py`
(dry-run default, `--apply`, stdlib `zipfile` xlsx parse, no openpyxl); it reads cells by
column LETTER because Excel omits empty cells, and lowercases before transliterating
because NFKD has no decomposition for ø/ı. **Owner-only** — deliberately absent from the
draft board search all managers see, with a test enforcing it.

**Sync cadence** is fixture-aligned + code-gated: `/admin/sync` runs
`services.sync_plan` (pure `rules.decide_sync`) → `full | live | skip` from
{a full sync today?, a PL fixture live now?, a GW deadline today?}. The cron
(`.github/workflows/cron.yml`) fires often within a window; the endpoint no-ops when
nothing's live. `?force=1` forces a full sync.

**Ineligible players:** a non-DEF added to FPL after the draft (i.e. not in the
season's `player_pool_snapshot`, captured at rollover) is flagged in
`player_ineligibility` (`services.flag_ineligible`, run after each full sync) — never
mutating the global `Player` row — surfaced on the homepage and excluded from
draft/keeper search.

## Auth & authorization (per-manager identity)

A **hard gate** (`GateMiddleware` in `main.py`) requires a logged-in identity
before any HTML page renders — first visit redirects to `/who` (a button per
manager + Admin). Exempt: the `/v1` JSON API, any request with a valid
`X-Auth-Token` (cron `/admin/sync` + programmatic `/admin/*`), `/static`, and the
login surface. HTMX logged-out requests get an `HX-Redirect` (full nav, not a swap).

- **Per-manager passwords** live on `managers.password_hash` (stdlib PBKDF2 via
  `auth.hash_password`/`verify_password` — no extra deps). NULL = first-time set
  flow at `/login`→`/set-password`. Admin clears it (reset) at
  `POST /admin/managers/reset-password` (button on `/admin/health`); the manager
  then sets a new one. Session keys: `session["manager_id"]` (the `fpl_manager_id`)
  and `session["manager_name"]`; admin keeps `session["admin"]`.
- **Scoped writes** via `auth.can_act_as(request, *fpl_ids)` (admin bypasses):
  keepers only for your own team; trades require you as a party; draft picks only
  when you're on the clock; **draft order is admin-only**. Failures → 403 via
  `_forbidden`. Forms auto-fill/lock to the logged-in manager; identity is injected
  into every template by the `_identity` Jinja context processor in `templating.py`
  (so the nav shows who you are without each route passing it).
- The commissioner can **remove** a standings adjustment
  (`services.delete_standing_adjustment`, `POST /admin/standings/delete`).
- The **editing lock** (`leagues.writes_locked`, toggled at `/admin/health`) still
  layers on top: when locked, only admin can write picks/trades.
- **Hardening** (see `SECURITY.md`): secure/`same_site=lax` cookies (HTTPS-only via
  `SESSION_HTTPS_ONLY`), a `SECRET_KEY` start-up guard in prod, `hmac.compare_digest`
  for the admin password/token, security headers (`SecurityHeadersMiddleware`),
  `text/plain` error responses (`_err`), and bounded numeric input (`_safe_int`).
  Env vars + the secret-rotation runbook live in `SECURITY.md`.

**My Team pages:** `/my-team` (your current squad with rich FPL stats — form, PPG,
season pts, G/A/CS/bonus/min, ICT, ownership, availability, keeper badges, a
recent-points sparkline) and `/my-team/upcoming` (next 3 H2H opponents with both
squads and each player's real-life PL fixture + difficulty). Admin can view any
manager's via `?fpl=`. Rich player stats come from the classic FPL bootstrap
(`sync_players`); PL fixtures from the classic fixtures feed (`sync_fixtures` →
`fixtures` table); `services.current_gameweek` derives the GW from stored dates
(no live FPL call).

## Working style

- Work in scoped chunks, one feature area per session — never the whole app.
- Propose a plan before writing code on anything non-trivial.
- Keep changes reviewable; the human commits between pieces.
- When you settle a convention or finalize a command, update this file.
