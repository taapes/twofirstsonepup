# Backlog

Feature and bug items not yet scheduled. Each entry records what's wrong, why the
obvious fix doesn't work, and where the code lives — so picking one up doesn't mean
re-deriving the investigation.

Status values: `open` | `in progress` | `blocked`
Priority values: `P0` (before the 2026 draft) | `P1` (after it) | `P2` (after the
rollover) | `P3` (tooling/quality)

Full spec is `docs/requirements.md`; conventions are in `CLAUDE.md`. This file is only
for work that is *known but not done*.

---

## What to do next

Ordered 2026-08-15, the day before the draft. **The highest-priority work is operational,
not code** — only item 0 involves running anything against the repo.

### P0 — before the draft

Worked **one at a time**: clarify → plan → do → confirm, before moving to the next.

**1. Apply the goalie-team migrations to prod and set `goalie_team_mode` on the 25/26
row.** Confirmed: goalie teams are live for this draft (14 picks + a club). First because
it fails *silently* — unset, the board sizes to 15 instead of 14, no club can be drafted,
and three preflight checks skip. Set it on the **25/26** row; the draft runs on the
outgoing season.

**2. Backfill the two IL placements** — Šeško/Scott (`start_gw=37`, replacement Gabriel
Jesus, per commit `4cf5d40`) and Kudus/Kevin T (replacement Trossard), via
`POST /admin/keepers/il-backfill`. **This blocks item 3 for those two managers:** an IL'd
player only becomes a keeper candidate once the IL row exists, so Scott and Kevin T
cannot submit a correct keeper list until this is done.
*Sequencing hazard:* do any **goalkeeper** IL backfill **before** item 1 — switching
goalie teams on makes historical GK backfills impossible. Neither of these two is a GK.

**3. Chase the six outstanding keeper submissions.** A manager with no submission gets a
full un-reduced board, which shifts *every other manager's* pick positions, not just
their own.

**4. Run `scripts/preflight_draft.py`** — data readiness. Read two lines: the
`(checking the 2026 draft)` print, and check 6's detail, which must describe a **14**-round
board. Preflight goes red once the phase is `draft`, so there is no verify-while-live mode.

**5. Full regression, green with ZERO skips, on the exact code being drafted with — the
LAST thing before starting the draft.** The code-readiness gate, deliberately last so it
runs against the finished state rather than a moving one. The 553-pass run on 2026-08-15
does **not** discharge it: it ran against a working tree carrying another session's
in-flight `rules.py` / `services.py` edits, so it tested code that isn't what will be
deployed. Command and acceptance criteria: see
[Running a full regression](#running-a-full-regression-before-the-draft) below —
**`0 skipped` is part of passing**, and both failure modes are silent.

### P1 — after the draft
5. Release overlay (G. Jesus, Trossard) — one mechanism, both cases.
6. `goalie_team_owner` season skew — self-heals at the rollover.

### P2 — after the rollover
7. Post-GW38 season alignment (+ migrate the 2026-stamped rows onto the 26/27 row).
8. Keeper years survive a drop.
9. Discovery picks get a 3-year clock instead of 4.

### P3 — tooling and quality
10. IL backfill name search — would have made item 2 painless.
11. preflight's "rollover NOT done" check can't detect a rollover.
12. Make the test skip loud. *(P3 only because item 0 pins the workaround by hand; with
    this guard, item 0 becomes a one-command check.)*
13. Historical GK IL backfill refusal — promote if a goalkeeper case appears.
14. v2 in-app league — `blocked`.

---

## State as of 2026-08-15 09:54

From `snapshots/pre-draft-20260815.json`. **Stale by design** — it predates that day's
goalie-team commits, so treat every line as *verify*, not *assume*.

| Observed | Reading |
|---|---|
| `goalie_team_mode` key **absent** from the league row | The column did not exist in that DB yet — the four goalie-team migrations had not reached it |
| `injury_list: 0` | The Šeško backfill promised in `4cf5d40` was never done; Kudus likewise |
| `keeper_selections: 20`, from only **4** managers | John, Kevin F, Kevin S, Kevin T, Mark and Scott had submitted nothing |
| `draft_lottery: 10`, `draft_picks: 0`, `phase=offseason`, `keepers_locked=False` | Draft order set; draft not started |

---

## Bugs

### Keeper years must survive a drop — the clock belongs to the player, not the owner

**Priority:** `P2` — after the rollover; a full season of runway.
**Status:** `open` — rule change, decided 2026-08-15. **No known player is affected for
the 2026 draft**, so this is not draft-blocking.

**The rule.** Keeper years hold even when a player is dropped and picked back up on
waivers/FA — including by a *different* manager. If Scott dropped Haaland at the end of
last year and someone else picked him up, the clock does **not** reset: if it was
exhausted, the new owner cannot keep him.

**This applies to waivers/FA only.** A player who goes back into the draft pool and is
**drafted** starts fresh at 4 years, exactly as today. Going through the draft is what
resets a clock; slipping through waivers is not.

Two decisions taken with the rule:
- **The carried clock is still capped at the waiver fresh cap** (`KEEPER_FRESH_WAIVER`,
  3). A player with 4 years left who goes through waivers arrives with 3 — a waiver
  acquisition is still worth one year less than a draft one.
- **He is still labelled `waiver`**, so he continues to eat one of the manager's two
  waiver keeper slots. Only the *clock* becomes a property of the player; the label keeps
  its current meaning.

**Why this isn't a small change: the clock is per `(manager, player)` today, and a
clock with no owner cannot be stored.**

- `KeeperSeed` is keyed `(manager_id, player_id)` with `manager_id` **NOT NULL**
  (`models.py`, `uq_keeper_seed_mgr_player`). Per-manager keying was itself a deliberate
  fix — keying on player alone let two managers' seeds collide — and
  `tests/test_keeper_override.py:95`
  (`test_seeds_are_keyed_per_manager_not_per_player`) pins it.
- `_derive_keeper_status` builds `carried` from exactly two manager-scoped sources: this
  manager's own seed row, and a recursive lookup of the sender's clock via `Trade` rows.
  **There is no "previous owner" edge for a waiver pickup** — once a player is off the
  roster, nothing records that anyone used to own him.
- `advance_season` only iterates `KeeperSelection` rows, i.e. players actually *kept*. A
  dropped player has no selection, so no seed is written on the new league and his clock
  ceases to exist. The existing stance is explicitly *lose the clock rather than invent
  one*.

**Scope — what this rule does NOT change.** A player who isn't kept goes back into the
draft pool, and **being drafted is a fresh acquisition: 4 years.** That is expected and
must stay. The rule is narrowly about the **waivers/FA** path — dropped and claimed
*without* passing through a draft.

Today's logic already draws that line correctly, via the presence of a seed:
`started_with_manager` (on the GW1 roster) with **no seed** means drafted → `("draft",
4)`; with a seed means kept → the carried remaining. Nothing here should be touched.

**Today's behaviour on the path that IS wrong.** Manager A holds a player with 1 year
left and drops him; B claims him on waivers → `carried` is `None` and there is no trade
edge, so `rules.keeper_status(False, False, False, None)` returns **`("waiver", 3)`**. B
gets a full waiver clock and the player is keeper-eligible, when A's remaining year
(here 1, or 0 for an exhausted player) should have followed him.

**Fix sketch.**
1. Make a clock storable without an owner — either `KeeperSeed.manager_id` nullable with
   a `(league_id, player_id)` unique key, or a separate player-level ledger. This is the
   real work.
2. `advance_season` must persist clocks for players who were *not* kept, plus a decision
   on whether an unowned year still ticks down.
3. `_derive_keeper_status`: fall back from `seed_remaining.get((mid, pid))` to a
   player-level lookup — but **only on the waiver/FA path**, so a drafted player still
   resets to 4.
4. `rules.keeper_status` likely needs no change at all: its `dropped` /
   clean-FA-pickup branch already honours a non-None `prev` and already applies
   `min(prev, fresh_waiver)`, which is the agreed cap. Feeding it the right `prev` is the
   whole job.

**One edge case to decide while implementing.** "On the GW1 roster with no seed" is a
*proxy* for "drafted" — `_derive_keeper_status` never consults `draft_picks`. A player
picked up in preseason free agency after the draft also lands on the GW1 roster with no
seed, and so would reset to 4 under the same branch. Whether that should instead carry
the clock is a genuine question, and closing it means joining to `draft_picks` — the same
missing link behind the discovery-pick entry below.

**Interim tool, no code needed:** `set_keeper_override` accepts `years_remaining` 0..4
per `(manager, player)`, so a known case can be corrected from `/admin/keepers` and is
written to the audit log.

**Tests that pin the current reset-on-drop behaviour** and would need revisiting:
`tests/test_rules.py:314/325/334/342`, `tests/test_keeper_override.py:31/95`,
`tests/test_trade_overlay.py:252/386`. **Docs to update:** `docs/requirements.md:32-33`
and `:42` ("dropped players lose keeper eligibility"), and the keeper section of
`CLAUDE.md`.

---

### Kevin's Kudus: restore the IL keeper and release the replacement

**Priority:** **`P0` for the IL backfill** (it blocks Kevin T's keeper submission),
`P1` for releasing Trossard.
**Status:** `open`

**What's needed.** Kevin had Kudus on the injury list at the end of 25/26, so Kudus
should be a **draft keeper** on his squad. Trossard — the replacement who took the roster
slot — should be removed and sent to free agency with no owner, treated as a GW38 drop.

**This is the same shape as the Šeško case** already handled in commit `4cf5d40`, and the
same shape as the G. Jesus item below. The pattern is now clear and worth naming: when a
manager finishes the season with someone on the IL, **two** corrections are needed, not
one —

1. **Backfill the IL placement** so the injured player becomes visible as a keeper
   candidate. `_derive_keeper_status`'s `final_candidates` is the *union* of final-GW
   roster presence and IL coverage, so the IL row is what makes Kudus appear at all. Use
   the admin backfill at `/admin/keepers` (`POST /admin/keepers/il-backfill`) — note it
   currently demands raw FPL ids, which is the "IL backfill form" item under Features.
2. **Release the replacement.** FPL never knew about our IL, so the final GW38 snapshot
   contains Trossard, not Kudus. After step 1 the union yields **both**, leaving Kevin
   with 16 candidates for a 15-man squad. Releasing Trossard is what rebalances it.

Step 2 has no mechanism today — see the G. Jesus entry below for why (the ownership
overlay can reassign but cannot un-own) and for the proposed release overlay. **G. Jesus
is itself the Šeško replacement**, so one release mechanism settles both cases.

---

### `goalie_team_owner` reads the wrong season before a rollover

**Priority:** `P1` — self-heals at the rollover; fix only if club trades or the team page
are needed in between.
**Status:** `open` — affects the 2026 draft

**Symptom.** A goalie team drafted before the season rollover reads as owned by
**nobody**.

**Root cause.** `services.goalie_team_owner` (`services.py:3204-3208`) builds ownership
as:

```python
    cur = league.season_year or 0
    owner = {
        tid: fpl for (sy, tid), (fpl, _how) in _goalie_team_history(db).items()
        if sy == cur
    }
```

But `_goalie_team_history` (`services.py:3137-3150`) keys on the **`DraftPick`'s own
`season_year`** — 2026 for the upcoming draft — while `league.season_year` is 2025 until
`advance_season` runs. The two never match, so every club drafted pre-rollover is
filtered out.

**Knock-on.** `trade_goalie_team` refuses with "X doesn't hold Y"; the club block on the
manager team page and the club-swap path both render nothing.

**Not affected.** The in-draft rules are fine — `_team_unavailable_reason` and
`_goalie_team_required_reason` are scoped by the `season_year` passed in, so "one club
per manager" and "one club to one manager" hold correctly *during* the draft.

**Why tests miss it.** `tests/test_goalie_team_periphery.py:92` seeds
`DraftPick.season_year == league.season_year`, so the skew can't appear.

**Fix sketch.** Resolve club ownership against the same "upcoming season" the draft uses
rather than `league.season_year`. Closely related to the season-alignment item under
Features — fixing that properly makes this disappear.

---

### `goalie_team_mode` is per-season and must be set on the row being drafted

**Priority:** `P0` — highest of the data items; fails silently.
**Status:** `open`

**Symptom.** The goalie-team rule silently does nothing, and the board is the wrong size.

**Root cause.** `goalie_team_mode` lives on the **league row** and defaults to `'off'`
(`models.py:98-99`); `record_pick` reads it off the current league. Because a draft runs
on the *outgoing* league row (see the season-alignment item), the mode has to be set on
**that** row — not on the new season's row, which doesn't exist yet at draft time.

**If it's left `off`:** the board sizes to 15 picks per manager instead of 14
(`rules.draft_picks_per_manager`), no club can be drafted, and the three goalie-specific
checks in `scripts/preflight_draft.py` are **silently skipped**, since they sit behind
`if goalie_teams_on(...)`. Nothing asserts the mode is the intended value, so every
surface agrees with every other surface and the run looks clean.

**Fix sketch.** Have preflight assert the expected mode explicitly rather than branching
on it, and surface the picks-per-manager number as its own check instead of burying it in
another check's detail string.

---

### preflight's "rollover NOT done" check cannot detect a rollover

**Priority:** `P3`
**Status:** `open`

**Root cause.** `scripts/preflight_draft.py:53-58`:

```python
        check(
            "rollover NOT done (still on the pre-draft season)",
            league.is_current and league.phase != "draft",
            ...
        )
```

The name promises a rollover detector; the predicate is `is_current and phase != draft`.
After `advance_season` the **new** row is precisely the one that is `is_current=True`
with `phase='preseason'` — so this **passes cleanly on an already-rolled-over league**.
It can only fail on a non-current row, or once the draft has already started.

**The compounding part.** `upcoming_season` is derived as `league.season_year + 1`
(`preflight_draft.py:63`) with no cross-check against the calendar or
`sync._season_start_year`. If `season_year` is stale, every downstream check examines the
wrong year *consistently* and the script reports all green — the data is self-consistently
wrong. The only signal is the informational `print` at `preflight_draft.py:64`.

**Also worth knowing.** Preflight and a live draft are mutually exclusive: once
`enter_draft_phase` sets `phase='draft'` this check fails, so there is no "verify while
live" mode. Run it *before* starting the draft.

**Fix sketch.** Compare `league.season_year` against the calendar-derived season
(`sync._season_start_year`) and fail loudly on a mismatch; rename or re-predicate the
rollover check to test what it claims.

---

### G. Jesus should be a free agent, not on Scott's roster

**Priority:** `P1` — cosmetically wrong but mechanically harmless for the draft, which
reads keeper *selections*, not candidate counts.
**Status:** `open`

**Symptom.** G. Jesus shows on Scott's squad and shouldn't. The desired end state is
simply that he becomes a **free agent** — treat him as dropped after GW38.

**Why (found later, while working the Kudus entry above).** This is not arbitrary: G.
Jesus was the **IL replacement for Šeško** (see commit `4cf5d40`, which backfilled that
placement with `start_gw=37, replacement=Gabriel Jesus`). Once Šeško is restored as a
keeper candidate, Scott holds 16 for a 15-man squad, and the replacement is the one who
leaves. So this and the Kudus/Trossard entry are **one pattern with one fix**, not two
one-off data edits.

**Root cause — there is no roster-removal path in the codebase at all.** This is the
whole difficulty; it is not a one-line data edit:

- `rosters` is written **only** by `sync.sync_rosters` (`sync.py:690-699`, an `_upsert`
  keyed on `(manager_id, player_id, gameweek_id)`). Nothing in `services.py`,
  `admin.py`, `ui.py`, `api.py` or `scripts/` ever constructs or deletes a `Roster` row.
  The two-truths rule forbids it: a fabricated row is indistinguishable from a synced
  one to `get_transactions`, anti-tanking and `reconcile_absences`.
- The commissioner overlay `services.player_ownership` (`services.py:4055`) is a
  **reassignment** map `{player_id: manager_id}`, folded in `_owner_maps`
  (`services.py:4091-4121`). `Trade.to_manager` is **non-nullable**
  (`models.py:313-315`) and both `record_trade` and `trade_player` require two distinct
  managers — so the overlay can move Jesus from Scott to someone else, but **cannot
  express "goes to nobody."**
- `return_from_il(..., via="waiver")` (`services.py:1583`) only sets IL status/`end_gw`;
  it never touches `rosters`. The real roster change is expected to arrive via sync.
- `models.Transaction` (with its add/drop `action`) is **dead code** — nothing
  constructs it. Add/drops are *derived* by diffing consecutive snapshots
  (`services.get_transactions`, `services.py:1683`), and a "drop" in keeper terms is a
  *gap in snapshot presence* (`_dropped`, `services.py:2870`), never a stored row.

**Fix sketch — a release overlay, the sibling of the trade overlay.** Add a small
league-custom table (`roster_releases`: `league_id`, `player_id`, `manager_id`,
`effective_gw`, plus a note for the audit log) and fold it into `_owner_maps` as a
*subtraction*, applied after the trade fold. Doing it there means it flows automatically
to `player_ownership`, `effective_owner` and `_effective_roster_pids`
(`services.py:4144-4159`) — i.e. to every roster read, including `/my-team` and
`get_rosters`. Never write or delete a `rosters` row.

Four consequences to handle beyond the roster view:

1. **Keeper clock.** Dropped players lose keeper eligibility, but `_dropped`
   (`services.py:2870`) infers drops from snapshot gaps and will not see a release — so
   the release must feed it, or Jesus remains a keeper candidate for 26/27.
2. **Draft availability.** He must read as available to `search_players`' taken-oracle
   so he can actually be drafted in 26/27.
3. **`/admin/health`.** The 15-man roster check deliberately reads *raw* `Roster`
   (`services.py:5401-5406`), not the overlay, because it validates the **sync** — its
   comment notes a legal player-for-pick trade already leaves managers at 14 and 16.
   By that reasoning a release should leave this check untouched (Scott stays 15) and
   the discrepancy should surface, if anywhere, as its own check alongside "site trades
   applied".
4. **Admin surface.** A "release a player" form, most naturally on `/admin/corrections`
   beside the existing trade and pick correction tools.

---

### Discovery-drafted players get a 3-year waiver clock instead of 4

**Priority:** `P2` — next bites at the September discovery draft, not this one.
**Status:** `open`

**Symptom.** A player taken in the September discovery draft should count as
draft-acquired for keeper purposes (4 years, `rules.KEEPER_FRESH_DRAFT`). He is instead
derived as waiver-acquired with 3 years.

**Root cause.** `_derive_keeper_status` (`services.py:2763-2975`) never queries
`draft_picks` at all — so there is no `draft_type` filter, because there is no query to
filter. In `rules.keeper_status` (`rules.py:400-450`) the `"draft"` label comes *solely*
from `started_with_manager`, i.e. presence on the **GW1 roster**. A discovery pick is
made in September, so he is not on the GW1 roster and has no `Trade` row, and falls
through to `rules.py:444` → `("waiver", min(prev, 3))`. Nothing distinguishes him from
an ordinary waiver claim.

**The near-miss that hides it.** `submit_keepers` (`services.py:3252-3276`) *does*
synthesize the right thing — `{"acquisition": "discovery", "years_remaining":
KEEPER_FRESH_DRAFT}` — but **only inside the `if not st:` branch**, i.e. only when the
player is *off-roster*. In the normal success case, where the discovery pick joined the
PL and is on the manager's roster (exactly when he matters), `status.get(player.id)`
hits and the derived `("waiver", 3)` wins. He stays exempt from the waiver *cap*
(`rules.py:492-495`), so the bug shows up only as a missing keeper year.

**Two structural obstacles.**

- Recorded discovery picks are **free text with `player_id=None`**
  (`record_discovery_pick`, `services.py:5078-5084`; documented at
  `models.py:533-536`), so there is no FK to join a discovery pick to a rostered player
  even if the derivation wanted one. Historical imports land in a different table again
  (`DiscoveryResult`, `models.py:845`).
- `"discovery"` is not in `rules.KEEPER_ACQUISITIONS` (`rules.py:397`), so
  `set_keeper_override` (`services.py:2641`) rejects it. The only workaround available
  today is overriding to `acquisition="draft"`, which does restore the 4-year clock.

**Fix sketch.** The smallest correct change is at selection time: in `submit_keepers`,
apply the discovery 4-year clock whenever `is_discovery`, rather than only in the
off-roster branch. Decide separately whether the derived *label* should become
`"discovery"` (which means adding it to `KEEPER_ACQUISITIONS`) or stay
`"draft"`-equivalent. Linking discovery picks to `players.id` is the larger, more
general alternative and would also fix the derivation itself.

**Test gap.** No test constructs a discovery pick and asserts on keeper status. Existing
discovery coverage is only the cap (`tests/test_rules.py:309-346`) and board
availability (`tests/test_draft_availability.py:140-165`).

---

### Historical goalkeeper IL backfill is refused once goalie teams are on

**Priority:** `P3` — but **it goes live the moment `goalie_team_mode` is set** (P0 item
1), so do any goalkeeper backfill before that. Promote if a GK case appears.
**Status:** `open`

`_refuse_goalkeeper_list_move` (`services.py:1510`) gates on the **current** league's
`goalie_team_mode`. Once goalie teams are enabled it will refuse a legitimate
*historical* goalkeeper injury-list backfill from a pre-goalie-team season, where
individual keeper ownership was the rule. The check should consider the season being
edited, not today's league row. Found while looking at the IL backfill form below.

---

## Features

### Post-GW38 activity should belong to the following season

**Priority:** `P2` — the largest item here; do it with the 2026-row migration.
**Status:** `open` — **deliberately deferred until after the 2026 draft** (decided
2026-08-15, the day before it)

**The intent.** A season ends at GW38. Everything after that — keepers, trades, the
draft, future picks — belongs to the *following* season and should be stored on that
season's league row.

**What happens today instead.** Those rows are written to the **outgoing** league row
with `season_year` set to the next year, and `league.season_year + 1` is threaded through
the app to compensate. That expression appears in five independent places:
`templates/base.html:108` (the draft nav link), `ui.py:367` (keepers), `ui.py:1099`
(draft-prep), `scripts/preflight_draft.py:63`, and the health check in `services.py`.

Concretely, for the 2026 draft: picks get `season_year=2026` (correct) and
`league_id` = the **25/26** row (the thing being objected to). Nothing carries
`DraftPick` rows across league rows, so they stay there — after the rollover, the 26/27
season has no draft history of its own.

**Rolling over first is not a workaround.** `effective_keeper_selections`
(`services.py:4311`) filters on `league_id=league.id`, so a freshly created 26/27 row
returns **zero** keeper selections: `get_draft_board` would hand every manager a full
un-reduced board and show kept players as available. `_reverse_standings_managers` would
likewise return `[]`. `scripts/preflight_draft.py:53` encodes "rollover NOT done" as a
**pass** condition — the draft is designed to run pre-rollover.

**Why this was deferred rather than done before the draft** — three reasons, heaviest
first:

1. **A hard external blocker.** A new season's row can only be created by syncing a real
   FPL draft league id (`ui.py` → `sync_league_and_managers`), and
   `leagues.fpl_league_id` is unique and non-nullable (`models.py:46`). At GW38 in May
   that league does not exist in FPL yet. Aligning activity to the new season from GW38
   onward therefore requires a **provisional league row** — a schema change plus a
   reconciliation step when the real id later appears. That is the real design work here.
2. **Breadth.** Every year-scoped read flips from `season_year + 1` to `season_year`, and
   every writer (keeper selections, future picks, draft lottery, order overrides,
   standing adjustments, trades) has to target the new row. `advance_season`'s keeper
   carry reads selections off the *old* row and would need rewriting.
3. **It would have been a live data migration on draft eve** — ~38 keeper selections, 50
   future picks, 10 lottery rows and 150 keeper seeds moving rows in production, hours
   before ten people draft.

**Why deferring costs nothing.** No information is lost: `season_year` already records
the correct answer on every affected row. The later migration is mechanical and testable
— move rows with `season_year == 2026` from the 25/26 league to the 26/27 row once it
exists — and doing it after the draft means migrating a finished dataset rather than one
being written to live.

**Re-confirmed on draft eve (2026-08-15), after being challenged.** The decisive reason
is not breadth, it is a single concrete breakage: `effective_keeper_selections` filters on
`league_id`, so a new row returns **zero** of the ~38 submitted keeper selections — full
un-reduced boards for everyone, kept players draftable, and `_reverse_standings_managers`
empty as well. Rolling over first therefore *also* means migrating ~38 keeper selections,
10 lottery rows, 50 future picks and 150 keeper seeds, then fixing the `season_year + 1`
derivations (which would otherwise compute **2027** and render an empty board), against
prod, hours before ten people draft. The asymmetry decides it: a bad deferral is fixable
on Sunday, a bad migration means the draft cannot run.

Two findings that make deferral safer than first assumed:
- `advance_season` reads keeper selections off the **old** row filtered on the *new*
  `season_year`, so the post-draft rollover carries keepers correctly with no extra work.
- `_goalie_team_history` queries `DraftPick` with **no league filter**, keying on
  `season_year` — so club ownership history survives the `league_id` mismatch intact.

**The one real cost of waiting:** between the draft and the migration, `/draft/2026` on
the new row renders empty and the 26/27 season has no draft history of its own.

**Related trap to fix at the same time.** `/admin/sync` calls `sync_all()` with no league
id, falling back to the `FPL_DRAFT_LEAGUE_ID` env. After a rollover that env still points
at the old, now-frozen league, and every sub-task takes the frozen-skip branch which sets
`log.ok = True` — so **the nightly cron reports green while syncing nothing**. Update the
env in Render immediately after any rollover.

---

### Accent-insensitive matching in the `<datalist>` pickers

**Priority:** `P3` — the live draft search is already fixed; these are admin/manager
forms, not the draft.
**Status:** `open`

**Symptom.** `search_players` now unaccents both sides, so typing "Sesko" finds Šeško on
the draft board. The `<datalist>` pickers do **not** benefit: they render every player as
an `<option>` and the *browser* does the matching, against the literal label. So "Sesko"
still finds nothing in the IL place/return forms (`templates/my_team.html`) and the
trade-player form (`templates/draft.html`).

**Why it can't reuse the same fix.** The server-side query isn't involved — matching is
client-side against `services.list_players`' `label` (`"Name · Team"`).

**Fix sketch.** Two options, both cheap:
- Emit an ASCII-folded alias in the option text so the browser matches either form —
  e.g. keep `value` as the real name and append the folded spelling, or render a
  `data-*` attribute and do the matching in the existing small resolver function.
- Or replace the datalist with the HTMX typeahead already used for the discovery keeper
  search (`templates/_discovery_search.html` + its route), which goes through
  `search_players` and therefore inherits the unaccent fix for free. Heavier, but it
  deletes a second matching mechanism rather than patching it.

Reuse `scripts/import_projections.py`'s `_norm` + `_TRANSLIT` for the folding — it
already handles ø/ı/ğ, which NFKD alone does not.

---

### IL backfill form must search by player name, not FPL id

**Priority:** `P3` — worth doing before the *next* backfill rather than during one.
**Status:** `open`

**Symptom.** "Backfill a historical injury-list placement"
(`templates/admin_keepers.html:14-40`) asks for `injured_fpl_id` and
`replacement_fpl_id` as **bare numeric text inputs**. No commissioner knows FPL element
ids, so the form is effectively unusable.

**Fix sketch.** Reuse the name-picker pattern that already exists rather than inventing
one. The self-service IL form (`templates/my_team.html:62-86`) renders a `<datalist>` of
`{fpl_id, label}` and resolves name → hidden id in an `onsubmit` handler (`ilResolve`);
`templates/draft.html:140-156` does the same. Both are fed by `services.list_players`
(`services.py:3574`), which returns `label` as `"Name · Team"` to disambiguate duplicate
names. Two pickers on one page need distinct datalist ids and a shared or parameterized
resolver. The richer HTMX typeahead (`templates/_discovery_search.html` + `ui.py:382`)
is the alternative, but it hardcodes `discoveryResults`/`setDiscovery`, so two boxes
can't reuse it as-is.

**Key the picker on `players.code`, not `fpl_id`.** `fpl_id` is only *this season's*
element id — nullable, and NULL for any player who has left the PL
(`models.py:148-153`) — and `_resolve_player` (`services.py:1347`) refuses NULL
outright. Since this form exists precisely to enter **historical** placements, an
`fpl_id`-keyed picker would silently fail to address exactly the players it was built
for. Work needed:

- Extend `services.list_players` (or add a sibling) to emit `code`.
- Add a `_resolve_player_by_code` beside `_resolve_player`.
- Give `place_on_il` a code-based entry point, or resolve to `players.id` in the route
  before calling it.

**Ripples.** `ui.py:1223-1234` renders `admin_keepers.html` from
`services.keeper_overrides_context` and does **not** pass a player list today — it must.
`ui.py:1277-1301` (`admin_il_backfill`) changes its form field names, and
`tests/test_il_keeper_visibility.py:92-140` posts the current numeric fields, so it
updates alongside.

---

## Running a full regression before the draft

**Priority:** `P0` item 5 — the code-readiness gate, and the **last** P0 step before the
draft starts, so it runs against the finished state rather than a moving one.
**Status:** `open`. A run on 2026-08-15 was green (**553 passed, 0 skipped, 0 failed**,
~2 min) but **does not discharge this**: it ran against a working tree carrying another
session's in-flight `rules.py` / `services.py` edits, so it tested code that isn't what
will be deployed. Re-run on the deploy candidate — clean tree, at the commit Render will
serve — after items 1–4 are done.

**Acceptance: `0 skipped` is part of passing**, and don't read the exit code through a
pipe.

**Scope: the whole suite, not a draft subset.** The draft surface is wider than the
filenames suggest. Direct: `test_draft_availability`, `test_draft_order`,
`test_pick_errors`, `test_goalie_team_draft/_keepers/_mode/_periphery`,
`test_draft_prep`, `test_draftprep_goalie_board/_shape/_model`,
`test_il_keeper_visibility`, `test_keeper_override`, `test_keeper_privacy`. Indirect but
load-bearing: `test_rules` (keeper clocks + slot generation), `test_trade_overlay` (pick
trades, clock carry), `test_pl_teams` (clubs), `test_departed_players` (search
availability), `test_phase`/`test_phase_advance` (`draft_available` gating),
`test_corrections` (deleting a pick mid-draft). At ~2 minutes there's no reason to
subset.

Also confirm the test DB is schema-current (the fixture builds it from migrations) — a
mismatch there is exactly how a missing goalie-team column would hide.

Recorded in detail because a naive `pytest` run is **actively misleading** — it exits 0
while testing barely half the suite. Two traps, both of which bit on the 08-15 run:

1. **`TEST_DATABASE_URL` unset → 310 tests skip silently.** A bare `pytest` reported
   "243 passed, 310 skipped" and exit code 0. Every DB-backed test — which is to say
   nearly all the draft, keeper and trade coverage — was skipped. CLAUDE.md's warning
   ("confirm a run says `passed`, not `skipped`") is the whole ballgame.
2. **`alembic` must be on `PATH`.** The `test_engine` fixture builds the schema by
   shelling out to `alembic` (not `Base.metadata.create_all`, because the partial unique
   index `uq_players_fpl_id_live` exists only in the migration). With the venv not
   activated this fails `FileNotFoundError: 'alembic'` and turns all 310 into **errors**,
   which a `| tail` pipeline will happily report as exit 0.

The command that actually runs everything:

```sh
docker run -d --name fpl-test-pg -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=fpltest -p 55432:5432 postgres:16   # once

PATH="$PWD/.venv/bin:$PATH" \
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/fpltest \
PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q
```

`PYTHONDONTWRITEBYTECODE=1` is house style for anything involving mutation testing — a
stale `__pycache__` otherwise reports "no bite" against a restored file.

**Worth doing:** make the skip loud rather than silent (a session-scoped check that fails
when `TEST_DATABASE_URL` is missing unless an explicit opt-out is passed), so a green
`pytest` can be trusted at a glance.

---

## Known / deferred

### v2 in-app league — goalie teams

**Priority:** `P3`
**Status:** `blocked`

The goalie-team rule is complete on `main`; the v2 work on `v2/in-app-league` is
deliberately deferred in favour of pre-draft items. The blocker: v2's scoring engine
builds its scoreable set from `gameweek_points.player_points`, which
`sync_gameweek_points` fills from `/entry/{fpl_manager_id}/event/{gw}` — i.e. only the
15 players FPL assigned that manager. A goalkeeper owned under the club rule but not
assigned by FPL therefore scores zero. This blocks *any* divergence between our ledger
and FPL's rosters, not just goalie teams.

The fix is cheap in effort — `/event/{gw}/live` is already fetched and carries every
player's stats — but `v2/in-app-league` now diverges substantially from `main`, so
restarting means merging the `pl_teams`, keeper and trade schema work first.
