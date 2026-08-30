# FPL Draft Keeper League

A public website for a Fantasy Premier League **Draft Keeper** league. It syncs
data from the official FPL Draft API and layers on custom league rules (keepers,
waivers, trades, drafts, injury list, cups, anti-tanking). The system runs
year-round and must be able to reconstruct league state for any gameweek.

Full feature spec and database schema live in `docs/requirements.md` — read it
before any non-trivial work. Known-but-unscheduled bugs and features live in
`docs/BACKLOG.md`; add to it rather than losing an item between sessions.

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
  `TEST_DATABASE_URL` (local Postgres recipe in `tests/conftest.py`). **Without it the
  run now stops before collection** with `ERROR: TEST_DATABASE_URL is not set` (exit 4)
  — a silent `585 skipped, exit 0` used to read as green and has caused false-green
  regressions here. Set `ALLOW_DB_SKIP=1` to opt out on purpose (pure-rules only).
  Both that check and the alembic-on-PATH check live in `pytest_configure`, **not** in
  the `test_engine` fixture: a session-fixture failure is cached and re-raised per
  dependent test, which is what turned one unactivated venv into 585 identical errors.
- **The three committing test files are refused by default.** `test_audit.py` /
  `test_demo.py` / `test_sync_freeze.py` build sessions from `DATABASE_URL` (which
  `db.py` resolves via `load_dotenv()`, i.e. **production Neon** on a dev machine) and
  commit. `pytest_collection_modifyitems` skips them unless `ALLOW_COMMITTING_DB_TESTS=1`,
  naming the host it refused. **A plain `pytest` is now safe** — the old convention of
  hand-typing three `--ignore` flags is obsolete, and shouldn't be revived: it failed on
  2026-08-24 because zsh doesn't word-split an unquoted `$IG`, and they hit prod twice.
  A full run is `1060 passed, 18 skipped`; those 18 are the refusal, not lost coverage.
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

**`fpl_manager_id` (FPL's entry_id) is NOT stable across seasons — proven false on
2026-08-18.** Much of this codebase bridges league rows on it (`advance_season`'s
identity + keeper carries, `_goalie_team_history`, `_in_progress_bridge`,
`_manager_bridge`, the login session). At the 26/27 rollover FPL issued all ten
managers brand-new entry ids (25/26: 5520-268927; 26/27: a contiguous 58528-58537
block) with **zero** overlap, so every one of those bridges silently matched nothing:
no display names, no password hashes (all logins broken) and no keeper seeds carried.
Nothing warned, because each bridge `continue`s on a miss. `display_name` is the only
identity the league itself owns; prefer it for cross-row work, and treat an
entry-id bridge as best-effort. See the `P0` backlog entry.
**The rollover no longer guesses.** `advance_season` takes an explicit
`pairing={new_manager_id: old_manager_id}` and RAISES on an incomplete one (naming
both sides; `force=True` is the "someone joined or left" hatch, audited as `[FORCED]`).
`/admin/season/advance` syncs the new league then stops at `/admin/season/mapping`,
where the commissioner confirms who is who — `is_current` doesn't move until they do,
so abandoning a rollover halfway is safe. `services.suggest_manager_pairing` pre-fills
a guess from team names (6/10 on real data, **0 wrong**, pinned by a test); it is never
applied on its own, same rule as the discovery matcher.

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
  Two rules live in `record_pick`: a manager gets ONE club
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
- **Squad quotas (enforced from 2026-08-30):** `record_pick` refuses a pick that would
  break FPL's shape — `rules.SQUAD_POSITION_LIMITS` (2 GKP / 5 DEF / 5 MID / 3 FWD), or
  `OUTFIELD_POSITION_LIMITS` under goalie-team mode (13 outfielders + a club).
  **This reversed a long-standing deliberate decision** (a sixth defender used to be
  legal and a test pinned it). It changed nothing live: FPL Draft refuses an illegal
  pick upstream, and all ten 26/27 squads were exactly 2/5/5/3 when checked. It matters
  because the auto-sub projection's formation maths assumes a legal squad.
  `services._squad_quota_reason` counts this season's main-draft picks plus keepers,
  excluding the slot being filled right now (or an admin correction would be refused by
  its own pick) and excluding discovery picks (not in the PL yet). Position resolves via
  `PlayerSeason` **falling back to the global `Player` row** — the fallback is
  load-bearing, not tidiness: without it a season with no snapshot counts zero of
  everything and enforces nothing, which is how this shipped the first time, passing the
  very test meant to prove it worked. Unlike scoring, a draft is always the current
  season, so the global row is the same human. Deliberately NOT folded into
  `_unavailable_reason` — that doubles as search's taken-oracle, and a defender isn't
  "taken" because YOUR back line is full.
- **Keepers:** 15-man rosters; up to 5 keepers/season (6 if a discovery keeper
  applies). Max 4 years of keeper eligibility for a draft- or trade-acquired
  player (`rules.KEEPER_FRESH_DRAFT`); a waiver/FA-acquired player — including one
  dropped and re-acquired, which always relabels "waiver" regardless of original
  acquisition — gets only 3 (`rules.KEEPER_FRESH_WAIVER`) — track the clock per
  player. Waiver keepers capped at 2 (from 2025 on). Traded players KEEP keeper
  history.
  **THE CLOCK BELONGS TO THE PLAYER, MID-SEASON.** Drop a player and whoever claims him
  off waivers inherits his remaining years (capped at `KEEPER_FRESH_WAIVER`, still
  labelled `waiver` so it still eats one of the two waiver slots) — an EXHAUSTED clock
  means the claimant cannot keep him either. `_status_for` resolves the clock from three
  sources in strict order: this manager's `KeeperSeed`, then a trade sender, then the
  previous holder this season (recursed with `upto` = their last GW, mirroring the trade
  path). Every step tests `is None`, never truthiness — a deliberate seed of 0 is falsy
  and must not fall through. At the ROLLOVER anyone not kept resets: `advance_season`
  iterates `KeeperSelection`, so a non-kept player's clock simply ends, which is the
  rule, not an oversight — there is deliberately no carryover table.
  **"On the GW1 roster" is only a PROXY for "drafted"** and it over-grants to a preseason
  free-agent signing, who lands on GW1 too and collected a draft-length clock.
  `_drafted_this_season` consults real `DraftPick` rows — but returns a `trusted` set
  alongside, and the distinction applies ONLY to managers with at least one recorded main
  pick that season. Seasons before 2026 predate the live draft board, and reading their
  silence as "undrafted" would regress every historical keeper to `waiver`. A pick
  carrying only free text (three real 26/27 ones do) is resolved against that manager's
  own GW1 roster; an UNRESOLVED one drops its manager from `trusted`, because the pick we
  couldn't read might be the very player being asked about.
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
  submission for that season. Acquisition labels are `rules.KEEPER_ACQUISITIONS` =
  `draft | waiver | trade | discovery`; only `waiver` shortens the clock (3 vs 4) and
  only `waiver` counts against the ≤2 cap. See the discovery-draft bullet for how the
  `discovery` label is asserted — it can't be derived from rosters or trades.
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
  prompt; admin can still act for anyone. See "Injury / International lists" below for
  the ownership overlay, the season-end resolution, and the must-return alert.
- **Anti-tanking:** Flag a manager when >=3 of their ROSTERED players (the whole
  15-man squad, not just the XI) record 0 minutes in each of >=3 CONSECUTIVE
  gameweeks. Across-gameweek rule, players may differ week to week. Thresholds
  are constants in `rules.py`. (Whole-squad scope was chosen deliberately even
  though it flags most of the league — see [[anti-tanking-whole-squad-choice]].)
  Show infractions on homepage and admin panel.
  **The count is only the zeros the manager is answerable for**, or the rule fires on
  the calendar rather than on neglect. `_tanking_counts_by_manager` strips three
  structural causes before calling `rules.zero_minute_count`: a club with **no fixture**
  that GW, a player covered by the **IL or international list** (`_absence_cover`, the
  same set the keeper-drop derivation uses — the two must agree on "covered"), and the
  spare **goalkeeper**. The GK allowance (`ANTI_TANKING_FREE_ZERO_GKS`) is
  **all-or-nothing**: a squad must carry two keepers and only one can start, so one at 0
  minutes is roster construction and is forgiven — but every club fields a keeper every
  week, so TWO at zero means neither is starting anywhere and both count. Excuses apply
  BEFORE the allowance, so a blank-club backup is excused as a blank and the allowance
  still shields the other. A GW with **no fixture rows at all** excuses nobody — that's
  missing data, not twenty blank clubs, and excusing there would switch the rule off for
  every historical season. Position/club come from `PlayerSeason`, never the global pool
  or the lineup slot (recycled element ids).
  **A flag carries no money.** Fines are a separate MANUAL commissioner ledger (`fines`
  table, `services.add_fine`, `/admin/standings`); nothing converts a flag into a fine or
  a points deduction. The only automatic fine is last place (`PAYOUT_STRUCTURE`).
  Dismissals (`TankingFlagClear`) are honoured by `_open_windows`, which `_manager_status`
  and `flagged_actions` both read; only `get_flags` still returns cleared windows, because
  admin needs to see and restore them.
- **Trades:** Allowed only end of GW38 -> Jan 31. Player-for-player,
  pick-for-player, or pick-for-pick. Conditions free-text initially. Trades
  update keeper clocks and the draft board.
  **A pick trade can carry a CONDITION**, as a CLAUSE on the `Trade` row over TERMS in
  `trade_condition_terms`. Terms combine under `condition_logic` (`all`/`any`) and
  `condition_effect` says what being met does: `escalate_round` (the pick moves either
  way but as `pick_round_if_met`) or `transfer_if_met` (the pick moves ONLY if met).
  Both fold into `pick_ownership` without leaving a value to undo — the first changes
  the **KEY** (`_effective_pick_round`), the second changes **whether the fold writes**
  (`_condition_applies`). `condition_logic IS NULL` is the discriminator for "ordinary
  pick trade", checked by every read path.
  **One clause per Trade row, deliberately** — a row moves one pick, so a deal with
  three conditional clauses is three rows, which is what a multi-pick deal already was.
  That is why there is no clause/group table.
  Four evaluable metrics (`rules.CONDITION_METRICS`) — `total_points` names a PLAYER,
  `league_finish`/`cup_win`/`pup_cup_win` name a MANAGER, and the two cup metrics are
  boolean facts, so comparison/threshold are stored NULL rather than defaulted. The
  subject is `manager_name`, **a person name and never a `managers.id` FK** — one
  manager row exists per season, and a condition entered today resolves against a season
  whose rows don't exist yet (the `FuturePick.owner` precedent).
  **`metric='manual'` is the escape valve, and it lives on the TERM, not the clause.**
  A real 2026 deal (KT<->KS) needed a four-way OR whose branches included "cunha less
  than 3 red cards" (no such column) and "pick 12 scoring 225" (a subject that isn't a
  player). A manual term stores the clause verbatim in `note` and resolves only via
  `manual_state`, set by the commissioner on `/admin/corrections`
  (`services.set_condition_term_state`, manual terms only — an evaluable metric is
  answered by the data). Term-level is what makes that OR work: the three knowable
  branches still decide it. `rules.combine_condition_states` short-circuits on the
  decisive answer BEFORE considering pending terms, so `any` goes met on a known branch
  with an unknown outstanding and `all` goes not_met on one failure; an EMPTY term list
  is `pending`, never met, because `all([])` is True in Python.
  Resolved fresh on every read (`services._resolve_condition` -> `_resolve_term`) and
  **never written back to `pick_round`**. An evaluable term resolves ONLY once its
  season's league row is `sync_locked`, uniformly for all four metrics — until then it
  is `pending` and the BASE round stands, even when the live number already clears the
  threshold; `pending` is deliberately not `not_met`, and an undecided bracket stays
  pending too. `season_year` is per TERM, not per clause. `league_finish` reads
  `get_standings` (adjusted + alphabetical tie-break), never `Standing.rank`. Cup
  winners resolve through `services._resolve_cup_winner_name` — live bracket, then the
  `season_history` fallback — which is the helper to extend for a fifth metric.
  Entry is **commissioner-only** (route + template gate). The entry and correction forms
  share `templates/_condition_form.html` (the metric/comparison lists had already
  drifted between two copies) and post **parallel arrays**, one entry per term, zipped
  back by `ui._condition_terms_from_form`; a repeater row with a blank metric is dropped
  rather than validated. Display keys (`conditional`, `condition_status`,
  `condition_note`, `manual_terms`) are attached only to conditional rows, so an
  ordinary pick dict is unchanged — two tests assert that by exact dict equality.
  `Trade.conditions` (free text) is finally read: it holds the deal as written, so the
  wording survives a clause only partly expressible in terms.
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
  **A discovery acquisition is draft-length (4 years), not waiver-length**, and carries
  its own `"discovery"` label in `rules.KEEPER_ACQUISITIONS`. It has to be asserted
  explicitly, because `_derive_keeper_status` reads only rosters and trades and a
  September pick is neither — he joins in January, so he's on no GW1 roster and has no
  `Trade` row, and falls through to `("waiver", 3)`. Two independent witnesses supply
  it, and they cover each other's blind spot: `discovery_linked` (from `DraftPick`,
  public, so it works for **viewer-less** callers) and `discovery_flagged` (every
  `is_discovery` selection — from `KeeperSelection`, so it stays behind the keeper
  privacy gate and is empty at `submit_keepers` time, which is why that function
  synthesizes the label itself on BOTH the on- and off-roster paths). Don't collapse
  them. `discovery_only` is the narrower off-roster subset and keeps its own job of
  widening the candidate set. The synthesized label is gated on **not dropped** —
  `acquisition=` short-circuits `keeper_status`'s dropped branch, so ungated it would
  launder a genuine drop-and-re-acquire clean forever. A seed still beats all of it.
  **`"discovery"` the LABEL is not `KeeperSelection.is_discovery` the FLAG**: the flag
  is the bonus 6th slot and is what `validate_keeper_selection` keys the cap exemption
  on. A discovery-acquired player kept in an ordinary slot is labelled `"discovery"`
  and raises no cap. Don't conflate them.
  **Linking a pick to a real player is `services.link_discovery_pick`, admin-only and
  never automatic.** A pick is recorded as free text (`record_discovery_pick`) because
  the player has no `players` row yet; linking is what lets the derivation see it at
  all. `Player.name` is FPL's short `web_name` while managers type full names, so
  name-matching is a coin flip and a wrong link hands someone another manager's keeper
  on a 4-year clock — nothing downstream would flag it. `player_label` is kept as
  entered (the board still prefers it). No migration was needed: the `DraftPick` CHECK
  is team-scoped. Admin form on `/admin/corrections`; `unlink_discovery_pick` undoes a
  mislink. Linking also makes him `taken` in **discovery** search only — the overlay is
  `draft_type`-scoped, so the main draft is untouched.
  **Finding the player is assisted; deciding is not.** `services.match_discovery_picks`
  runs after every full sync (at the end of `sync.run_sync`, `sync.py:1025` — *outside* the
  `sync_locked` guard, since it reads the global pool against picks that may live on an
  older league row, and never from a per-task sync helper, which must stay on the FPL-canonical side)
  and writes `discovery_match_suggestions` rows only. Three tiers — exact / strong
  (token subset, which is how a typed "Nick Woltemade" reaches web_name "Woltemade") /
  close (`difflib` ≥ 0.85, stdlib only). `players.full_name` (FPL's first + second name,
  written by `sync_players`) exists for this; `players.name` is only `web_name`.
  Normalisation is a **token-wise** local copy of `import_projections._norm` — lowercase
  → translit → NFKD, in that order, because ø/ı have no NFKD decomposition; a test
  asserts the *tier*, since a broken table still scrapes past the fuzzy threshold.
  Rejected suggestions are **kept, not deleted** — that (plus `UNIQUE(draft_pick_id,
  player_id)`) is what stops the nightly run re-proposing a dismissal. Admin confirms or
  rejects on `/admin/corrections`; confirm calls `link_discovery_pick` **first**, so a
  refused link leaves the suggestion pending rather than recording a decision that never
  happened.
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
  **The score shown is PROJECTED, not FPL's raw live total.** FPL applies bench
  substitutions only when a gameweek is FINALISED, so its mid-week number shows a
  manager carrying a hole it will later fill — measured 2026-08-30, four of ten managers
  were understated, one by six points. `rules.project_auto_subs` (pure) fills it;
  `services.projected_points_by_manager` resolves the inputs.
  **The substitution rule is deliberately NOT FPL's literal one.** FPL says the incoming
  player must have PLAYED, which is only equivalent at gameweek end. Applied live it
  skips a bench player whose match is tomorrow, promotes the man behind him, then
  reverses — the projection thrashes. The test here is "can this player still score?"
  (0 minutes AND his club finished, or a blank GW ⇒ ruled out), which is stable and
  **converges on FPL's rule exactly** once every match is over. That convergence is the
  verification: **on a finalised gameweek the projection must equal
  `GameweekPoints.total_points`**, pinned by a test and confirmed against production GW1.
  **The goalkeeper rule needs no special case** — with GKP pinned to exactly 1 in
  `XI_POSITION_MAXIMUMS`, swapping the keeper for an outfielder leaves 0 and swapping an
  outfielder for the bench keeper leaves 2. Both illegal. The outfield ceilings are
  unreachable while a squad is FPL-legal (squad limit == XI limit for DEF/MID/FWD), so
  only the keeper ceiling ever binds.
  `players_remaining_by_manager` runs on the **effective** XI, so a projected sub whose
  own match is still to come counts as left to play (flagged `sub: True`) — on the picked
  XI he appeared nowhere, understating exactly the managers this helps.
  **Matchup analysis** (`services.matchup_analysis`) is a deterministic sentence per tie,
  four states, draws called out ("9 to draw, 10 to win"). The contingency does NOT
  explode: what's uncertain is *which* player fills a slot, not how many slots there are,
  so it is one clause naming the cover, never a tree. Cover is keyed **by the at-risk
  player** and answered by re-running the projection with him hypothetically ruled out —
  naming "the next bench player" is wrong nearly every time, since slot 12 is almost
  always the backup keeper and he can only replace the keeper. The arithmetic lives in
  Python so the later pundit layer (Item 19) dresses up facts rather than computing them.
- **Waiver window:** `services.waiver_window` surfaces waivers-vs-free-agency on
  `/transactions` (informational; add/drops happen in FPL).
- **Injury / International lists:** IL (same-position replacement, 4-GW min stay,
  **capped at one per manager**) and the **international list** (AFCON/Asia Cup:
  same-position replacement, no min stay, **UNCAPPED** — a manager may have several
  players away at once, each with their own replacement, since the league can't control
  call-ups; re-add when the nation is eliminated) both preserve keeper eligibility —
  their gameweeks are folded into the "covered" set in `_derive_keeper_status`
  (`_absence_cover`) so an absence never counts as a drop. Manager self-service on My
  Team (`/il/*`, `/intl/*`). **Goalkeepers are out of scope on both lists once goalie
  teams are on** (`_refuse_goalkeeper_list_move`): the same-position rule is
  unsatisfiable, since the only keepers you own are your own club's — and an injured club
  keeper needs no action anyway, because his backup plays and scores in the same slot.
  **Ownership is additive, not a swap** (`docs/DESIGN_IL_OWNERSHIP.md`): a manager holds
  their FPL-synced roster PLUS everyone out on an absence, so a manager with someone on
  the IL genuinely holds 16. `services._absence_held` is the single "who holds him now"
  predicate — deliberately NOT `_absence_cover`, which is status-blind and would keep a
  season-end `'returned'` entry "covered" forever, forking from ownership on the one case
  that matters most (a manager submitting a keeper the site accepts, that
  `effective_keeper_selections` then silently drops). Folded into `_owner_maps`
  **before** the trade fold (folding it after makes a trade of an absent player
  permanently unappliable) and guarded `if pid not in owner` so a mis-entered absence can
  never steal a rostered player. `place_on_il`/`place_on_intl` require the player to
  actually be THIS manager's — `services._validate_absence_eligibility`, which checks
  the current effective roster first and, failing that, falls back to whether the
  manager held him at some point THIS season (the same `presence` dict
  `_derive_keeper_status` shares) with nobody else currently holding him for real —
  the self-service historical path (`services.dropped_players_for_manager` is the My
  Team picker's candidate list, added 2026-08-24 for "drafted him, he got hurt, dropped
  him for a replacement before ever recording it here"). Refuses loudly, never
  silently, if a DIFFERENT manager now holds him. The admin historical backfill
  (`require_roster=False`) skips this entirely — for a genuinely old season with no
  roster history to check at all. **Season-end resolution**: an
  absence still open after GW38 must be resolved back to the roster size — Release the
  absentee (`via="waiver"`, he goes to nobody), or Return him and **name who leaves**
  (`released_player_id` on the absence row, folded as the one subtraction in
  `_owner_maps`, applied only after `_owner_maps`' additive fold). This is
  manager-designated, never derived: an earlier design tried to follow the replacement's
  roster slot forward automatically and was withdrawn, because FPL records no paired
  add/drop, so which arrival replaced which departure is genuinely unknowable from a
  diffed snapshot — don't re-propose it. `submit_keepers` refuses for a manager with an
  unresolved absence past GW38; `advance_season` refuses to roll over while one is open
  (same `force=True` shape as its manager-pairing check). **Must-return alert**: once
  eligible, the absent player logging real minutes for his club while still off the
  roster is a violation — `sync.sync_gameweek_points` already fetches those minutes for
  every player via `/event/{gw}/live` and used to discard an absentee's; they're now
  persisted as `last_played_gw` (`services.record_absentee_minutes`), and
  `services._return_required_entries` (surfaced on `flagged_actions` and
  `/admin/health`) fires once `il_return_eligible_gw` has passed for the IL, immediately
  for the international list. Alert-only — enforcement can't be technical, since the
  action happens in the FPL app; fines are the existing manual ledger
  (`services.add_fine`), never automatic.
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

**Which GAMEWEEK gets synced must match what the site reads.** `sync_rosters` and
`sync_gameweek_points` now resolve it through `services.current_gameweek` (derived from
stored GW dates — the same number `/scoreboard`, `/transactions`, the keeper derivation
and anti-tanking all use), falling back to `sync.get_current_gw` only for a league whose
calendar hasn't synced yet. **They must never resolve it differently from their
readers.** `get_current_gw` filtered FPL's `/pl/event-status` on
`s.get("status") in ("L","F")`, and **that payload has no `status` key** — entries are
`{bonus_added, date, event, leagues_updated, points}` — so it returned its `default=1`
forever. It looked right for exactly as long as the real answer was 1, then GW2 began
and both tasks kept writing GW1 while every page asked for GW2 and found nothing, both
logging `ok=True` because they had synced *a* gameweek successfully. Found 2026-08-30
from "the scoreboard doesn't show players left to play". `get_current_gw` now falls back
to the highest `event` the payload names. Two `data_health` checks assert **the current
GW specifically** has rosters and points — the old "gameweek points populated" asserted
`count > 0` across all GWs and stayed green on GW1's rows throughout.

**Sync cadence** is fixture-aligned + code-gated: `/admin/sync` runs
`services.sync_plan` (pure `rules.decide_sync`) → `full | live | skip` from
{a full sync today?, a PL fixture live now?, a GW deadline today?}. The cron
(`.github/workflows/cron.yml`) fires often within a window; the endpoint no-ops when
nothing's live. `?force=1` forces a full sync.
**Which league gets synced comes from `leagues.is_current`, not the env.**
`sync_all()` used to read `FPL_DRAFT_LEAGUE_ID` directly, so after a rollover it kept
targeting the OUTGOING (now `sync_locked`) league — every sub-task took the frozen-skip
branch, which sets `log.ok = True`, and the cron reported green while syncing nothing
for a day. `sync._current_league_id()` now resolves the current row first and logs a
`resolve_league` SyncLog row naming the league it chose; the env is a bootstrap for a
database with no league rows and nothing else. **Don't reintroduce an env read here** —
flipping `is_current` is meant to be the only step a rollover needs.

**Outbound Discord (`discord_bridge.py`).** The first OUTBOUND network call in the
codebase. Two incoming **webhooks**, each its own env var and each OFF when unset:
`DISCORD_WEBHOOK_URL` (public — announces every new trade once) and
`DISCORD_ALERT_WEBHOOK_URL` (private — `flagged_actions` + failed `data_health` checks).
A webhook needs no bot, no Developer Portal app, no privileged intent and no Manage
Server permission; the URL IS the credential, so it is env-only (`SECURITY.md`).
CLAUDE.md's "no live FPL calls in request handlers" rule is about inbound sync, but its
reasoning applies identically outbound, so: called ONLY from the post-sync hook in
`sync.run_sync` (outside the `plan` branch, so a site trade doesn't wait for tomorrow's
full sync), never from a request handler, never inside a service transaction, and
**never from `record_audit`** — audit is a write-path primitive and hanging HTTP off it
would give every future audited action a network dependency. `post_message` never
raises and `run_outbound` swallows even a bug in our own rendering: a trade is the real
work, the announcement is a side effect.
**Two markers, because "have I already said this?" has two shapes.** `trades.announced_at`
is the announce queue (`WHERE announced_at IS NULL`), stamped **per success** so a
partial failure leaves the rest queued; the migration **back-stamps every existing
trade**, without which the first deploy would dump the league's whole history into the
channel. It must be persisted, not in-process: trades also arrive from the FPL feed and
sync re-runs constantly. Alerts have no row to stamp — `flagged_actions`/`data_health`
are recomputed from scratch every sync and carry no ids — so `discord_alerts` dedupes on
a **hash of the rendered alert**, with `UNIQUE(league_id, fingerprint)`. That choice sets
the cadence and needs no special-casing: identical text never re-posts, while text that
moves ("on the IL 4 GWs" → "5 GWs") is new information and does, so an unresolved item
nags about once a GW rather than once a sync. The dedupe row is written only AFTER a
confirmed send (the `confirm_discovery_suggestion` ordering). Rendering reuses
`get_trades`' existing display shape rather than re-deriving it, and long alert batches
are SPLIT at `MAX_MESSAGE_CHARS` — Discord rejects >2000 outright, so truncating would
lose the tail silently. A `sync_locked` league is skipped (guard lives in
`run_outbound`, not the call site, so it's testable). Both announcers take an injectable
`send`; that seam is where a bot token replaces the webhook when the inbound half needs
threaded replies.

**Inbound Discord (`discord_bridge.py` + `discord_parse.py`).** Reads `#trades` and the
IL channel and stages what it finds for review. **NOTHING is ever applied
automatically** — every parsed announcement becomes a `DiscordIngest` row the
commissioner confirms on `/admin/corrections`, the same rule as
`discovery_match_suggestions` and for the same reason (a wrong player match moves a
keeper clock and nothing downstream would flag it).
**That is permanent, not a cautious v1**, and the real messages are why: an IL post
("`ekitike IL 1-4`") names **no replacement player**, and `place_on_il` requires one, so
the write is structurally incomplete however confident the parse. A trade post is often
written by someone who isn't a party to it, never says whose pick a traded pick
originally was, and uses two pick notations. So the goal is not to remove the human —
it is to make confirming an announcement **one click instead of a form**.
`services.suggest_il_replacement` supplies the missing field, pre-selected in the queue:
players this manager ADDED that GW first (roster-snapshot diff), then the rest of their
squad, narrowed to the injured player's position and excluding him. **The squad tier is
load-bearing, not a nicety** — every real IL post says "1-4", so `start_gw=1` and there
is no prior snapshot to diff; a diff-only version returns nothing for exactly the
messages that motivated the feature while passing a test written at GW2. A suggestion
only, for the same reason `docs/DESIGN_IL_OWNERSHIP.md` refuses to derive the season-end
release.
**`managers.discord_user_id` is the single highest-value piece of setup** — with it the
AUTHOR of an IL post is a known manager at certainty 1.0 and no name matching happens at
all, and it is the only way to resolve a handle like "Sir Hefty Boy". UNIQUE is
**(league_id, discord_user_id)**, never global (one row per manager PER SEASON), and
`advance_season` carries it forward beside `display_name`/`password_hash` or the map
silently empties at the next rollover.
`discord_parse.py` is **PURE and deterministic — no LLM.** The fuzzy half is deciding
which human "Cunha" is, and `_score_match` already does that; once split, finding the
fields is a regex. `🚨 TRADE ALERT` is on every trade post and nothing else, so
classification is exact. Manager resolution is `discord_id` → exact `display` →
**unambiguous initials** ("KT" = "Kevin T"; the league has two K-managers, so that check
is load-bearing) with **no fuzzy tier** — ten candidates means asking is cheap and
guessing is not. **`Pick N` is the overall position and a bare ordinal is the round**
(confirmed, not assumed); `2026 4th 1st` and `6-9 Discoveries` are staged **unresolved**
rather than guessed, because either reading reassigns a different manager's slot. The
assumed pick owner (the giver) is shown as an assumption.
Raw messages are stored BEFORE parsing (`discord_messages`), so a parser bug is fixed by
re-running over rows rather than re-fetching, and an uninterpretable message is still
*visible*. The poll cursor is `MAX(discord_message_id)` ordered **numerically** — they
are strings (snowflakes exceed 2^53), so a lexical max puts "9" above "10" and the
cursor walks backwards. Rejected proposals are KEPT and never revived.
**Two Discord misconfigurations fail SILENTLY** and both look exactly like a quiet
channel: the `MESSAGE_CONTENT` intent gates **REST as well as the gateway** (blank
`content`, HTTP 200), and a missing `READ_MESSAGE_HISTORY` returns an **empty array, not
a 403** — on a private channel that needs an explicit permission overwrite, since
guild-level roles don't reach it. `discord_bridge.probe_channel` distinguishes them and
`data_health` surfaces both. Never retry a 401: 401/403/429 count toward a Cloudflare ban
at 10,000 per 10 minutes, so a bad token disables the sweep.

**Ineligible players:** a non-DEF added to FPL after the draft (i.e. not in the
season's `player_pool_snapshot`, captured at rollover) is flagged in
`player_ineligibility` (`services.flag_ineligible`, run after each full sync) — never
mutating the global `Player` row — surfaced on the homepage and excluded from
draft/keeper search.
**The pool must actually exist or the rule is a no-op.** `flag_ineligible` returns 0 on
an empty snapshot BY DESIGN, so a season with no pool has the rule silently switched
off; `data_health`'s "draft-day player pool captured" asserts the INPUT, since 0
ineligible players is legitimate but 0 POOL never is. `snapshot_player_pool`'s only
caller is the rollover route, so a season rolled over before that function worked stays
empty forever — `scripts/capture_draft_day_pool.py` seeds it from a pre-draft snapshot
(dry-run default; refuses if the snapshot's `(fpl_id -> code)` mapping disagrees with
the DB, i.e. it came from the wrong side of a rollover). 26/27 was seeded this way on
2026-08-24; 25/26 has no pre-draft snapshot and stays an acknowledged gap.
**Enforcement is deferred, so ownership must be VISIBLE.** Add/drops happen in the FPL
app — nothing here can block picking up an ineligible player, and the only hard stop is
`rules.validate_keeper_selection` rejecting the keeper submission months later. So
ownership is surfaced in two places besides the homepage report (which lists players,
not owners): a `flagged_actions` entry ("Ineligible player", manager + name) and an
`ineligible` pill plus a squad-level banner on My Team (`_rich_player_rows` sets the
key, so `get_my_team` and `get_my_team_in_progress` both get it). Both key on the
SEASON's element id via `PlayerSeason`, never the global `Player.fpl_id`.

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
