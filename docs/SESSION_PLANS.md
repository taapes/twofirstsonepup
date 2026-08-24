# Session plans — backlog execution

One section per backlog item, in execution order. Each section has a short **plan**
(what/why, for the human), a **recommended model**, and a **session prompt** to paste
verbatim into a fresh Claude Code session running that model.

Ground rules for every session:

- Work ONE item per session. Do not pick up adjacent backlog items you notice.
- Read `CLAUDE.md` in full before touching code; it overrides instincts.
- The relevant `docs/BACKLOG.md` entry is the investigation record — read it before
  the code it names. Line numbers cited there and here may have drifted; search by
  symbol name.
- Do not commit. Leave the working tree for human review, and end with a summary of
  what changed, what was tested, and anything that surprised you.
- After the work passes, update the item's entry in `docs/BACKLOG.md` (status → done,
  or delete the entry) as part of the same change.
- Acceptance for anything touching `services.py`/`rules.py`/`models.py`: the FULL
  DB-backed regression, green with **0 skipped**. The command (Postgres container
  once, then the run):

  ```sh
  docker run -d --name fpl-test-pg -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=fpltest -p 55432:5432 postgres:16   # once; `docker start fpl-test-pg` thereafter

  PATH="$PWD/.venv/bin:$PATH" \
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/fpltest \
  PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m pytest -q
  ```

  A run reporting `310 skipped` means `TEST_DATABASE_URL` never reached pytest —
  that is a FAIL, not a pass. Do not read the exit code through a pipe.

---

## Item 1 — In-progress squad view: `/my-team` and `/teams` during draft + preseason

**Priority:** do first — every manager's My Team page is blank right now (preseason,
no gameweeks on the 26/27 row until FPL's first sync).
**Recommended model:** Sonnet 5.
**Backlog entry:** "Show the in-progress squad (keepers + picks so far) once the
draft starts — and after it".

**Plan.** Build the two new sibling read functions the 2026-08-17 design specifies
(`get_teams_in_progress`, `get_my_team_in_progress`), matching the output shapes of
`get_keepers` / `get_my_team` exactly so no template changes are needed. Routes for
`/teams`, `/team/{fpl}`, `/my-team` switch to the new functions when
`league.phase in ("draft", "preseason")`; the public `/v1/.../keepers` API and
`/my-team/upcoming` are untouched. One design correction to the backlog entry, found
2026-08-18: the design predates the rollover — keeper selections and the 2026
`DraftPick` rows live on the **old 25/26 league row** with old-row `manager_id`s,
while the current league is the 26/27 row. The new functions must therefore resolve
draft data by `season_year` (not `league_id`) and bridge managers by
`fpl_manager_id`. Done that way, the functions keep working unchanged after the
later season-alignment migration moves those rows.

**Session prompt:**

```text
Read CLAUDE.md in full, then the docs/BACKLOG.md entry titled "Show the in-progress
squad (keepers + picks so far) once the draft starts — and after it". You are
implementing exactly that design, plus one correction described below. Work only on
this feature.

CONTEXT / CURRENT STATE (2026-08-18): the 2026 main draft is complete and the season
rollover has run. The current league (`services.current_league`) is the NEW 26/27 row
(phase 'preseason', season_year 2026). The 2026 draft's DraftPick rows and
KeeperSelection rows live on the OLD 25/26 league row, with manager_id pointing at
that row's Manager rows. FPL has not opened the season, so the 26/27 row has no
gameweeks and no rosters — which is why /my-team and /teams render blank today.

READ BEFORE WRITING, in services.py: _squad_players (~line 915), get_my_team (~960),
_derive_keeper_status (~2984), get_keepers (~3411), effective_keeper_selections
(~4396), _effective_roster_pids (~4429). Also api.py lines 54-60 (the public
/v1/leagues/{key}/keepers route) and ui.py routes at ~323 (/teams), ~333
(/team/{fpl_manager_id}), ~433 (/my-team), ~581 (/my-team/upcoming). Also the
DraftPick model in models.py (~527): a pick is exactly one of player_id (a player),
player_label (free-text discovery), team_id (a goalie-team club).

BUILD:

1. A small year helper (private to services.py is fine): the draft year for the
   in-progress view is league.season_year + 1 while phase == 'draft' (the draft runs
   on the OUTGOING row pre-rollover), and league.season_year while phase ==
   'preseason' (post-rollover, the new row's year equals the draft year). Today that
   yields 2026 either way for the respective rows.

2. services.get_teams_in_progress(db, league) -> same output shape as
   get_keepers(...). Per manager of the CURRENT league: the union of
   (a) that manager's effective keeper selections for the draft year, and
   (b) that manager's main-draft DraftPick rows for the draft year.
   Cross-row resolution — this is the correction to the backlog design:
   - Selections: find the league row that actually holds KeeperSelection rows for
     the draft year (query KeeperSelection.league_id distinct on season_year ==
     draft year; fall back to the current league if none) and call
     effective_keeper_selections(db, that_row, draft_year) against it.
   - Picks: query DraftPick by (season_year == draft year, draft_type == 'main')
     with NO league_id filter — the same precedent _goalie_team_history already uses.
   - Bridge managers via Manager.fpl_manager_id (the stable entry id): map the old
     row's manager_id -> fpl_manager_id -> the current row's Manager, and render
     under the current row's Manager.display. Drop nothing silently — if a pick's
     manager has no current-row counterpart, include it under the old row's display
     name rather than losing it.
   Rendering: kept players/clubs keep their real derived acquisition/years/eligible
   facts; drafted players render with acquisition=None, years_remaining=None,
   eligible=None, kept=False (the established "blank keeper facts" convention from
   commit da36736). A team_id (club) pick renders with the club's name from
   pl_teams, matching however get_keepers renders a kept club. Skip player_label
   discovery rows (main draft only here). No dedup pass: record_pick already refuses
   a player who is kept, so the sets are disjoint — but assert nothing that relies
   on it beyond that.

3. services.get_my_team_in_progress(db, league, fpl_manager_id) -> same output shape
   as get_my_team. First extract the "player-id set -> rich per-player rows" portion
   of get_my_team into a shared private helper (stats, availability, keeper badges,
   sparkline handling — whatever of it survives having no gameweek data), then call
   that helper from both get_my_team (behavior unchanged) and the new function with
   the keepers+picks id set from step 2's logic. A freshly drafted player's stats are
   last season's real numbers via the existing stat lookup — do not zero them and do
   not special-case them. Where a stat genuinely needs gameweek rows that don't
   exist (the recent-points trend), return the same empty/None shape get_my_team
   already produces when data is missing.

4. Routes (ui.py): /teams and /team/{fpl_manager_id} call get_teams_in_progress when
   league.phase in ("draft", "preseason"), else get_keepers exactly as today.
   /my-team likewise with get_my_team_in_progress vs get_my_team. /my-team/upcoming
   and api.py's /v1 keepers route: DO NOT TOUCH — the /v1 route is a public contract
   that must not vary with phase.

5. Zero template changes. If a template needs a change, your output shape is wrong —
   fix the shape.

TESTS (pytest, DB-backed, in the existing style — see tests/ for fixtures):
- A manager with N keepers + M picks shows N+M entries, keepers with real facts,
  picks with the blank-facts convention.
- A manager with 0 picks shows only keepers; no placeholder rows.
- Cross-row: selections + picks seeded on an OLD league row (different league_id,
  old-row manager ids) still appear under the corresponding current-row manager.
- A goalie-team (team_id) pick renders with the club name.
- Phase regression guard: with phase == 'in_season', /teams and /my-team behavior is
  byte-identical to before this change.
- Public-API guard: GET /v1/leagues/{key}/keepers output is identical regardless of
  league.phase.

ACCEPTANCE: full suite green with 0 skipped, using the command in the header of
docs/SESSION_PLANS.md. Then update the backlog entry's status. Do not commit; end
with a summary of changes and anything that surprised you.
```

---

## Item 2 — Trades / Transactions / Picks pages: cross-season + season attribution + filters

**Priority:** second — all three pages render blank after the 26/27 rollover.
**Recommended model:** Sonnet 5.
**Backlog entry:** "Trades, transactions, and picks pages are scoped to the current
league row".

**Plan.** Widen the three read functions past the current league row, group by
season, and add client-side filters. Decisions confirmed 2026-08-18: trades show all
seasons (per-season sections, newest first); transactions the same (current season
first); picks stay forward-looking (`pick_season_year >=` current season year across
all rows). **A trade's season is computed on read, not taken from the storing row**:
a season's trade window runs from the prior GW38 through Jan 31 (the spec deadline),
so any post-GW38 offseason trade belongs to the FOLLOWING season — the storage
migration stays with the season-alignment item, but this page must already show the
right grouping. Filters: season + manager dropdowns and a player/club text search on
trades and transactions; a manager dropdown only on picks. Two hazards the executing
session must respect: `Trade.created_at` is shared (migration timestamp) on all
pre-2026-08-11 rows, and per-league manager-name maps silently render blank
from/to cells on cross-row trades.

**Session prompt:**

```text
Read CLAUDE.md in full, then the docs/BACKLOG.md entry titled "Trades, transactions,
and picks pages are scoped to the current league row". You are implementing that fix
with confirmed decisions below. Work only on this feature.

CONTEXT (2026-08-18): the 26/27 rollover ran; services.current_league now returns
the new 26/27 row. All historical Trade / FuturePick / Gameweek+Roster data lives on
the old 25/26 row, so /trades, /transactions, and /picks render blank.

READ BEFORE WRITING: services.py get_trades (~5130), get_transactions (~1769),
get_future_picks (~4881), pick_ownership (~4291), corrections_context (~5496),
player_names (~1506); models.py Trade (~302 — note the created_at comment: `date` is
NULL everywhere and the PK is a random uuid4, so created_at is the ONLY reliable
ordering), FuturePick (~864 — owner/original_owner are free-text PERSON names
matched against Manager.display, which is the stable cross-season identity),
Gameweek (~218); ui.py routes /picks (~593), /trades (~1458), /transactions
(~1469); templates/trades.html, transactions.html, picks.html, base.html.

BUILD:

1. Season attribution helper in services.py, e.g. _trade_season_year(trade):
   - event_gw set (FPL-synced, in-season) -> the STORING row's league.season_year.
   - event_gw NULL (commissioner row) -> bucket created_at:
     season = d.year - 1 if d.month == 1 else d.year.
     (Jan trades belong to the season that started the prior calendar year; a
     May-Dec trade belongs to the season starting that year — which makes a
     post-GW38 offseason trade land in the FOLLOWING season, the confirmed rule.)
   - KNOWN IMPRECISION, document in a comment but do not solve: Trade.created_at was
     backfilled by migration f5a6b7c8d9e0 (2026-08-11) with one shared timestamp, so
     pre-migration commissioner rows all bucket into 2026. Admins can set event_gw
     via edit_trade to re-file any misfiled row. If you observe misfiled rows while
     testing, list them in your final summary; do not hand-edit data.

2. get_trades: drop the league_id filter (query ALL Trade rows). Build the names map
   over ALL managers ({m.id: m.display for m in db.query(Manager)}) — the current
   per-league map is why cross-row trades would render blank from/to cells. Return
   shape becomes grouped: [{"year": int, "trades": [row...]}], years descending;
   within a season order by (event_gw desc, NULLs last, then created_at desc). Keep
   each row's existing keys (id, kind, what, from, to, gw, source, edited). ALSO fix
   the pre-existing bug in the same loop: a goalie-team trade (team_id set,
   pick_round and player_id NULL) currently renders kind="player", what="—" — render
   kind="club" with the pl_teams club name. Ripple: corrections_context calls
   get_trades and feeds /admin/corrections — flatten the grouped result there so the
   admin page's shape is unchanged.

3. get_transactions: leave the existing per-league derivation alone (GW numbers
   repeat 1-38 per season; cross-league diffing by bare GW number would produce
   garbage). Add a wrapper get_all_transactions(db) that iterates league rows
   ordered season_year desc, calls the existing function per row, and returns
   [{"year": int, "weeks": <existing shape>}], skipping seasons with no moves. The
   /transactions route calls the wrapper; waiver_window stays current-league.

4. get_future_picks: collect candidate years from FuturePick and pick-Trade rows
   across ALL league rows; keep only year >= current_league.season_year. Keep
   pick_ownership league-scoped (it is the draft board's single source of truth —
   do not change it). Call it once per (league row, year), iterating league rows
   ASCENDING by season_year and letting newer rows overwrite older entries for the
   same (round, original_owner) key — the same future year can legitimately have
   rows on multiple league rows, and person names are the stable key. Output shape
   unchanged.

5. Templates + filters (client-side only; no new endpoints — all data is already on
   the page):
   - trades.html: per-season <details class="card"> sections using the exact
     pattern the trade-notes block in the SAME file already uses (~lines 29-44);
     newest season open by default, older collapsed.
   - transactions.html: per-season sections, current first; keep the existing GW
     grouping inside each.
   - Add one small vanilla-JS filter helper to base.html's existing inline script
     block (~30 lines, no dependencies): filterable rows/sections carry data-season
     and data-managers (space-joined person names); a text search matches the row's
     visible text. Controls: <select> season + <select> manager + <input
     type="search"> on trades and transactions; <select> manager ONLY on picks
     (matching a row when owner OR original_owner matches). Options are rendered
     server-side from the data on the page. When a filter hides every row in a
     season section, hide that section's header too. Reuse existing CSS (.card,
     input/select styles); add a .hidden utility class if none exists.

6. Tests (DB-backed, existing style):
   - get_trades: trades seeded on two league rows -> correct season groups; an
     August commissioner trade (no event_gw) lands in the FOLLOWING season's group;
     a synced trade with event_gw lands in its storing row's season; from/to names
     resolve for both rows' managers (no None); a team_id trade renders the club
     name with kind="club".
   - get_all_transactions: snapshots on two league rows -> season-desc sections, no
     cross-season GW collision.
   - get_future_picks: same year on two league rows -> newer row's ownership wins;
     a year below the current season_year is excluded.
   - corrections_context still yields a flat trade list.

7. docs/BACKLOG.md: mark this entry done, and add a note to the "Post-GW38 activity
   should belong to the following season" entry that the display-attribution rule
   (post-GW38 -> following season, Jan 31 boundary) is now live on read and the
   eventual storage migration must match it.

ACCEPTANCE: full suite green with 0 skipped (command in the header of this file).
Manual spot-check: run the dev server against a dev DB and confirm all three pages
render historical data on the post-rollover league and each filter control works,
and /admin/corrections still lists trades. Do not commit; end with a summary.
```

---

## Item 3 — History page blanked by the rollover

**Priority:** third — same rollover blanking as Item 2, smallest item in the queue.
**Recommended model:** Haiku 4.5.
**Backlog entry:** "History page is scoped to current league row".

**Plan.** `get_history` (`services.py:5392`) and its two helpers
(`_discovery_by_season` ~5455, `_cups_by_season` ~5436) filter **five** queries by
`league_id` — `SeasonHistory`, `ManagerHonors`, `HistoricalStanding`,
`DiscoveryResult`, `CupMatch` (the backlog entry only names the first three). History
is by definition cross-season and there is only one logical league, so the fix is
dropping all five filters, keeping year-descending order, with a cheap dedupe guard
in case the same year's rows were ever imported onto two league rows. All five
tables carry free-text names (`manager_name`, `team_name`, `player_name`), so there
is no cross-row FK or name-map hazard.

**Session prompt:**

```text
Read CLAUDE.md in full, then the docs/BACKLOG.md entry titled "History page is
scoped to current league row". Work only on this fix.

CONTEXT (2026-08-18): the 26/27 rollover ran; services.current_league returns the
new 26/27 row, which has no history rows, so /history renders blank. History data
lives on older league rows. There is only ever one logical league, so querying
across all league rows is correct.

READ BEFORE WRITING: services.py get_history (~5392) and the two helpers it calls,
_cups_by_season (~5436) and _discovery_by_season (~5455); the /history route in
ui.py (~1357); templates/history.html. Note the backlog entry names three filtered
tables but there are FIVE league-scoped queries across those three functions:
SeasonHistory, ManagerHonors, HistoricalStanding, DiscoveryResult, CupMatch.

BUILD:
1. Remove the league_id filter from all five queries (get_history and both
   helpers). Keep the existing orderings (year/season descending, rank, honors
   sort). The functions may keep their `league` parameter for signature stability
   even if unused — note it in the docstring.
2. Dedupe guard, in case the same year's rows exist on two league rows: seasons
   deduped by year (first wins), honors by manager name, standings/discovery/cup
   groups merged by year. If you observe actual duplicates while testing, list them
   in your final summary — do not edit data.
3. No template changes expected; the shapes are unchanged.
4. Tests (DB-backed, existing style): seed history rows on an OLD league row while
   a NEWER row is is_current — /history (or get_history called with the current
   league) returns them; a duplicate-year seed on two rows renders once.
5. Update the backlog entry (status -> done).

ACCEPTANCE: full suite green with 0 skipped (command in the header of this file).
Do not commit; end with a summary.
```

---

## Item 4a — Discovery picks: real player links + the 4-year "discovery" clock (structural fix)

**Priority:** before the September discovery draft. Item 4b builds on this one.
**Recommended model:** Opus 5 — multi-surface change with precedence rules
(seed > discovery > derived) and a privacy-adjacent derivation.
**Backlog entry:** "Discovery-drafted players get a 3-year waiver clock instead of 4".
**Decisions (2026-08-18):** structural fix, not the one-branch patch; the displayed
acquisition label becomes `"discovery"` (added to `rules.KEEPER_ACQUISITIONS`);
linking a free-text pick to a real player row **always requires admin
confirmation** — never name-based auto-linking (`Player.name` is FPL's short
web-name, managers type full names; a wrong link is unrecoverable). A companion
session (Item 4b) adds sync-driven match *suggestions* and an admin dashboard on
top; `services.link_discovery_pick` built here is the single linking primitive it
will call.

**Plan.** Three pieces. (1) Let a discovery `DraftPick` carry `player_id` — already
storable with no migration (the CHECK is team-scoped; keep `player_label` alongside
as the as-entered name) — via a new admin "link discovery pick" tool used once the
player actually joins the PL and has a `players` row. (2) Teach
`_derive_keeper_status` about discovery acquisitions: a `(manager, player)` pair
with a linked discovery pick, or an `is_discovery` keeper selection (today only the
off-roster `discovery_only` set feeds this), resolves as `acquisition="discovery"` →
`rules.keeper_status` already returns `("discovery", 4)` through its override branch
untouched. Commissioner seeds keep precedence (a pinned test enforces this). (3) Fix
`submit_keepers`' on-roster branch so an `is_discovery` submission gets the
discovery clock instead of the derived `("waiver", 3)`, and add `"discovery"` to
`KEEPER_ACQUISITIONS` so `set_keeper_override` accepts it. The `"discovery"` label
flows through trades verbatim (`keeper_status`'s final branch returns `traded_from`
with no whitelist) — correct, and distinct from the receiver's own `is_discovery`
bonus-slot flag, which stays independent.

**Session prompt:**

```text
Read CLAUDE.md in full (especially the keeper and discovery-draft sections), then
the docs/BACKLOG.md entry titled "Discovery-drafted players get a 3-year waiver
clock instead of 4". You are implementing the STRUCTURAL fix with the decisions
below. NOTE: the backlog entry's line numbers are stale by ~200 lines; the ones
here are current.

DECISIONS (confirmed): the acquisition label is "discovery" (add it to
rules.KEEPER_ACQUISITIONS); discovery DraftPick rows get linked to real players.id
only through services.link_discovery_pick with explicit admin action — NEVER
auto-linked by name matching (Player.name is FPL's short web_name, managers type
full names; a wrong link is unrecoverable). A FOLLOW-UP session will add
sync-driven match suggestions that call your link function on admin confirm — so
build link_discovery_pick as a clean service-layer primitive (resolvable by pick +
player, idempotence-checked, audited), not as logic inlined in a route.

READ BEFORE WRITING:
- services.py: record_discovery_pick (5350-5382 — forces player_id=None today),
  get_discovery_board (5313-5347 — line 5341 already renders player_label OR a
  linked player_id, so the board needs no changes), _derive_keeper_status
  (2984-3184; the discovery_only set at 3059-3072, the acquisition= channel at
  3137-3139, the _status_for recursion at 3106-3147), submit_keepers (3456-3565;
  the off-roster is_discovery branch at 3486-3514 already synthesizes
  {"acquisition": "discovery", "years_remaining": KEEPER_FRESH_DRAFT}; the
  on-roster path has NO is_discovery handling — that is the bug), set_keeper_override
  (2741-2807; validation at 2752-2760 rejects "discovery" today), search_players
  (4986-5125; the taken overlay at 5085-5090 is draft_type-scoped), delete_draft_pick
  (~5285) and corrections_data (~5486) for the admin-surface pattern.
- rules.py: KEEPER_ACQUISITIONS (442), keeper_status (445-495 — the
  `if acquisition:` branch at 483-488 treats any non-"waiver" label as
  draft-clocked, so this function needs NO changes; the final branch at 494-495
  returns traded_from verbatim, so "discovery" flows through trades unchanged).
- models.py: DraftPick (527-585 — the CHECK is team-scoped, so
  (draft_type='discovery', player_id set, player_label kept) is storable with NO
  migration), KeeperSelection (476-524), KeeperSeed acquisition comment (~472).
- tests/test_discovery_keeper_slot.py — the most load-bearing file; its
  seed-overrides-discovery-clock test (176-193) pins the precedence you must keep.

BUILD:

1. rules.py: add "discovery" to KEEPER_ACQUISITIONS. Nothing else in rules changes.
   set_keeper_override now accepts it for free via its membership check; update the
   KeeperSeed.acquisition comment in models.py to mention it.

2. Admin link tool: services.link_discovery_pick(db, league, *, season_year,
   pick_number, player_fpl_id) — resolves the DraftPick (draft_type='discovery'),
   resolves the player by fpl_id (he has one by the time linking matters — he just
   joined the PL; that is the whole use case), sets player_id while KEEPING
   player_label as the as-entered name, writes a record_audit entry, commits.
   Refuse if the pick doesn't exist or the player is already linked elsewhere for
   the same season/draft_type (the taken overlay in search_players at 5085-5090
   will start marking him taken in discovery searches once linked — that is
   correct, and a test should pin it). Admin route + a small form on
   /admin/corrections beside the existing pick-correction tools, using the
   established admin-write pattern (services function enforcing rules,
   RuleViolation -> 400). An unlink path (set player_id back to NULL) with its own
   audit action, for a mislink.

3. _derive_keeper_status: build a discovery-pick map alongside the other inputs
   (~3038): the set of (manager_id, player_id) for DraftPick rows with
   draft_type='discovery' AND player_id IS NOT NULL for the relevant season. Key
   the manager side via Manager.fpl_manager_id bridging, NOT raw managers.id —
   manager rows are per-season and picks may live on a different league row (see
   _goalie_team_history at 3187-3196 for the precedent/hazard). Also split the
   existing discovery_only logic: keep discovery_only (off-roster is_discovery
   selections) for the candidate-set union it already does, and add a broader
   discovery_flagged set (ALL is_discovery selections, on- or off-roster). Then the
   acquisition channel at 3137-3139 becomes: seed_acq wins, else "discovery" if the
   pair is in discovery_only, discovery_flagged, or the new linked-pick map, else
   None. Do NOT touch the privacy gating (kept_for/kept_all) — the linked-pick map
   comes from DraftPick, which is public draft history, but discovery_flagged comes
   from KeeperSelection and must stay behind the same gate discovery_only already
   respects.

4. submit_keepers on-roster branch: when is_discovery and status.get(player.id)
   hits, override to acquisition="discovery", years_remaining=KEEPER_FRESH_DRAFT —
   UNLESS a KeeperSeed row exists for (manager, player), in which case leave the
   derived (seed-driven) status alone. The pinned test at
   test_discovery_keeper_slot.py:176-193 (commissioner seed overrides the
   discovery clock) must keep passing. The GKP-refusal guard in the off-roster
   branch applies to the on-roster discovery case too when goalie teams are on —
   apply the same check.

5. Leave alone: rules.keeper_status; the waiver-cap exemption
   (validate_keeper_selection keys on the is_discovery FLAG, not the label — do not
   conflate them); record_discovery_pick stays free-text at record time (the player
   usually has no players row yet in September); DiscoveryResult (the historical
   free-text table) is out of scope; get_discovery_board (already renders both).
   A traded-away discovery player: the "discovery" label follows him through
   keeper_status's traded_from chain automatically — but the receiver's OWN
   is_discovery bonus-slot flag does not, by design.

6. Tests (DB-backed, existing style):
   - A linked discovery DraftPick + the player on the manager's roster -> derived
     status ("discovery", 4) with NO keeper selection submitted.
   - An on-roster is_discovery submission (unlinked pick) -> selection records
     acquisition "discovery" with 4 years, not ("waiver", 3).
   - Seed precedence: existing test_discovery_keeper_slot.py:176-193 still passes,
     plus the same assertion through the new linked-pick path.
   - set_keeper_override accepts acquisition="discovery".
   - link_discovery_pick: happy path (label kept, audit written), refuses a
     nonexistent pick, refuses double-linking; after linking, search_players marks
     the player taken in discovery-draft search but NOT in main-draft search
     (mirror tests/test_draft_availability.py:140-148).
   - Trade chain: sender has a discovery-acquired player; receiver's derived label
     is "discovery" with the sender's clock.
   - The whole existing test_discovery_keeper_slot.py and test_keeper_privacy.py
     suites pass unchanged.

7. Docs: update the backlog entry (done), and CLAUDE.md's keeper/discovery bullets
   to mention the "discovery" acquisition label and the admin link tool.

ACCEPTANCE: full suite green with 0 skipped (command in the header of this file).
Do not commit; end with a summary including anything about the precedence rules
that surprised you.
```

---

## Item 4b — Discovery match suggestions: sync-driven matching + admin dashboard

**Priority:** after Item 4a lands; before the September discovery draft pays off in
January (when picks start joining the PL).
**Recommended model:** Opus 5 — two migrations, a matching algorithm, a sync hook,
and a new admin surface.
**Depends on:** Item 4a's `services.link_discovery_pick`.
**Decisions (2026-08-18):** when the daily player sync brings in new FPL data,
unlinked discovery picks are matched against it; an exact or close match is flagged
for **admin confirmation** — nothing ever auto-links, even a perfect-score match
(two humans can share a name). Everything unmatched stays a manual process, via an
admin dashboard listing every unlinked discovery pick.

**Plan.** Verified 2026-08-18: **no sync-cadence change is needed** — the first
cron hit of each UTC day always runs a full sync (`rules.decide_sync`, rules.py:187)
and `sync_players` is deliberately ungated by season freeze (sync.py:190-203), so
the player pool already refreshes daily year-round, including preseason. Three
pieces: (1) store the player's full name — `Player` keeps only the short web-name
today and the bootstrap's `first_name`/`second_name` are discarded, which is what
makes typed-full-name matching possible; (2) a matcher producing *suggestions* in a
new purpose-built table (the existing `commissioner_alerts` table is dead code with
no status/FK columns — do not use it); (3) an "Unmatched discovery picks" dashboard
section on `/admin/corrections` with confirm/reject per suggestion and a manual
search fallback.

**Session prompt:**

```text
Read CLAUDE.md in full (two-truths boundary especially), then the docs/BACKLOG.md
entry "Discovery-drafted players get a 3-year waiver clock instead of 4" for
background, and docs/SESSION_PLANS.md Item 4a — this session builds the match-
suggestion pipeline on top of 4a's services.link_discovery_pick, which must already
exist. Work only on this feature.

FACTS VERIFIED IN ADVANCE (do not re-derive):
- Sync cadence needs NO change: rules.decide_sync (rules.py:187) returns "full" on
  the first cron hit each UTC day; sync_players runs on every full plan and is
  deliberately ungated by season freeze (sync.py:190-203) — the pool refreshes
  daily year-round.
- Player (models.py:131-176) stores ONLY web_name in `name` (_name(e) at
  sync.py:258 keeps web_name; first_name/second_name are discarded). The full
  bootstrap element dict `e` is in scope in sync_players phase 2 (sync.py:302-336).
- commissioner_alerts (models.py:699-709) is dead code — zero readers/writers, no
  status/FK columns. Do NOT use it; build a purpose-built table.
- The post-sync hook site is main.py:246-256 (/admin/sync lives in main.py, not
  admin.py), where flag_ineligible/reconcile_absences already run under
  `if plan in ("full", "live")`.
- Unlinked picks enumerate as: draft_type='discovery', player_id IS NULL,
  player_label IS NOT NULL, team_id IS NULL (defensive — models.py:532-537 warns
  the CHECK is deliberately narrow, so don't assume exactly-one-of on live rows).
  season_year is on the row and indexed.

BUILD:

1. Migration #1: `players.full_name` (nullable String), written by sync_players
   phase 2 from bootstrap `first_name + " " + second_name`. This is FPL-canonical
   data written by sync — the legal side of the two-truths boundary. Include it in
   the sync notes counts only if trivial; do not restructure sync_players.

2. Migration #2: `discovery_match_suggestions` table — id (UUID pk),
   draft_pick_id FK, player_id FK, score (float), method (str), status
   ('pending'|'confirmed'|'rejected'), created_at (server_default now()); UNIQUE
   (draft_pick_id, player_id) so the daily run upserts instead of duplicating, and
   a rejected pair is never re-raised.

3. services.match_discovery_picks(db) -> summary dict (counts for logging):
   - Enumerate unlinked discovery picks (filter above) across league rows.
   - Candidates: all Player rows. Normalize with a TOKEN-WISE variant of
     scripts/import_projections.py's _norm + _TRANSLIT (lowercase FIRST, translit
     SECOND, NFKD THIRD — the order is load-bearing; ø/ı do not NFKD-decompose).
     Do NOT reuse or modify history_import._norm — its comment forbids unifying.
   - Match tiers: exact (normalized label == normalized full_name or web_name,
     score 1.0); strong (label tokens are a subset of full-name tokens or vice
     versa); close (difflib.SequenceMatcher ratio >= 0.85 on joined normalized
     strings — stdlib only, this repo is deliberately dependency-austere; do not
     add rapidfuzz/python-Levenshtein).
   - Upsert 'pending' suggestions for each pick/candidate pair not already
     confirmed/rejected; multiple candidates per pick allowed, ranked by score.
     NEVER write DraftPick.player_id here — even a 1.0 score only suggests.
   - This function reads league-custom tables, so it lives in services.py and is
     NOT called from inside sync.py (sync must never touch league-custom tables).

4. Wire-up: call match_discovery_picks from main.py's post-sync block under
   `if plan == "full":` — but OUTSIDE the `not league.sync_locked` guard that gates
   flag_ineligible: the matcher's relevance doesn't depend on the current league's
   freeze state. Also add an admin "Run matching now" POST route that calls it
   directly and redirects back to /admin/corrections.

5. Dashboard: an "Unmatched discovery picks" section on /admin/corrections (route
   ui.py:1157-1169, context builder services.corrections_data ~5472, template
   templates/admin_corrections.html — follow its existing h2 + card + table +
   per-row POST form idiom, delete-style confirm() where destructive). Per unlinked
   pick: label, season_year, owner, pending suggestions (candidate name, club,
   score, Confirm / Reject buttons), and a manual fallback that searches by name
   (the existing search_players unaccent path) and links by fpl_id via 4a's
   link_discovery_pick. Confirm = link + mark confirmed + audit; Reject = mark
   rejected + audit; both follow the corrections audit convention (previous values
   in details, services.py:5292-5305 as the pattern). Add a one-line visibility
   check to services.data_health (~5641, the add(name, ok, detail) idiom):
   "unlinked discovery picks" with the pending-suggestion count in the detail.

6. Tests (DB-backed, existing style):
   - Matcher: exact full_name match; web_name-only match (typed "Nick Woltemade"
     vs web_name "Woltemade" via token subset); accent/translit cases (Ødegaard,
     Kadıoğlu — mirror tests/test_projections.py:36's guard); a close match
     produces a pending suggestion with score < 1.0; a rejected pair is NOT
     re-raised on a second run; nothing is ever auto-linked.
   - Confirm flow: confirm links the pick (the discovery board renders the real
     player via the existing `dp.player_label or pnames.get(dp.player_id)` at
     services.py:5341) and writes audit; reject persists across runs.
   - sync_players writes full_name; existing sync tests unaffected.
   - data_health line reflects unlinked/pending counts.
7. Update docs/BACKLOG.md (this extends the discovery entry) and CLAUDE.md's
   discovery bullet with one line about the suggestion pipeline.

ACCEPTANCE: full suite green with 0 skipped (command in the header of this file) —
note the test fixture builds the schema from migrations, so both new migrations get
exercised automatically. Manual pass on a dev DB: record a fake discovery pick, run
the matcher, confirm a suggestion on /admin/corrections, see the linked name on the
discovery board. Do not commit; end with a summary.
```

---

## Item 5a — Migrate the 2026 draft onto the 26/27 row

**Priority:** soon — until it runs, `/draft/2026` renders empty and the 26/27 season
has no draft history of its own.
**Recommended model:** Opus 5 — a one-shot production data migration plus a subtle
board-derivation change. Not a Haiku/Sonnet item.
**Backlog entries:** "Post-GW38 activity should belong to the following season" (the
migration half) and "2026 draft board inaccessible after rollover" (closed by this).
**Decisions (2026-08-18):** split from the provisional-row architecture (Item 5b,
then parked as a planning session; retired 2026-08-20). **FuturePick rows and pick-trade Trade rows do NOT
move — future picks are season-agnostic by design**, a standing ~5-year outlook on
pick ownership that never migrates between league rows (Item 2 already reads them
cross-league). Seed reconciliation is **report-only**: discrepancies are listed and
fixed by hand via `/admin/keepers`, never auto-corrected.

**Plan.** Move the 2026-stamped draft data from the 25/26 row to the 26/27 row
(`DraftPick`, `KeeperSelection`, `draft_lottery`, `draft_order_override`), remapping
`manager_id` FKs by `fpl_manager_id` since manager rows are per-season. Two code
changes ride along, both corrections to the backlog's sketch: (1) the five
`season_year + 1` call sites do NOT get a blanket flip — the 2027 draft will again
run pre-rollover where `+1` is correct; they need the phase-dependent draft-year
helper (Item 1 introduces it for draft/preseason; extend to all phases). (2) The
board can't render a migrated draft unaided: `get_draft_board(current, 2026)`
derives rounds 2+ from reverse standings, and the 26/27 row has NO Standing rows —
the order derivation must resolve its standings source from the row whose
`season_year == draft_year − 1`, falling back to the passed league. Mandatory ops
rails: snapshot first, full rehearsal on a Neon test branch, dry-run-default script.

**Session prompt:**

```text
Read CLAUDE.md in full (especially "League phase lifecycle & multi-season" and the
main-draft section), then the docs/BACKLOG.md entries "Post-GW38 activity should
belong to the following season" and "2026 draft board inaccessible after rollover".
You are doing the MIGRATION half only — the provisional-row architecture for future
seasons is explicitly out of scope. This touches production league data: follow the
ops rails below exactly.

CONTEXT (2026-08-18): the 2026 draft ran pre-rollover, so its rows live on the
25/26 league row with season_year=2026 and manager_id FKs pointing at 25/26 Manager
rows. The rollover then created the 26/27 row (is_current, season_year=2026). The
migration moves the draft data to the row it belongs to.

DECISIONS (confirmed): FuturePick rows and pick-trade Trade rows DO NOT MOVE —
future picks are season-agnostic by design (a standing multi-year outlook; the
/picks page reads them cross-league). Trades are a historical record and stay where
they happened. Seed reconciliation is REPORT-ONLY.

READ BEFORE WRITING: services.py get_draft_board, _reverse_standings_managers,
effective_keeper_selections (~4396), advance_season (~561 — note how it already
carried keeper seeds years−1 and keeper selections onto/off the rows at rollover),
_goalie_team_history (queries DraftPick by season_year with NO league filter — it
must be unaffected); models.py DraftPick (uq_draftpick_slot on league_id +
season_year + draft_type + pick_number), KeeperSelection (unique manager_id +
player_id + season_year), DraftLottery, and the draft_order_override table;
scripts/cleanup_recycled_league.py (the house pattern for a one-off repair script)
and scripts/import_projections.py (the dry-run-default + --apply pattern);
snapshot.py. The five season_year+1 call sites: templates/base.html (~108, draft
nav), ui.py (~367 keepers, ~1099 draft-prep), scripts/preflight_draft.py (~63), and
the /admin/health check in services.py — line numbers are from 2026-08-15 and have
drifted; search by expression.

BUILD:

1. scripts/migrate_2026_draft.py — dry-run by default, --apply to execute:
   - Resolve source row (25/26, season_year=2025) and target row (26/27,
     season_year=2026, is_current) explicitly; abort loudly if either is ambiguous.
   - Build the manager remap old_manager_id -> new_manager_id via
     Manager.fpl_manager_id; abort if any referenced manager has no counterpart.
   - Move to the target row, remapping manager FKs: all DraftPick rows with
     season_year=2026 on the source row (all draft_types — discovery may or may not
     exist yet); all KeeperSelection rows with season_year=2026; all DraftLottery
     rows for the 2026 draft; all draft_order_override rows for 2026 (if any).
     Check unique-constraint collisions on the target BEFORE writing; abort on any.
   - KeeperSeed reconciliation, REPORT-ONLY: for each source-row seed, verify the
     target row has the carried seed advance_season should have written (years−1,
     same player, remapped manager); list missing/duplicated/mismatched ones and
     exit nonzero without touching them — they get fixed by hand via
     /admin/keepers.
   - Never touch FPL-canonical tables (rosters, standings, gameweeks, players,
     transactions) or Trade/FuturePick rows.
   - Dry run prints exact row counts per table and the manager remap; --apply
     wraps everything in one transaction.

2. Draft-year helper: Item 1 introduced a phase-dependent helper (draft ->
   season_year+1, preseason -> season_year). Extend it (or create it in services.py
   if Item 1 hasn't landed) to cover all phases: offseason and draft -> +1,
   preseason and in_season -> season_year. Apply it at the five season_year+1 call
   sites. Do NOT blanket-flip them to season_year: the 2027 draft runs pre-rollover
   on the 26/27 row, where +1 is correct again.

3. Board order derivation: get_draft_board(league, year) must resolve the standings
   source for rounds 2+ (and anything else that reads the finishing season's order)
   from the league row whose season_year == year - 1, falling back to the passed
   league when no such row exists. Without this, the migrated 2026 board generates
   ZERO slots — the 26/27 row has no Standing rows. DraftLottery and
   draft_order_override lookups stay on the passed league (the script moved them).
   Keep the stored-manager_id-wins rule for already-picked slots intact.

4. Tests (DB-backed, existing style):
   - Seed a two-row scenario mirroring prod (old row with standings + a completed
     draft's picks/selections/lottery stamped year N, new row is_current with
     season_year N and no standings); run the script's move logic; assert
     get_draft_board(new_row, N) renders every pick with correct owners, and
     effective_keeper_selections(new_row, N) returns all selections.
   - The unique-collision abort and the missing-manager abort both fire.
   - Seed reconciliation reports a synthetic drift case without writing.
   - The draft-year helper: each phase maps to the right year.
   - _goalie_team_history results identical before/after a move.

5. Docs: update both backlog entries (migration done; board entry closed). Note in
   the "Post-GW38" entry that FuturePick/Trade rows are deliberately excluded —
   future picks are season-agnostic by design.

OPS RAILS (in order, non-negotiable):
a. python snapshot.py save (prod) — keep the file.
b. Create a Neon test branch of main; run the script (dry-run, then --apply)
   against the BRANCH with APP_ENV=test; browse /draft/2026, /teams, /keepers,
   /admin/health there.
c. Only then run against prod: dry-run, review counts, --apply.
d. Verify on prod: /draft/2026 full board, /admin/health all green.
e. Confirm with the user that Render's FPL_DRAFT_LEAGUE_ID env var points at the
   NEW season's FPL league id — if it still names the old frozen league, the
   nightly cron reports green while syncing nothing.
f. Full regression, 0 skipped (command in the header of this file).

Do not commit; end with a summary including the prod row counts moved and the seed
reconciliation report.
```

---

## Item 5b — Provisional-row architecture for post-GW38 season alignment (RETIRED 2026-08-20)

**Priority:** **retired — do not run this session.** Full reasoning in
`docs/BACKLOG.md` under "What to do next". In short: its payoff was removing the
draft-row migration, that migration is now scripted, tested and health-checked, the two
bugs it would have prevented are fixed in the read path instead, and the build lands on
`verify_league_feed`'s recycled-league guard — which a provisional row (managers carried,
no FPL id yet) is precisely the wrong shape for. Revisit only on a named trigger: a fifth
league-scoped table taking post-GW38 writes, or the migration's manager remap breaking.
The prompt below is kept as the design brief if that trigger ever fires.
**Recommended model:** Opus 5, producing a written design — **no code in that
session**.
**Backlog entry:** "Post-GW38 activity should belong to the following season" (the
architecture half).

**Plan.** Design (don't build) the mechanism that lets post-GW38 activity land on
the following season's row from day one: a provisional league row created at season
end, reconciled with the real FPL league id when it appears in August. The hard
constraint is `leagues.fpl_league_id` being unique and non-nullable while the new
FPL league doesn't exist yet at GW38.

**Session prompt:**

```text
Read CLAUDE.md in full (especially "League phase lifecycle & multi-season" and
"FPL league ids are NOT stable across seasons"), then the docs/BACKLOG.md entry
"Post-GW38 activity should belong to the following season". Produce a DESIGN
DOCUMENT (a new docs/ file) and an implementation plan — write NO application code
in this session.

The design must answer, at minimum:
1. Schema: how a provisional league row exists before the FPL league id does
   (nullable fpl_league_id + partial unique index? a placeholder? a separate
   provisional flag?) and how advance_season becomes "reconcile the provisional row
   with the real league" instead of "create new". Cover the failure mode where the
   real league's manager set differs from the carried one (verify_league_feed's
   "a row with no managers accepts anything" rule must not let a recycled id merge
   into a provisional row that HAS managers).
2. Resolution: how current_league, the login/identity flow (entry_id -> manager
   row), sync's _resolve_league, and the FPL_DRAFT_LEAGUE_ID env interact with a
   provisional row — including whether manager rows are provisionally created
   (identity carry happens at rollover today).
3. Writers: every post-GW38 writer that must target the provisional row (keeper
   selections, draft lottery, order overrides, draft picks) — and the ones that
   deliberately must NOT move: FuturePick and pick-trade Trade rows are
   season-agnostic by design (decided 2026-08-18; a standing multi-year pick
   outlook that never migrates), and trades are historical records attributed to
   seasons on read (the Jan-31 rule from the trades-page work).
4. Readers: the phase-dependent draft-year helper (built in Items 1/5a) is the
   single place the season_year+1 compensation lives — show how the provisional
   row lets it collapse to an identity, and what regression tests pin the cutover.
5. Migration/cutover: how existing seasons are unaffected, and what the first
   provisional season's rollout looks like step by step.

Ground the design in the actual code: advance_season, current_league,
sync._resolve_league, rules.verify_league_feed, enter_draft_phase, and the Item 5a
migration script as the worked example of the data shapes involved. End with the
open questions the commissioner must decide before implementation.
```

---

## Item 6 — IL ownership done right (design session) — **DONE 2026-08-20**

**Outcome:** design agreed and written to **`docs/DESIGN_IL_OWNERSHIP.md`**. No code
written, as intended. The build is split into **Item 6b** and **Item 6c** below;
`6c` depends on `6b`. Six league rules were settled by the commissioner in that
session (additive ownership; IL capped at one but the international list **uncapped**;
the roster is always 15 so a player can never be dropped without a replacement; the
returning player may displace **anyone**; an absence still open after GW38 must be
resolved down to 15, by Releasing the absentee or Returning him and **naming who
leaves**; a must-return alert keyed on minutes, with fines left to the existing manual
ledger; no validation of international absences)
— they are recorded in §2 of the design doc and are not open for re-litigation.
An earlier rule requiring the *replacement's tracked slot* to be released was
**withdrawn 2026-08-20** as too hard to automate — FPL records no paired add/drop. The
manager naming the departing player is what replaced it (§4.4/§5).
The session also found four things the naive design gets wrong (fold order, the
`status == 'active'` predicate, cessation at season end, the wrongly-capped
international list) and two live bugs; all are in the doc.

**Priority:** before this season's first IL case — the 26/27 season is starting, IL
self-service is live on My Team, and the last two IL cases each required live
debugging plus manual row surgery on a frozen season, which is not available
mid-season.
**Recommended model:** Opus 5, producing a written design and a follow-up build
prompt — **no application code in that session**.
**Backlog entries:** "Review IL-driven keeper restoration end to end" (the live
entry), plus the retired "Kevin's Kudus" and "G. Jesus should be a free agent"
entries as the incident record and the release-overlay fix sketch.
**Decisions (2026-08-18):** the 25/26 cleanup is moot — the frozen row is
quarantined and nothing forward-looking reads it; do NOT design for retroactive
repair. IL and discovery remain **separate concepts**: the `is_discovery` flag must
never again be used to represent an IL situation (the Kudus workaround). What gets
unified is the READ side only — one "who effectively holds this player" layer with
trades, IL, discovery, and (if the design needs it) releases as separate inputs.

**Session prompt:**

```text
Read CLAUDE.md in full (two-truths boundary, trades overlay, IL sections), then
three docs/BACKLOG.md entries: "Review IL-driven keeper restoration end to end"
(the brief), and the retired "Kevin's Kudus" and "G. Jesus should be a free agent"
entries (the incident record + the release-overlay fix sketch). Produce a DESIGN
DOCUMENT (a new docs/ file) — write NO application code. This is a design session
for the NEXT mid-season IL case, on a live, syncing season; retroactive repair of
the frozen 25/26 season is explicitly out of scope (decided 2026-08-18).

THE PROBLEM IN ONE SENTENCE: when a manager places a player on the injury list and
FPL's synced roster shows the replacement instead, exactly one reader
(_derive_keeper_status) knows the IL'd player is still held — every other reader
of ownership (get_rosters /my-team, player_portal, effective_owner /
effective_keeper_selections slot math, the health check) disagrees, and each past
incident was fixed by bolting a bespoke boolean onto whichever reader broke first
(three carve-outs so far, each found live).

DESIGN CONSTRAINTS (settled, do not revisit):
- IL and discovery stay SEPARATE concepts. The is_discovery flag is for the
  discovery draft's bonus keeper only; representing an IL situation with it (the
  Kudus workaround) must become impossible or unnecessary.
- The unification happens on the READ side only: extend the existing overlay
  pattern (services.player_ownership / effective_owner / _owner_maps — the sibling
  pattern CLAUDE.md documents for trades) so IL coverage feeds the same single
  ownership answer that trades already do. Never write or delete rosters rows —
  the two-truths boundary. No new parallel mechanisms.
- rosters stays FPL-canonical; anti-tanking's _absence_cover and the keeper
  derivation's covered-gameweeks set must remain in agreement (CLAUDE.md notes
  they share the same set today — the design must not fork them).

THE DESIGN MUST ANSWER:
1. Mid-season IL semantics end to end: manager places player X on IL, drops him in
   FPL, adds replacement Y (synced). What does every surface show — my-team, teams,
   players tab, transactions, anti-tanking, keeper derivation, slot math — while X
   is on IL? On X's return (or season end)? Is the league's intent "16 held, 15
   active + 1 IL" or "15 held with X's slot lent to Y"? Ground this in
   docs/requirements.md's IL rules (one IL player, same-position replacement, 4-GW
   minimum, return after GW38 or via waiver).
2. Whether place_on_il / the IL backfill should feed the ownership overlay
   automatically (the backlog's question 1) — and what return_from_il unwinds.
3. Whether a release path ("player goes to nobody") is required by the IL design
   (e.g. season-end replacement release) — if yes, incorporate the retired G. Jesus
   entry's roster_releases fix sketch (a subtraction folded into _owner_maps after
   the trade fold) including its four named consequences: keeper clock via
   _dropped, draft availability via search_players' taken-oracle, the /admin/health
   15-man check staying RAW deliberately, and the admin form. If no, say why.
4. The reader audit (the backlog's question 3): enumerate every reader of
   player_ownership / effective_owner / _effective_roster_pids and every direct
   Roster reader, and state for each whether it consumes the new layer or
   deliberately reads raw (with the reason recorded, like the health check's).
5. The keeper interaction: how end-of-season IL (the Šeško/Kudus shape) works
   next time with ZERO manual steps — IL row exists, keeper candidacy appears,
   the replacement's fate is handled, no row surgery, no is_discovery costume.
6. Migration/compat: confirm nothing changes for seasons already frozen, and that
   the Kudus-era is_discovery carve-outs (e503afd, 958cce8) can stay in place
   for the discovery draft's legitimate use without ambiguity.

END WITH: (a) the open questions the commissioner must decide, if any; (b) a
build-session prompt appended to docs/SESSION_PLANS.md as "Item 6b", in the same
format as the other items, with a recommended model.
```

---

## Item 6b — Absence ownership: one predicate, one fold — **BUILT 2026-08-20**

**Outcome:** built in the same session as the design, uncommitted for review. 760 passed
/ 0 skipped; the 2 failures are the known `P3` prod-DB test files (`test_sync_freeze`
fails on a clean tree too, `test_demo` only because production hasn't had migration
`d4e5f6a7b8c9` applied). Every guard mutation-tested, including fold order.
Deviations from the plan below, both deliberate: the admin **historical IL backfill** had
to be exempted from the new roster-ownership guard (`require_roster=False`) — that route
exists precisely because the snapshot shows the replacement, so guarding it refuses every
case it was built for; and `unresolved_absences` treats **phase `offseason`** as season
over, not just `current_gameweek >= 38`, because `current_gameweek` returns None when
deadline dates are missing and the guard would have been silently inert.


**Priority:** before this season's first IL case. This is the half that retires the
three carve-outs; until it lands, the next absence is another live debugging session.
**Recommended model:** Opus 5 — a change to the single most load-bearing read path in
the app (`_owner_maps` feeds rosters, My Team, the Players tab, the keepers page and
draft slot math), with two orderings that are wrong in the obvious direction.
**Design doc:** `docs/DESIGN_IL_OWNERSHIP.md` — read it in full first; §4 is the build.
**Backlog entry:** "Review IL-driven keeper restoration end to end".
**Decisions:** all seven league rules in §2 of the design doc are settled — do not
re-open them. In particular the international list is **uncapped**, ownership is
**additive** (effective squad = 15 + ≤1 IL + N intl), and an absence open after GW38
must be resolved back to 15.

**Plan.** Extract `_absence_held` as the single "who holds him now" predicate and fold
it into `_owner_maps` **before** the trade fold, then switch `services.py:3248` to the
same helper so candidacy and ownership cannot disagree. Add the season-end resolution:
an absence open after GW38 must balance the squad back to 15, so the existing Return
button must ask which player leaves and store it in a new `released_player_id`, folded
as the one subtraction (§4.4). Then the supporting corrections in §10: lift the international cap
(guard + docstring + the `{% elif %}` in the template), close the `place_on_il` write
path, block `advance_season` on open absences, fold `manager_assets` and `player_portal`
onto the overlay, and add the `data_health` checks. `_absence_cover` stays exactly as it
is — it answers a different question and anti-tanking must not change.

**Session prompt:**

```text
Read CLAUDE.md in full (the two-truths boundary, the trades overlay, the IL and
anti-tanking bullets), then docs/DESIGN_IL_OWNERSHIP.md in full. That design is
agreed; implement §4 and §10 of it. Do NOT redesign, and do NOT re-open the seven
league rules in §2 — they were decided by the commissioner. Item 6c (the
return-required alert) is a SEPARATE later session: do not build it here. §5 of the doc
records a WITHDRAWN rule (deriving slot succession) — do not build that either.

Line numbers in the doc may have drifted — search by symbol name.

THE CORE CHANGE. Extract a helper `_absence_held(db, league, last_n)` returning
{(manager_id, player_id)} for players a manager still holds through an injury-list or
international-list entry. Predicate: held iff status == 'active', OR (status ==
'returned' AND last_n <= end_gw). 'waived' is never held past end_gw; NULL status is
not held. Then:

1. Fold it into services._owner_maps BEFORE the existing trade fold, not after. The
   trade fold is guarded `if owner.get(t.player_id) == t.from_manager`; an absent
   player has no snapshot owner, so folding absences second makes a trade of an
   absent player permanently unappliable and pins /admin/health's "site trades
   applied" check red forever. Guard the absence fold with `if pid not in owner` so a
   mis-entered absence can never steal a rostered player, and order the query by
   (start_gw, id) — player_ownership is called twice in one player_portal request and
   a nondeterministic winner makes two panels on one page disagree. Comment both, the
   way the trade guard above is commented.
2. Replace the `il` half of the candidacy union in _derive_keeper_status (the
   `final_candidates |= {...covered...}` line) with the SAME helper. This is not
   cosmetic: it is what stops candidacy and ownership forking on a season-end return,
   and it fixes a live bug where a player dropped and claimed by ANOTHER manager
   becomes a keeper candidate for both (reconcile_absences keys on (manager, player),
   so the original entry never auto-closes).
3. Season-end resolution (§4.4), the ONE subtraction in this design. An absence still
   open after GW38 must resolve the squad back to 15, and the end-of-season prompt on
   My Team already exists ("Season over — add this player back or Release them"). Its
   Release button already resolves correctly (the absentee goes to nobody, the frozen 15
   stands). Its RETURN button does not: it leaves the manager holding 16. Fix that
   branch — Return must also ask which of the frozen 15 leaves, and store it.

   Storage is a nullable released_player_id FK on InjuryList / InternationalList, NOT a
   roster_releases table: there is at most one release per absence resolution and it
   belongs on the row that caused it. Set it in return_from_il / return_from_intl; fold
   it into _owner_maps as a subtraction AFTER the additive fold. It is required only at
   season end — mid-season the manager swaps in FPL and the sync sees it, so the column
   stays NULL and cessation is right.

   It is MANAGER-DESIGNATED, never derived. Do not try to infer who should leave; §5 of
   the doc records why that rule was withdrawn.

   Enforce it where it would be cashed in: submit_keepers refuses for a manager with an
   unresolved post-GW38 absence (manager-scoped, not a global lock — it should stop only
   the person who owes a decision). Add a data_health check listing them.

   Still NOT built: a roster_releases table, a release reason, releasing a player outside
   an absence resolution, an admin-only release form, or any automatic choice of who
   leaves.

LEAVE _absence_cover EXACTLY AS IT IS. It answers a different question ("was he
excused that gameweek") and is shared with anti-tanking; the two must keep reading the
same rows but they legitimately differ in predicate. Do not merge them.

THEN the supporting corrections (§10 of the doc):
 - Lift the international-list cap: drop the one-active-entry guard in
   place_on_intl, fix the models.py docstring that claims one per manager, and move
   the "Send a player to international duty" form in templates/my_team.html off its
   {% elif %} so it renders alongside an existing absence. KEEP place_on_il's cap.
 - place_on_il must require the injured player to be on the manager's effective
   roster, and must refuse injured == replacement (place_on_intl already does).
 - advance_season must refuse while any absence entry is active, in the same
   fail-loudly shape as its pairing check, with the same force=True hatch. The
   absence overlay is NOT self-retiring the way the trade overlay is — say so in a
   comment.
 - manager_assets: route players through _effective_roster_pids instead of joining
   Roster raw. player_portal: replace its hand-rolled copy of effective_owner with a
   call to effective_owner.
 - New data_health checks: an active entry whose player is rostered by someone else;
   one player named by two managers' entries; NULL-status entries.

EVERY reader left reading raw Roster must carry a comment saying why, in the style of
the existing "Deliberately raw Roster, NOT the trade overlay" comment on the 15-man
check. The absence of such comments is what let three carve-outs accumulate. §9 of the
doc is the audit — reconcile it against the tree and fix any drift.

TESTS. tests/test_trade_overlay.py is the template: a test per reader. Cover at
minimum — the fold order (a trade of an absent player applies, and the health check
goes green); candidacy vs ownership agreeing when end_gw >= last_n (the existing
test_a_returned_il_entry_does_not_grant_candidacy_on_its_own uses end_gw=15 and does
NOT cover this); a player dropped and claimed by another manager is a candidate for
exactly one of them; season-end Release leaves 15 and season-end Return-plus-named-drop
also leaves 15, while an unresolved absence blocks submit_keepers for THAT manager only;
MULTIPLE simultaneous international
absences for one manager (every existing fixture has at most one); place_on_il's
one-active-entry guard; and that get_transactions, anti-tanking and the 15-man check
are all unchanged. Mutation-test the two guards and the fold order.

ACCEPTANCE: the full DB-backed regression, green with 0 skipped (command in the header
of this file). A run reporting "310 skipped" means TEST_DATABASE_URL never reached
pytest — that is a FAIL. Update docs/BACKLOG.md and the spec corrections in §13 of the
design doc (requirements.md, CLAUDE.md, the models.py docstring, and the "15-man" copy
in three templates). Do not commit; end with a summary.
```

---

## Item 6c — The return-required alert — **BUILT 2026-08-21**

**Outcome:** built the day after 6b, uncommitted alongside it. Migration
`e5f6a7b8c9d0` adds `last_played_gw` to both absence tables (chained off 6b's
`d4e5f6a7b8c9`). `services.record_absentee_minutes(db, league, live_stats, gw_number)`
persists it — called from `sync.sync_gameweek_points` with the `elements` map already
fetched, no new HTTP call. `services._return_required_entries` is the shared predicate
consumed by both `flagged_actions` (homepage nag) and a new `/admin/health` check ("no
absentee playing while still parked"). One deviation from the plan: the
minutes-persistence logic was pulled out of `sync.py` into the services function above
specifically so it's unit-testable without an HTTP call or the configured database —
`sync.py` now has a (non-circular) `import services`. Tests appended to
`tests/test_absence_ownership.py` (17 new cases: `record_absentee_minutes` behaviour,
the min-stay gate, the uncapped-international case, and `reconcile_absences` — which had
no test at all before this). Every mutation named in the build prompt bites, plus two
more found while writing the tests (the closed-entry guard, the IL-only eligibility
gate).

**Priority:** after 6b, before the AFCON window (Dec/Jan) — that is when several
absences per manager become likely and a parked returnee is easiest to miss.
**Recommended model:** Sonnet 5 — this is now a small, well-specified change. It was an
Opus item when it also carried slot succession; that half was withdrawn.
**Design doc:** `docs/DESIGN_IL_OWNERSHIP.md` §6 (and §5 for what was cut).
**Depends on:** Item 6b, loosely — the alert reads absence rows, which 6b does not
change, so it can be built independently if 6b slips.
**Decisions:** rule 6 in §2 of the design doc. **Alert-only enforcement, confirmed.**
Enforcement cannot be technical — the action happens in the FPL app, so there is no
request of ours to block. Fines stay on the existing manual ledger
(`services.add_fine`, `/admin/standings`); do NOT wire an automatic fine.

**Plan.** Persist the absent player's minutes — already fetched and discarded in
`sync_gameweek_points` — and flag when he has played but is still off the manager's
roster, suppressed for the IL until the minimum stay has elapsed.

**Session prompt:**

```text
Read CLAUDE.md in full, then docs/DESIGN_IL_OWNERSHIP.md §6. Do not re-open the league
rules in §2. Note that §5 records a withdrawn rule (slot succession) — do NOT build it.

THE RULE. A player back from injury or international duty must be re-added to the
manager's roster immediately. The site alerts when he isn't: the absent player has
logged MINUTES for his club and is still off the manager's roster. For the IL this is
suppressed until il_return_eligible_gw has passed — the 4-GW minimum stay holds even if
he recovers sooner. The international list has no minimum stay, so it alerts as soon as
he plays.

THE DATA IS ALREADY THERE AND THROWN AWAY. sync_gameweek_points fetches
/event/{gw}/live, whose elements map carries minutes for EVERY player in the game, but
it only persists minutes for the players in each manager's picks. An absent player is on
nobody's roster, so his minutes are discarded. Read them out of the payload already in
hand — no new HTTP call — and persist the fact on the absence row. `last_played_gw` is
the smallest thing that works and is the only new stored field in this design.

DO NOT widen GameweekPoints.player_points to include absent players.
rules.zero_minute_count iterates that list and it must keep meaning "FPL's lineup", not
"our notion of the squad" — widening it would silently change the anti-tanking rule.
That rule is load-bearing and its excusal logic is subtle; leave it alone.

SURFACING. Use the channels that already exist: flagged_actions (already the "you owe
the league an action" nag on the homepage) and a data_health check. Name the manager,
the player, and how many gameweeks he has been playing while parked. Do NOT create an
automatic fine — nothing else in the app converts a flag into money except last place,
and an automatic one would misfire on sync timing. The commissioner fines by hand via
services.add_fine on /admin/standings if they want to.

Both lists are covered, and the international list may have SEVERAL active entries for
one manager (it is uncapped as of 6b) — so this is a per-entry alert, not per-manager.

TESTS. Fires on minutes with the player still off-roster; suppressed for an IL inside
the minimum stay and fires once past it; fires for an international absence immediately;
clears when the player is re-added; several simultaneous international absences each
alert independently. Also pin that reconcile_absences still auto-closes an entry on
re-add — there is no test for that function at all today.

ACCEPTANCE: the full DB-backed regression, green with 0 skipped (command in the header
of this file); "310 skipped" is a FAIL. Mutation-test the min-stay suppression. Update
docs/BACKLOG.md and CLAUDE.md. Do not commit; end with a summary.
```

---

## Item 7 — Keeper years survive a drop (the clock belongs to the player) — **DONE 2026-08-24**

**Outcome:** built, uncommitted for review. **The scope collapsed on the day**: the
commissioner confirmed the rule is MID-SEASON only — at the rollover anyone not kept
resets, which `advance_season` already does. So build items 1 and 2 of the prompt below
(the `keeper_clock_carryover` table and the rollover carry) were **not built**, and there
is no migration and no backfill. Item 3's mechanism changed from a stored ledger to a
recursive lookup of the previous holder — which also covers a case the ledger would have
missed, a prior holder who arrived by trade and so had no seed of his own.

Items 4 (drafted vs. preseason FA) and 5-7 were built as written. `rules.keeper_status`
needed no change, as predicted. Two things the prompt didn't anticipate, both found by
reading production: three real 26/27 main picks carry only a free-text `player_label`
(so a `player_id`-keyed drafted-set would have docked them a year), and the
"has recorded picks" guard is better applied **per manager** than per season — an
unresolvable label pick removes only its own manager from the trusted set.

**None of the six pinned tests needed rewriting.** They pin `rules.keeper_status`, which
is untouched; `test_keeper_override.py:96` (per-manager seed keying) and
`test_trade_overlay.py:386` (`seeds == []` after a rollover) both pass unedited, the
latter being a useful signal the no-table scope is right. New coverage lives in
`tests/test_keeper_clock_follows_player.py` (17 cases).

**The drafted distinction must refine ONLY the no-seed case** — a kept player holds a GW1
slot with no `DraftPick` row, so applying it to every GW1 player reclassified 60 of 150
live roster players from `draft` to `waiver`. Caught by the read-only production diff,
not by the suite. Keep that check in the recipe for anything touching keeper derivation:
derive against prod before and after, and diff.


**Priority:** season runway — next matters for summer 2027 keeper selections;
`set_keeper_override` covers any known case in the interim. Run after Items 4a and
5a (both make `DraftPick` consultable where this needs it).
**Recommended model:** Opus 5 — schema + derivation + `advance_season` + 7 pinned
tests deliberately flipped.
**Backlog entry:** "Keeper years must survive a drop — the clock belongs to the
player, not the owner".
**Decisions:** rule decided 2026-08-15 (carried clock capped at the waiver 3, label
stays `waiver`); two open rule questions closed 2026-08-18: **the clock is FROZEN
while unowned** (it only ticks for seasons the player was actually kept), and
**preseason FA carries the clock** — only being DRAFTED resets to 4, so "on the GW1
roster" is no longer a valid proxy for "drafted" and the derivation must consult
real `DraftPick` rows.

**Session prompt:**

```text
Read CLAUDE.md in full (keeper section especially), then the docs/BACKLOG.md entry
"Keeper years must survive a drop — the clock belongs to the player, not the
owner". You are implementing that rule change with three decisions settled below.
This deliberately flips pinned behavior — the entry lists the tests that must be
REWRITTEN, not appeased.

THE RULE: keeper years persist when a player is dropped and reacquired via
waivers/FA — including by a DIFFERENT manager, and including preseason FA after the
draft. Only being selected in a draft (main or discovery) resets the clock to 4.
The carried clock is capped at KEEPER_FRESH_WAIVER (3) on any waiver/FA
acquisition, and the label stays "waiver" (it still eats a waiver keeper slot).
DECIDED 2026-08-18: the clock is FROZEN while unowned — a player dropped with 2
years who sits unclaimed for a full season still arrives with 2 (then capped at 3,
which is a no-op here); it does NOT tick down across unowned seasons.

READ BEFORE WRITING: the backlog entry's structural analysis (KeeperSeed keying,
_derive_keeper_status's two manager-scoped carry sources, advance_season's
kept-players-only iteration); services.py _derive_keeper_status (the _status_for
resolver and its keeper_status call), advance_season, set_keeper_override;
rules.py keeper_status (its dropped/clean-FA branch already honours a non-None
prev and applies min(prev, fresh_waiver) — the whole job is feeding it the right
prev; it likely needs NO change); models.py KeeperSeed (per-manager unique key is
deliberate and PINNED by tests/test_keeper_override.py:95 — do not weaken it);
DraftPick. Items 4a/5a in this file, which made DraftPick rows consultable
(discovery links; migrated main-draft rows on the current row).

BUILD:

1. Schema: a new player-level ledger table (do NOT make KeeperSeed.manager_id
   nullable — the per-manager seed key is pinned and semantically different), e.g.
   keeper_clock_carryover: league_id FK, player_id FK, years_remaining int,
   source_note str, created_at; unique (league_id, player_id). One row means "if
   this player is acquired via waivers/FA this season, his clock arrives as
   years_remaining (then capped at 3)". Alembic migration.

2. advance_season: in addition to the existing kept-player seed carry (years−1,
   UNCHANGED), persist carryover rows on the NEW league for (a) every player whose
   derived clock on the outgoing season is known and who was NOT kept — at his
   remaining value, NOT decremented (frozen while unowned), and (b) every unclaimed
   carryover from the outgoing league, copied forward unchanged. A player who then
   gets DRAFTED ignores his carryover (the draft resets); one claimed on waivers
   consumes it.

3. _derive_keeper_status: on the waiver/FA path (dropped, or never-rostered-then-
   claimed), fall back from seed_remaining[(mid, pid)] to the carryover ledger for
   prev — rules.keeper_status's existing min(prev, fresh_waiver) does the cap.

4. The drafted-vs-preseason-FA distinction: today "on the GW1 roster with no seed"
   ⇒ ("draft", 4). Under the new rule that proxy over-grants: a preseason FA pickup
   also lands on GW1. Consult DraftPick (main draft, that season, that manager —
   bridge manager identity by fpl_manager_id, query by season_year): drafted ⇒
   ("draft", 4) exactly as today; on GW1 but NOT drafted ⇒ the waiver/FA path with
   the carryover fallback. GUARD: apply this distinction ONLY for seasons that have
   recorded main-draft picks (2026 onward) — historical seasons predate recorded
   picks and must keep the GW1 proxy, or every historical keeper regresses to
   "waiver". Pin that guard with a test.

5. Unchanged, verify with tests rather than touching: trades still chain the label
   and clock through keeper_status(traded_from=...); discovery resets like a draft
   (Item 4a's "discovery" label is draft-clocked); set_keeper_override still wins
   over everything including the carryover.

6. Tests: the entry lists the pins to REWRITE for the new rule —
   tests/test_rules.py:314/325/334/342, tests/test_keeper_override.py:31 (95 stays
   — per-manager seed keying is untouched), tests/test_trade_overlay.py:252/386.
   New coverage: drop → same-season reclaim by another manager carries min(prev,3);
   drop with an exhausted clock (0) → the claimant CANNOT keep him; unowned across
   a season boundary → frozen value arrives via advance_season's carryover;
   drafted after a drop → clean 4; preseason FA after the draft → carries, not 4;
   the historical-season guard.

7. Docs: docs/requirements.md:32-33 and :42 ("dropped players lose keeper
   eligibility" — now false as written), CLAUDE.md's keeper section, and the
   backlog entry (done).

ACCEPTANCE: full suite green with 0 skipped (command in the header of this file).
Do not commit; end with a summary, explicitly listing every pinned test you
rewrote and why.
```

---

## Item 8 — Verify `goalie_team_owner` self-healed at the rollover (verify-and-close)

**Recommended model:** Haiku 4.5. No code expected.
**Backlog entry:** "`goalie_team_owner` reads the wrong season before a rollover".

**Session prompt:**

```text
Read the docs/BACKLOG.md entry "goalie_team_owner reads the wrong season before a
rollover". The rollover has since run (2026-08-17): the current league row's
season_year is 2026, matching the DraftPick.season_year on the goalie-team picks —
so the skew described should have self-healed exactly as the entry predicts. VERIFY,
do not fix: read services.goalie_team_owner and _goalie_team_history, then write a
small DB-backed test (existing style) proving a club drafted with season_year == the
current league's season_year resolves to its owner. If Item 5a's migration prompt in
docs/SESSION_PLANS.md has already run, also confirm nothing there re-broke it. If
verified, mark the backlog entry resolved (self-healed, test added); if NOT verified,
STOP and report what you found instead — do not attempt the fix. Run the full suite
(0 skipped, command in this file's header). Do not commit.
```

---

## Item 9 — IL backfill form: search by player name, not FPL id

**Recommended model:** Sonnet 5.
**Backlog entry:** "IL backfill form must search by player name, not FPL id".

**Session prompt:**

```text
Read CLAUDE.md, then the docs/BACKLOG.md entry "IL backfill form must search by
player name, not FPL id" — it contains the full design; implement it as written.
The essentials: templates/admin_keepers.html's backfill form takes two bare numeric
FPL-id inputs nobody can use. Reuse the existing datalist name-picker pattern
(templates/my_team.html:62-86's ilResolve + services.list_players) with two pickers
on one page (distinct datalist ids, shared parameterized resolver). KEY THE PICKER
ON players.code, NOT fpl_id — this form exists for HISTORICAL placements, and
departed players have fpl_id NULL (which _resolve_player refuses). Work needed, per
the entry: list_players (or a sibling) emits code; a _resolve_player_by_code beside
_resolve_player; the route resolves code -> players.id before calling place_on_il;
ui.py's admin_keepers render passes the player list (it doesn't today); the backfill
route's form fields change, and tests/test_il_keeper_visibility.py:92-140 posts the
old numeric fields — update it alongside. Note: a separate design session (Item 6)
may later change IL backfill SEMANTICS; this item is only the form's input method
and stays valid regardless. Update the backlog entry. Full suite, 0 skipped
(command in this file's header). Do not commit.
```

---

## Item 10 — Make the test-DB skip loud

**Recommended model:** Haiku 4.5.
**Backlog entry:** the "Worth doing" note under "Running a full regression before
the draft", and P3 item 12 in the old ordering.

**Session prompt:**

```text
Read the docs/BACKLOG.md section "Running a full regression before the draft" —
both documented traps. Then tests/conftest.py. BUILD: a session-scoped pytest check
that FAILS the run loudly when TEST_DATABASE_URL is unset, unless an explicit
opt-out is passed (an env var like ALLOW_DB_SKIP=1 or a --no-db pytest option —
pick whichever is cleaner in this conftest), so "243 passed, 310 skipped, exit 0"
can never again read as green. The opt-out must exist: pure-rules tests are
legitimately runnable without a DB. Also make the alembic-not-on-PATH failure mode
produce ONE clear error naming the fix (activate the venv / PATH prefix) instead of
310 identical errors. Update the backlog note and the Tests bullet in CLAUDE.md's
Commands section to mention the opt-out. Verify all three modes by hand: no env ->
loud fail; opt-out -> pure tests pass with skips; full env -> everything runs, 0
skipped (command in this file's header). Do not commit.
```

---

## Item 11 — preflight: detect a rollover for real, cross-check the season year

**Recommended model:** Sonnet 5.
**Backlog entry:** "preflight's 'rollover NOT done' check cannot detect a rollover".

**Session prompt:**

```text
Read the docs/BACKLOG.md entry "preflight's 'rollover NOT done' check cannot detect
a rollover", then scripts/preflight_draft.py in full. Two fixes, per the entry:
(1) the rollover check's predicate (is_current and phase != 'draft') passes cleanly
on an already-rolled-over league — re-predicate or rename it to test what it
claims; (2) upcoming_season is derived as league.season_year + 1 with no
cross-check — compare league.season_year against the calendar-derived season
(sync._season_start_year) and FAIL LOUDLY on a mismatch, so a stale season_year
can't make every downstream check self-consistently wrong. If the shared
phase-dependent draft-year helper exists (built by Items 1/5a in this file), use it
for upcoming_season instead of the raw +1. Also lift the goalie-team observation
from the related backlog entry ("goalie_team_mode is per-season..."): preflight
must ASSERT the expected mode explicitly rather than branching on it, and surface
picks-per-manager as its own check. Add/extend tests if preflight has any; at
minimum run the script against the dev DB in both a healthy and a
deliberately-stale-season_year state and show both outputs. Update both backlog
entries. Full suite, 0 skipped. Do not commit.
```

---

## Item 12 — Accent-insensitive matching in the `<datalist>` pickers

**Recommended model:** Haiku 4.5.
**Backlog entry:** "Accent-insensitive matching in the `<datalist>` pickers".

**Session prompt:**

```text
Read the docs/BACKLOG.md entry "Accent-insensitive matching in the <datalist>
pickers" — implement its FIRST option (the ASCII-folded alias), not the HTMX
typeahead rewrite. The browser matches datalist options against literal text, so
"Sesko" finds nothing even though server-side search was fixed. BUILD: fold each
player name server-side using the same lowercase-first -> _TRANSLIT -> NFKD order
as scripts/import_projections.py's _norm (port the logic; do NOT import from a
script, and do NOT touch history_import._norm), emit the folded form in the option
text alongside the real label so the browser matches either spelling, and keep the
existing name -> hidden-id resolver working for both forms. Affected forms: the IL
place/return pickers (templates/my_team.html) and the trade-player form
(templates/draft.html) — and Item 9's backfill pickers if that has landed. Test:
list_players (or the emitting function) produces the folded alias for Šeško/
Ødegaard/Kadıoğlu-class names. Update the backlog entry. Full suite, 0 skipped.
Do not commit.
```

---

## Item 13 — `/teams` cards: uniform height, kept-only by default

**Recommended model:** Sonnet 5.
**Backlog entry:** "`/teams` grid renders uneven card heights".
**Decision (2026-08-18):** kept-only default with a per-card expand, plus the
`align-items: start` CSS fix. (The Kevin S/Steve row-count outliers were checked in
the backlog entry and are ordinary roster churn, not the IL duplicate-pair bug.)

**Session prompt:**

```text
Read the docs/BACKLOG.md entry "/teams grid renders uneven card heights". BUILD
both fixes, decided 2026-08-18: (1) align-items: start on .teamgrid
(templates/base.html); (2) each roster card shows ONLY kept players by default —
uniform ~5 rows — with the full list behind a native <details>/<summary> expand
(no JS; the app already styles details/summary). Respect keeper privacy: "kept"
visibility on this page already flows from the services layer
(rules.keepers_revealed) — do not add any new disclosure; when selections aren't
revealed to the viewer, the card falls back to showing the full list as today. If
Item 1's in-progress view is live, apply the same pattern there (keepers up top,
drafted players behind the expand). Template + CSS change only — no service
changes expected. Add a template-level or route-level test if the existing suite
has that idiom; otherwise verify by hand in the dev server and say so. Update the
backlog entry. Full suite, 0 skipped. Do not commit.
```

---

## Item 14 — Scheduled keeper lock and draft open

**Recommended model:** Sonnet 5.
**Backlog entry:** "Schedule keeper lock and draft open — fire automatically at a
set time".
**Decision (2026-08-18):** admin enters times in **US Eastern**; the server converts
via stdlib `zoneinfo` (`America/New_York`, DST-correct) and stores UTC.

**Session prompt:**

```text
Read CLAUDE.md (phase lifecycle section), then the docs/BACKLOG.md entry "Schedule
keeper lock and draft open" — it contains the full investigation; implement its fix
sketch with one decision settled: the admin enters times as US Eastern, the server
converts with zoneinfo("America/New_York") and stores UTC (columns are
DateTime(timezone=True)). BUILD, per the entry: (1) migration adding nullable
leagues.keepers_lock_at and leagues.draft_opens_at; (2) extend
advance_phase_if_due (or a sibling called at the same /admin/sync site): now >=
keepers_lock_at -> keepers_locked = True, and now >= draft_opens_at -> the SAME
path enter_draft_phase uses (keeper reveal + side effects stay in one place). Both
idempotent — clear the field once fired. Respect phase_manual exactly as existing
transitions do: a pinned phase is never overridden by a schedule, and the admin UI
must show when a pin would block the scheduled open. The manual "Start draft"
button is unchanged. (3) Admin UI on /admin/health: two datetime inputs labelled
US Eastern, rendering any stored value back in Eastern, with the cron-cadence
caveat VISIBLE next to them (no trigger 23:00-06:00 UTC -> a schedule landing
there fires at the 06:00 sweep, up to ~7h late — show the worst case). (4) Scope
guard from the entry: these two events only; do not generalize. Tests: pure
firing logic (due/not-due/idempotent/pinned), and a DST-boundary conversion case
(an Eastern time in March and one in November map to the right UTC). Update the
backlog entry and CLAUDE.md's phase-transitions paragraph. Full suite, 0 skipped.
Do not commit.
```

---

## Item 15 — Discord webhook: announce new trades

**Recommended model:** Sonnet 5.
**Backlog entry:** "Post a message to Discord when a trade is recorded".
**Decisions (2026-08-18):** announce **all new trades** — site-entered and
FPL-synced — exactly once each; commissioner edits and deletes stay **silent**; a
persisted per-trade marker prevents sync re-posts and backfill floods.

**Session prompt:**

```text
Read CLAUDE.md (architecture rule about live calls in request handlers;
SECURITY.md's secret-handling pattern), then the docs/BACKLOG.md entry "Post a
message to Discord when a trade is recorded" — its 'things to think about' list is
the requirements list. Decisions settled 2026-08-18: announce ALL new trades
(site-entered AND FPL-synced), once each; edits/deletes silent; webhook URL is an
env secret (DISCORD_WEBHOOK_URL; feature is OFF when unset — no config UI).

DESIGN (follow this shape, it answers the entry's open questions):
- Marker: a nullable announced_at column on trades (metadata on our own
  league-custom table; sync never writes it). The MIGRATION back-stamps every
  EXISTING trade so history can never flood the channel on first deploy.
- One mechanism, not two: services.announce_new_trades(db) sweeps trades with
  announced_at IS NULL, renders each through get_trades' existing human-readable
  shape (players, picks, clubs — do not re-derive), POSTs to the webhook, stamps
  announced_at per success. Call it fire-and-forget AFTER the trade write commits
  (the /trade route and the commissioner trade routes) and once from the post-sync
  hook in main.py after sync_trades — the same site flag_ineligible uses. A
  Discord outage or bad URL must NEVER fail or roll back a trade: catch
  everything, log, leave announced_at NULL for the next sweep. Do NOT call the
  webhook from inside record_audit (the entry flags this trap: audit is a
  write-path primitive) and do NOT put the HTTP call inside services' transaction.
- Timeout the POST aggressively (a few seconds) so a hung webhook can't stall a
  request or the sync.
Tests: sweep announces once and stamps; a failed POST leaves the row unstamped and
raises nothing; edits/deletes don't announce; the migration back-stamp; feature-off
(no env) is a no-op. Use a fake/monkeypatched HTTP layer — no real network in
tests. Document DISCORD_WEBHOOK_URL in SECURITY.md's env-var list. Update the
backlog entry. Full suite, 0 skipped. Do not commit.
```

---

## Item 16 — Historical GK IL backfill refused once goalie teams are on

**Recommended model:** Sonnet 5.
**Backlog entry:** "Historical goalkeeper IL backfill is refused once goalie teams
are on".

**Session prompt:**

```text
Read the docs/BACKLOG.md entry "Historical goalkeeper IL backfill is refused once
goalie teams are on", then services._refuse_goalkeeper_list_move and every caller.
The check gates on the CURRENT league's goalie_team_mode, so with goalie teams now
live it refuses a legitimate historical GK injury-list backfill from a
pre-goalie-team season where individual keeper ownership was the rule. FIX: the
check must consider the league row/season being EDITED, not today's current row —
thread the target league through (the backfill path already knows it). Behavior
matrix to pin with tests: current-season GK move with goalie teams on -> still
refused; historical backfill on a pre-goalie-team row -> allowed; historical row
that ALSO has goalie teams on -> refused. Update the backlog entry. Full suite,
0 skipped. Do not commit.
```

---

## Deferred — no session prompt

- **Item 5b** (provisional-row architecture): **retired 2026-08-20** (was parked for
  ~spring 2027); prompt kept above as a design brief, revisit-on-trigger only.
- **v2 in-app league / goalie teams** (`v2/in-app-league` branch): blocked on
  merging main's pl_teams/keeper/trade schema work before the scoring-source fix;
  revisit only when v2 is picked back up.
- **25/26 IL data cleanup** (G. Jesus / Trossard / Kudus rows): retired 2026-08-18
  as moot — frozen row, correct keepers carried forward. See the annotated backlog
  entries.
