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

Re-triaged **2026-08-18**, post-draft and post-rollover. The 2026-08-15 pre-draft P0s
are all discharged (draft ran 08-16, rollover ran 08-17). Every open item now has an
execution plan + a copy-paste session prompt with a recommended model in
**`docs/SESSION_PLANS.md`** — run them in file order:

1. ~~**Item 1** — in-progress squad view~~ — **done 2026-08-18** (`5a202be`)
2. ~~**Item 2** — trades/transactions/picks cross-season + Jan-31 attribution +
   filters~~ — **done 2026-08-18** (`5a202be`)
3. ~~**Item 3** — history page cross-season~~ — **done 2026-08-18** (`e986a13`)
4. **Item 4a/4b** — 4a (discovery pick links + the "discovery" keeper clock) is
   **done 2026-08-18** (`b94979a`); **4b** — sync-driven match suggestions + admin
   dashboard — still to do, before the September discovery draft — Opus 5
5. **Item 5a** — migrate the 2026 draft onto the 26/27 row (snapshot + Neon-branch
   rehearsal mandated) — Opus 5
6. **Item 6** — IL ownership design session (before the season's first IL case) —
   Opus 5
7. **Item 7** — keeper years survive a drop (rules decided: frozen while unowned;
   preseason FA carries; only a draft resets) — Opus 5
8. **Items 8–16** — small fixes and tooling, any idle session — Haiku/Sonnet

**Next up: Item 4b.** Items 1–4a are done; 5a (draft-row migration) is the next
substantial one.

Added 2026-08-18, not yet in `SESSION_PLANS.md`: **three test files can commit to the
production database** (`P3`, under Bugs) — `test_audit.py` / `test_demo.py` /
`test_sync_freeze.py` resolve their session from `DATABASE_URL`. Deliberate (they test
code that commits internally), but unguarded. It is also the reason every regression
summary here says "green excluding those three files".

Parked: **Item 5b** (provisional-row season alignment — planning session, spring
2027); **v2 in-app league** — `blocked`. Retired as moot 2026-08-18: the 25/26
G. Jesus / Trossard / Kudus data corrections (see the annotated entries below).

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

### Review IL-driven keeper restoration end to end — one signal, three separate carve-outs so far

**Priority:** `P1` — re-triaged 2026-08-18: the 26/27 season is starting and IL
self-service is live, so the next mid-season IL case can arrive any week. The two
25/26 incidents below are retired as moot (frozen row, correct keepers carried); this
entry is now purely forward-looking. Scoped as the Item 6 design session in
`docs/SESSION_PLANS.md`.
**Status:** `open`. Raised 2026-08-16 after two same-night incidents (Šeško/Scott,
Kudus/Kevin T) turned out to be the same structural gap wearing two different masks.

**What actually happened tonight, precisely.**

- **Šeško/Scott**: an `InjuryList` row (Šeško, replacement G.Jesus) already existed
  from an earlier session. It correctly makes Šeško a keeper *candidate* — that union
  lives in `_derive_keeper_status` (`services.py`, `final_candidates |= {k for k,
  covered in il.items() if last_n in covered}`). But **nothing else about the IL row
  corrects roster ownership**: the real, FPL-synced `Roster` row for GW38 still had
  G.Jesus, not Šeško, in Scott's 15th slot. Every reader of ownership *other than*
  `_derive_keeper_status` — `effective_owner`/`effective_keeper_selections` (draft slot
  math), `get_rosters` (`/my-team`), `player_portal` (the Players tab's owner column),
  the "site trades applied" health check — kept showing G.Jesus as Scott's, and Šeško
  as nobody's. Fixed tonight by directly deleting the stale `Roster` row and inserting
  a genuine one for Šeško, plus closing the IL entry via `return_from_il` — justified
  **only** because this season is permanently frozen (`sync_locked`) and will never be
  re-synced. That is a one-off correction, not a repeatable mechanism.
- **Kudus/Kevin T**: no IL row exists for him at all. Instead his `KeeperSelection` is
  flagged `is_discovery=True` — a mechanism meant for the September discovery draft,
  repurposed as a workaround because the IL path (as above) doesn't fully work. That
  workaround then needed **two separate fixes of its own tonight**, in two different
  functions, discovered one incident at a time: `effective_keeper_selections` didn't
  know about it (`e503afd`, slot math), then `_derive_keeper_status` didn't know about
  it either (`958cce8`, the keepers-page display). Both are now fixed, but only because
  each was hit by accident and diagnosed live.

**The pattern worth reviewing.** Every time the league needs "this player's roster slot
doesn't reflect reality" — an IL swap, an off-roster discovery pick — the fix has been
a bespoke boolean carve-out bolted onto whichever *one* reader function broke first.
There is no single place that answers "who really holds this player, accounting for
trades AND IL AND discovery" the way `player_ownership`/`effective_owner` already do
for trades alone. That already-existing overlay pattern generalizes naturally; it just
hasn't been generalized.

**What to decide, not build, right now:**
1. Should an IL backfill (`place_on_il`) that names a real replacement also correct
   `Roster`-derived ownership automatically — via an overlay (like the trade fold in
   `_owner_maps`), never a direct mutation — so `get_rosters`/`player_portal`/slot math
   all agree without a manual one-off each time?
2. Is `is_discovery` an acceptable general-purpose "off-roster keeper" escape hatch, or
   should IL coverage and discovery status be unified into one mechanism instead of two
   parallel ones that each need their own carve-out in every reader?
3. Audit every other reader of `player_ownership`/`effective_owner` for the same
   blind spot before the next incident finds one by accident — `get_rosters`
   (`services.py:899`), `player_portal` (`services.py:1832`), and the health check's
   "site trades applied" section are the ones already confirmed unaware of IL coverage.

**Related, not duplicate:** the "G. Jesus should be a free agent" entry below is the
*release* half of this same ownership question (a player leaving with no new owner);
this entry is the *restoration* half (a player returning who was never un-rostered in
FPL's eyes). A general fix likely wants to solve both through the same mechanism.

---

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

**Priority:** —
**Status:** `retired 2026-08-18` — moot. The 2026 draft ran with the correct keepers
(Kudus kept via the workaround); the 25/26 row is frozen and nothing forward-looking
reads the stale state (clocks derive from carried seeds, which came from the correct
selections). The forward-looking work is the IL ownership design session in
`docs/SESSION_PLANS.md` (Item 6).

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

**Priority:** —
**Status:** `retired 2026-08-18` — moot for the same reasons as the Kudus entry:
draft done with correct keepers, the 25/26 row frozen, no forward-looking reader
touches the stale state. Any residue is cosmetic on archived 25/26 pages only. The
release-overlay *design* survives as an input to the IL ownership design session
(`docs/SESSION_PLANS.md` Item 6); the fix sketch below is kept as its record.

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
**Status:** `done 2026-08-18` — Item 4a (the structural fix) and Item 4b (the match
suggestion pipeline + dashboard) are both complete.

**What was built.**

- `"discovery"` joins `rules.KEEPER_ACQUISITIONS`. `rules.keeper_status` needed no
  change at all — its `if acquisition:` branch already gives any non-`"waiver"` label
  the full draft-length clock, and its final branch returns `traded_from` verbatim, so
  the label chains through a trade untouched. `set_keeper_override` accepts it for free
  via its membership check.
- **`services.link_discovery_pick` / `unlink_discovery_pick`** — set/clear
  `DraftPick.player_id` on a `draft_type='discovery'` row while keeping `player_label`
  exactly as entered (`get_discovery_board` still prefers the label, so the board reads
  the way the draft happened). No migration: the `DraftPick` CHECK is team-scoped, so
  `(discovery, player_id set, player_label kept)` was always storable. Idempotent on a
  repeat of the same link; refuses a missing pick, a goalie-team pick, a re-link over an
  existing one, and two picks pointing at one player. Audited both ways
  (`discovery.link` / `discovery.unlink`) with previous values. Admin form on
  `/admin/corrections` beside the pick tools.
  **Never auto-matched by name, deliberately** — `Player.name` is FPL's short
  `web_name` and managers type full names, so a match is a coin flip and a wrong link
  hands one manager another's keeper on a 4-year clock with nothing downstream to flag
  it. Item 4b's suggestions call this function on admin *confirm*.
- **`_derive_keeper_status`** now has two independent witnesses to a discovery
  acquisition, because neither alone covers every caller. `discovery_linked` comes from
  `DraftPick` — public draft history, no privacy gate, works for viewer-less callers —
  and is bridged through `Manager.fpl_manager_id`, never `managers.id`, since a pick
  made before a rollover carries the outgoing row's manager uuid (the
  `_goalie_team_history` hazard). `discovery_flagged` is the new broader split of the
  old `discovery_only`: *every* `is_discovery` selection rather than only the off-roster
  ones, and it stays behind the same privacy gate `discovery_only` already respected.
  `discovery_only` keeps its original narrower job of widening the candidate set.
- **`submit_keepers`** applies the discovery clock on the **on-roster** path too — the
  actual reported bug — unless a `KeeperSeed` exists, and recomputes `eligible` from the
  corrected clock rather than inheriting the stale derived flag. The goalie-team GKP
  refusal now covers the on-roster discovery case as well.

**One deviation from the plan, deliberate.** The synthesized `"discovery"` label is
gated on `not dropped`. `acquisition=` short-circuits `rules.keeper_status`'s dropped
branch entirely, so without the gate a linked pick would launder a genuine
drop-and-re-acquire clean *forever* — a player who went through the open wire is a
waiver pickup no matter how he first arrived. An off-roster discovery keeper has no
roster history, so `_dropped` is False for him and the gate is transparent to the case
it matters for; the pinned seed-precedence test still passes untouched.

**Surprise worth recording.** `set_keeper_override` with an `acquisition` but no
`years_remaining` snapshots the *currently derived* clock into the new seed — so
correcting a mislabelled `("waiver", 3)` to `"discovery"` yields `("discovery", 3)`,
not 4, because the seed then outranks the label's own fresh-clock default. That is
pre-existing and applies identically to `"draft"` and `"trade"`; it is pinned by
`test_an_acquisition_only_override_freezes_the_clock_it_found`. Pass `years_remaining`
explicitly to move the clock.

**Tests.** `tests/test_discovery_acquisition.py` (27 cases): derivation via a linked
pick, the unlinked control, cross-manager isolation, cross-league-row bridging, seed
precedence on both paths, the drop gate, the trade chain, `submit_keepers`' on-roster
override and its GKP refusal, `set_keeper_override`, the full link/unlink surface, the
draft_type-scoped taken overlay, and the privacy split. `test_discovery_keeper_slot.py`
and `test_keeper_privacy.py` pass unchanged.

### Item 4b — sync-driven match suggestions + dashboard (done 2026-08-18)

Linking was manual and therefore wasn't going to happen: months after draft night,
somebody has to find one player in an ~800-row pool from a label they no longer
remember the spelling of. So the daily sync now proposes; the human still decides.

- **`players.full_name`** (migration `b2c3d4e5f6a7`) — FPL's `first_name + " " +
  second_name`, written by `sync_players` phase 2 where the element dict is already in
  scope. `players.name` is only `web_name`, the short form, and first/second names were
  being discarded. Nullable, no backfill: there is nothing to backfill *from*, and the
  value arrives on the next full sync, which runs daily year-round because
  `sync_players` is ungated by season freeze. Canonical data written by sync — the
  legal side of the two-truths boundary.
- **`discovery_match_suggestions`** (migration `c3d4e5f6a7b8`) — `UNIQUE(draft_pick_id,
  player_id)` makes the nightly run upsert instead of accumulating, and because a
  **rejected row is kept rather than deleted**, a dismissal is never re-proposed.
  `ON DELETE CASCADE` on the pick is the only cascade in the schema, deliberately: a
  suggestion is wholly derived and regenerable, and without it `delete_draft_pick` —
  written long before this table — would start failing on an FK it can't know about.
  Not `commissioner_alerts`: that's dead code with no status column or FKs, so it
  can't express "rejected, don't ask again", which is the whole requirement.
- **`services.match_discovery_picks`** — three tiers: exact (normalized label equals
  full_name or web_name), strong (token subset either way — how a typed "Nick
  Woltemade" reaches web_name "Woltemade"), close (`difflib` ≥ 0.85). Stdlib only; the
  repo is deliberately dependency-austere. Normalisation is a **token-wise local copy**
  of `import_projections._norm` (lowercase → translit → NFKD, order load-bearing), not
  an import: that one strips every non-letter, collapsing a name to a single token, and
  `history_import._norm` carries a comment forbidding unification.
  **It never writes `DraftPick.player_id`** — a 1.0 score still only suggests.
- **Wire-up** — called from `main.py`'s post-sync hook under `plan == "full"` but
  *outside* the `sync_locked` guard: that guard is about the current league's roster
  data being final, while this reads the global pool (just refreshed) against picks
  that may live on an older league row. The offseason, when everything is frozen, is
  exactly when September's picks start arriving in the PL. Not called from `sync.py`,
  which must stay on the canonical side of the boundary.
- **Dashboard** — "Unmatched discovery picks" on `/admin/corrections`: per pick the
  label/season/owner, ranked pending candidates with Confirm/Reject, a manual link by
  FPL id, a "Run matching now" button, and a name lookup that goes through
  `search_players` so it's accent-insensitive ("Sesko" finds "Šeško", which a
  browser-side `<datalist>` filter would not — see the open datalist item). Confirm
  calls `link_discovery_pick` **first**, so a refused link leaves the suggestion
  pending rather than recording a decision that didn't happen. `data_health` gains a
  line that goes red only when candidates are actually awaiting review.

**Tests.** `tests/test_discovery_matching.py` (23 cases) + 2 in
`test_player_identity.py`. Mutation-tested: auto-linking, dropping the translit table,
forgetting prior decisions, and removing the token-subset tier each fail the intended
tests. One test was **strengthened after a mutation failed to bite** — asserting only
that an accented name was *suggested* passes even with translit broken, because
'degaard' still scrapes past the 0.85 fuzzy threshold; it now asserts the match
**tier**, which is what actually proves normalisation ran.

<details>
<summary>Original investigation (2026-08-15)</summary>

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

</details>

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

### Three test files can commit to the PRODUCTION database

**Priority:** `P3` — latent, and it has been this way a while, but the failure mode is
writes to live Neon from a routine `pytest`. Promote if anyone runs the suite on a
machine with a normal `.env`.
**Status:** `open`. Found 2026-08-18 while running the Item 4a regression.

**What's actually wrong — and what ISN'T.** `test_audit.py`, `test_demo.py` and
`test_sync_freeze.py` build their sessions from `db.SessionLocal()` rather than the
`test_session` fixture, so they connect to whatever `DATABASE_URL` names. **That part is
deliberate and correct**: they test code that *commits internally* (`services.record_audit`,
the `sync.*` tasks), and a rollback-based fixture cannot test "this commits atomically"
inside a transaction it intends to roll back. Both files say so in their docstrings, and
they delete the rows they create. Don't "fix" it by moving them onto the rollback fixture
— that would delete the coverage.

The defect is that **nothing constrains the configured DB to be a test one.**
`tests/conftest.py` already refuses to run when `TEST_DATABASE_URL` is unset or equal to
`DATABASE_URL` — the project has exactly this safety concept — and these three files
don't participate in it. On a dev machine with a normal `.env` they will connect to
production Neon and `commit()`. Cleanup is best-effort: a mid-test failure leaks rows,
and `test_sync_freeze` commits five times per run.

**Fix (small, either will do).** Point them at `TEST_DATABASE_URL` — the local Postgres
container from the regression recipe is a real committing database, which is all they
actually need — or give them the same refuse-if-it-equals-`DATABASE_URL` guard
`conftest.py` already has. The first is better: it makes them runnable in CI.

**Not a duplicate of the silent-skip item** under "Running a full regression": that one
is about DB-backed tests *skipping* when `TEST_DATABASE_URL` is missing, and its proposed
loud-skip guard would not help here, because these three never ask for
`TEST_DATABASE_URL` at all. If anything they are the inverse — they run when they should
refuse to.

**How it presents.** In a sandbox with no network to Neon they fail/error as
`sqlalchemy.exc.OperationalError: connection to server at "ep-...neon.tech"`, which is
noise that masks real failures — 14 items on every full run. Every regression summary in
this backlog that says "green excluding these three files" is describing this.

---

## Features

### Show the in-progress squad (keepers + picks so far) once the draft starts — and after it

**Priority:** `P1` — real UX gap during a live draft and in preseason; not draft-blocking.
**Status:** `done 2026-08-18`. Built as `services.get_teams_in_progress` /
`get_my_team_in_progress`, wired into `/teams`, `/team/{fpl}`, `/my-team` for
`phase in ("draft", "preseason")`. One correction to the design below, found during
implementation: the design predates the rollover, so selections/picks for a
pre-rollover draft live on the OUTGOING league row with that row's own
`manager_id`s — both new functions resolve draft data by `season_year` (via the new
`_draft_year_for`/`_in_progress_bridge` helpers) and bridge managers across rows by
`Manager.fpl_manager_id`, never `managers.id`. Also fixed in passing: extended
`_derive_gk_team_keeper_status` and `goalie_team_owner` with an optional
`season_year` override (both defaulted to the prior behavior for every existing
caller) — needed because, pre-rollover, `league.season_year` lags the draft year
by one, and a club pick would otherwise be invisible in the in-progress view. Tests
in `tests/test_in_progress_squad.py` (14 cases: real-vs-blank facts, cross-row
bridging, orphaned managers, redraft/keeper-mode clubs, route phase-branching, and
a `/v1` API stability guard). Full suite green, 0 skipped, excluding the three
files (`test_audit.py`, `test_demo.py`, `test_sync_freeze.py`) that by design hit
the live configured `DATABASE_URL` rather than `TEST_DATABASE_URL` — unrelated to
this change and blocked in this environment by no network access to Neon.

**Preseason gap (added 2026-08-17, same root cause).** Once the draft is complete and
the league moves to `preseason`, managers cannot see their teams at all on `/my-team`.
`_squad_players` (`services.py:915`) returns `[]` when `gw_id is None` — which it is
until the first FPL sync succeeds and populates `gameweeks`. Since FPL hasn't opened the
new season yet, no sync can run, no gameweek exists, and every manager's My Team page is
blank despite a fully recorded draft. The fix below (a phase-gated fallback to
`draft_picks`) resolves both the live-draft and preseason cases identically — extend the
phase condition from `league.phase == "draft"` to `league.phase in ("draft",
"preseason")`.

**The ask.** Once the draft starts, `/teams` and `/my-team` should show each
manager's kept players plus whatever they've drafted so far — growing as picks are
made — instead of last season's finished roster.

**Confirmed safe before designing anything — worth stating plainly, since the
literal wording ("cleared") could be misread as data deletion.** This is a pure
display change. Both pages currently read the `rosters` table for GW38 via
`_derive_keeper_status`'s `final_candidates` (`services.py`, `/teams`) and
`_effective_roster_pids` (`services.py`, `/my-team`) — neither queries `DraftPick`
at all, so during a live draft they show the *old, finished 25/26 squad*, with zero
connection to tonight's picks. Nothing anywhere in this codebase mutates or deletes
`rosters` outside `sync.sync_rosters` (CLAUDE.md's two-truths rule), and this
feature doesn't change that — it builds a *new* view (kept ∪ drafted-so-far) and
shows that instead, once `league.phase == "draft"`. The real roster data is never
touched.

**Design, already worked out — two new functions, not two modified ones.**
`get_keepers` backs a public, unauthenticated `/v1` API route (`api.py:59`) whose
output must not silently change based on internal phase state — that's an external
contract. So: write `services.get_teams_in_progress` and
`services.get_my_team_in_progress` as new siblings, matching their counterparts'
output shape exactly (zero template changes needed), and have the **routes** decide
which to call based on `league.phase`:
- `/teams` (`ui.py`) and `/team/{fpl}`: call the new function when
  `phase == "draft"`, else the existing `get_keepers`.
- `/my-team`: same pattern with `get_my_team_in_progress`.
- `/my-team/upcoming` and `api.py`'s `/v1/.../keepers`: **untouched** — the former
  never reads the roster list (confirmed — it only uses `me["manager"]`, the
  display name), the latter must keep its contract stable regardless of phase.

**The two new functions' contents:**
- `get_teams_in_progress`: per manager, `effective_keeper_selections` (kept
  players/clubs, with their real derived acquisition/years/eligible — unaffected by
  this feature) **∪** this manager's `DraftPick` rows for the current draft
  (rendered with `acquisition=None, years_remaining=None, eligible=None,
  kept=False` — the same "blank keeper facts for a freshly-drafted player"
  convention already established for the Players tab, `da36736`). No dedup needed:
  `record_pick` already refuses a player who's already kept, so the two sets are
  always disjoint. Keeper privacy is moot in practice here — by the time
  `phase == "draft"`, `enter_draft_phase` has already set `keepers_locked=True`, so
  selections are already revealed to everyone.
- `get_my_team_in_progress`: same idea, but reuses `get_my_team`'s existing rich
  per-player rendering (stats, ownership, availability, keeper badges) — requires
  extracting that "id set → rich rows" portion into a small shared helper first, so
  both functions call it with a different starting id set. A freshly-drafted
  player's stats shown are automatically **last season's real numbers** (the
  existing stat-lookup machinery, unchanged) — not zeros, and not something this
  feature needs to compute specially.

**Tests planned, not written:** a manager with N keepers + M picks shows N+M
players with the right blank/real fact split; a manager with 0 picks shows only
keepers (no placeholder rows — confirmed with the user); a phase-not-draft regression
guard (old pages unchanged outside the draft window); and a public-API guard
proving `/v1/.../keepers` is identical regardless of phase.

**Full design detail, if picked up later:** this plan was fully fleshed out in a
2026-08-17 planning session before being deferred — the reasoning above is the
condensed version. Re-derive quickly by re-reading `_derive_keeper_status`,
`get_keepers`, `get_my_team`, `_effective_roster_pids`, and `effective_keeper_selections`
in `services.py`, and `api.py:59`'s `/v1/.../keepers` route.

---

### `/teams` grid renders uneven card heights — one manager's list looks twice as long

**Priority:** `P3` — visual polish, requested 2026-08-16.
**Status:** `open`

**The ask.** On `/teams`, side-by-side manager cards render very differently tall —
e.g. John's next to a "Kevin" looked roughly double the length.

**The layout mechanism.** `templates/teams.html` renders one `_roster_card.html` per
manager (a `<table>`, one row per player) inside `.teamgrid`
(`templates/base.html:66`):
```css
.teamgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; }
```
Plain CSS grid, no explicit `align-items` — the default is `stretch`, so every card in
the same grid *row* is padded to match the tallest card in that row. Managers are
ordered alphabetically (`Manager.display_name`), so which cards land in the same visual
row — and therefore which one "sets the height" for its neighbors — depends on however
many columns the viewport fits, not on anything about the two managers being similar.

**The row counts genuinely vary, which compounds it.** Checked against live data — each
card's row count is `len(get_keepers(...)[i]["players"])`, i.e. the *keeper-candidate*
set from `_derive_keeper_status`, not a fixed 15:
```
Kevin S  17    Steve    13
Gaby     15    Kevin F  15
John     15    Kevin T  15
Scott    15    Michael  15    Tucker  15
```
Most managers sit at exactly 15; **Kevin S (17) and Steve (13) are real outliers**,
worth a second look on their own terms — I checked both for the exact duplicate-pair
signature that caused the Šeško/Jesus and Kudus/Trossard incidents earlier tonight (an
IL-restored player and his still-rostered replacement both counted) and did **not**
find it in either manager's list; their variance looks like ordinary roster churn, not
a repeat of that bug. Worth confirming rather than assuming, since this session found
that exact pattern twice already.

**Text length inside a row varies too.** Acquisition labels differ a lot in width
(`draft` / `waiver` / `discovery`), and each card column is only `minmax(300px, ...)`
wide — a run of longer labels can wrap onto a second line and make an otherwise
same-row-count card visibly taller, independent of row count.

**Fix directions, not mutually exclusive:**
1. `align-items: start` on `.teamgrid` stops the stretch-to-tallest behavior — cheapest
   change, but leaves the underlying row-count variance visible rather than fixing the
   comparison.
2. Show only **kept** players by default (5 rows for everyone, uniform), with the full
   15-man roster behind a toggle/expand — matches what a viewer actually compares
   manager-to-manager ("who's keeping what"), and the page's own full detail stays one
   click away rather than gone.
3. If the row-count variance itself is the thing to fix (not just its visual symptom),
   that's actually the "Review IL-driven keeper restoration" entry above — confirm
   Kevin S/Steve first before assuming this entry and that one are the same work.

---

### Schedule keeper lock and draft open — fire automatically at a set time

**Priority:** `P3` — new feature, requested 2026-08-16, the night of the 2026 draft.
**Status:** `open`. Not built tonight — draft night is not the time to add a new
scheduling subsystem; captured here so the investigation isn't lost.

**The ask.** Let the commissioner set a date/time for keepers to lock and a (separate)
date/time for the draft to open, and have both happen automatically — no manual click.

**These are already two distinct events in the data model, which is good.** `keepers_locked`
is a plain boolean toggled by the `keepers_lock` checkbox on `/admin/lock`
(`ui.admin_lock`), independent of phase. The draft opening is a phase transition,
`offseason → draft`, done today only via `services.enter_draft_phase` (admin clicks
"Start draft"). So the feature is naturally two scheduled fields, not one:
`leagues.keepers_lock_at` and `leagues.draft_opens_at` (nullable datetimes), each
triggering its own existing action when due.

**There's already a pattern for exactly this — reuse it, don't invent a new one.**
`services.advance_phase_if_due` (`services.py:701`) is the existing time/GW-driven
auto-advance heartbeat, called from every `/admin/sync` run: GW38→offseason at season
end, preseason→in_season at GW1, and the Oct-1 discovery window auto-opening, via the
pure decision function `rules.next_phase` (`rules.py:148`). The new scheduled checks
belong right alongside these — compare `now >= keepers_lock_at` / `now >=
draft_opens_at` the same way `next_phase` compares `today` against the Oct-1 threshold,
and respect `phase_manual` exactly as the existing transitions do (an admin who has
manually pinned the phase should not be silently overridden by a schedule they set
earlier).

**Explicitly excluded from this today, on purpose.** `rules.next_phase`'s docstring is
direct about it: *"admin-confirmed moves (offseason→draft, draft→preseason, closing
discovery) are explicit elsewhere."* Opening the draft was deliberately kept a manual,
admin-confirmed action — this feature is asking to cross that boundary for one specific
transition, while the manual "Start draft" button must keep working unchanged (an admin
who wants to open it early, or a schedule that needs correcting, still needs the
override).

**The real constraint: "automatically" is bounded by the sync cron's cadence, and that
cadence has real gaps.** `.github/workflows/cron.yml`:
```yaml
- cron: "0 6 * * *"          # daily 06:00 UTC — guarantees one full sync/day
- cron: "*/30 11-23 * * *"   # every 30 min, 11:00–23:00 UTC
```
`advance_phase_if_due` only runs when `/admin/sync` runs. Between 23:00 and 06:00 UTC
there is **no trigger at all** — a schedule that lands in that window won't fire until
the 06:00 sweep, up to a ~7-hour delay. A time set for, say, 7am ET (11:00 UTC in
summer) lands right at the edge of the window and is fine; a time set for 9pm ET
(01:00 UTC) would sit unfired for hours. This has to be surfaced **in the admin UI
itself** when picking a time, not left as a code comment — a commissioner setting
"draft opens at 8:00am" needs to see the worst-case delay before they rely on it.
(CLAUDE.md is explicit that this project deliberately avoids paid Render cron for
exactly this reason — a finer-grained trigger means either a paid tier or a different
external pinger, both new infra decisions.)

**A new class of concern this codebase hasn't had to handle yet: timezone.** Every
existing date field (`draft_date`, the Oct-1 discovery threshold) is a plain `Date`,
compared against `date.today()` — no time-of-day, no timezone, no DST. A "lock at
7:00am" field is a genuine `datetime`, and the league is a US-based group while the
cron and `now` in `advance_phase_if_due` are UTC. Needs a decision: store the admin's
input as UTC directly (simplest, but the admin must do the mental conversion each time)
or store a US timezone name alongside and convert at comparison time (friendlier, but
touches DST twice a year — the one detail that has bitten similar features elsewhere).

**Fix sketch.**
1. Two nullable `DateTime(timezone=True)` columns on `leagues`:
   `keepers_lock_at`, `draft_opens_at`.
2. Extend `advance_phase_if_due` (or a sibling helper called at the same site) to check
   `now >= keepers_lock_at` → set `keepers_locked = True` (idempotent — clear the field
   or leave it, but never re-fire), and `now >= draft_opens_at` → call the same path
   `enter_draft_phase` already uses, so keeper-reveal and the pool-refresh side effect
   stay in exactly one place.
3. Admin UI: two datetime inputs on `/admin/health`, with the cron-cadence caveat
   rendered next to them, and a clear display of "still pinned manually" if
   `phase_manual` would prevent the scheduled draft-open from firing.
4. Decide the timezone question above before writing the migration.

**Not in scope for this entry**: any other event someone might later want scheduled
(discovery close, trade deadline reminders) — resist letting this grow past the two
events actually asked for.

---

### Post-GW38 activity should belong to the following season

**Priority:** `P2` — the largest item here; do it with the 2026-row migration.
**Status:** `open` — the MIGRATION half is built and BLOCKED; the provisional-row
architecture remains deferred.

**Migration half — built 2026-08-18, blocked on identity.**
`scripts/migrate_2026_draft.py` (dry-run default, `--apply`, one transaction) moves
`draft_picks` / `keeper_selections` / `draft_lottery` / `draft_order_override` for
`season_year=2026` from the 25/26 row to the 26/27 row, remapping every manager FK,
with all unique-constraint collisions checked BEFORE any write. `FuturePick` and
`Trade` rows deliberately do NOT move: future picks are season-agnostic by design (a
standing multi-year outlook read cross-league by `/picks`), and a trade is a record of
when something happened — `get_trades` already attributes it to a season on READ, so
moving the rows would make storage and display disagree. Keeper seeds are REPORT-ONLY.
**It currently aborts on production** — see "FPL entry ids are NOT stable across
seasons" below; nothing has been written.

Two silent failures found and fixed while building it, both of which would have made
the migrated board *look* fine:
- **Round-2+ order** came from the row being displayed. The 26/27 row is not empty of
  standings — it has ten rows of zeroes — so the board would have rendered a
  plausible but WRONG order rather than failing. `services._prior_season_league` now
  resolves the order (and the ownership question below) from the `season_year - 1`
  row, falling back to the passed row so every pre-rollover and archived read is
  unchanged.
- **`effective_keeper_selections`** asked "does this manager still hold him" of the
  row it was passed, and `effective_owner` answers from that row's LATEST GAMEWEEK.
  The 26/27 row has no gameweeks until FPL opens the season, so every selection would
  have read as "traded away" — ten full 15-slot boards with kept players draftable,
  which is the exact failure the pre-rollover draft existed to avoid, arriving from
  the other direction. Now judged on the prior-season row, with `_manager_bridge`
  translating the ownership map's manager ids onto the row being read.

The `season_year + 1` expressions are now `services._draft_year_for` at the five
draft-scoped sites (nav link, keepers page, draft-prep, preflight, `/admin/health`).
The other five occurrences answer a different question — "which keeper cycle is
open" — and correctly stay `+1`. Deliberately NOT blanket-flipped: the 2027 draft
runs pre-rollover on the 26/27 row, where `+1` is right again.

Tests: `tests/test_draft_row_migration.py` (29 cases).

<details>
<summary>Original deferral rationale (2026-08-15)</summary>

**Note added 2026-08-18.** The trades page's season-attribution rule is now LIVE on
read (see "Trades, transactions, and picks pages are scoped to the current league
row", done): a post-GW38 (offseason) commissioner-entered trade already DISPLAYS
under the following season, computed by `services._trade_season_year` from
`Trade.created_at` against the Jan 31 deadline. **The eventual storage migration
must match this same boundary** — a trade currently displayed under season N+1 by
the read-side rule needs to end up STORED as season N+1 too, or the migration and
the display will disagree the moment it runs. Also confirmed the same day: future
picks (`FuturePick` rows) are deliberately season-agnostic and are explicitly
OUT OF SCOPE for this migration — they never move between league rows, this season
or any future one.

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

</details>

**Related trap to fix at the same time.** `/admin/sync` calls `sync_all()` with no league
id, falling back to the `FPL_DRAFT_LEAGUE_ID` env. After a rollover that env still points
at the old, now-frozen league, and every sub-task takes the frozen-skip branch which sets
`log.ok = True` — so **the nightly cron reports green while syncing nothing**. Update the
env in Render immediately after any rollover.

---

### Trades, transactions, and picks pages are scoped to the current league row

**Priority:** `P2` — surfaces immediately after every rollover.
**Status:** `done 2026-08-18`. `get_trades(db)` now queries every league row and
groups by season (a NEW display-attribution rule — see below); `get_all_transactions(db)`
wraps the existing per-league `get_transactions` per season (kept per-league
internally: GW numbers repeat 1-38 every season, so a bare-number cross-league diff
would compare unrelated gameweeks); `get_future_picks` scans every row for candidate
years, keeping `pick_ownership` league-scoped and letting the newest row win a tie on
`(round, original_owner)`. All three pages got client-side season/manager/text
filters (`trades`/`transactions`: season + manager + text; `picks`: manager only —
`base.html`'s new `_applyRowFilters`, no new endpoints). Fixed in passing: a
goalie-team trade used to render `kind="player", what="—"`; now `kind="club"` with
the club's name. Tests in `tests/test_cross_season_trades.py` (12 cases). Full suite
green, 0 skipped, excluding the three files that hit the live `DATABASE_URL` by
design (unrelated, no network path to Neon in this environment).

**New rule, confirmed 2026-08-18 — a trade's season is DISPLAY-computed, never taken
from the storing row.** `services._trade_season_year`: an FPL-synced trade
(`event_gw` set) can't have crossed a season boundary, so the storing row's own
`season_year` is exactly right. A commissioner-entered trade (`event_gw` NULL) is
bucketed by `created_at` against the spec's Jan 31 trade deadline — any trade after
GW38 (May–Dec, offseason) belongs to the FOLLOWING season, any January trade belongs
to the season that started the previous calendar year. **Known imprecision, not
solved**: `Trade.created_at` was backfilled by migration `f5a6b7c8d9e0`
(2026-08-11) with one shared timestamp on every pre-migration row, so a
pre-migration commissioner trade always buckets into 2026 regardless of when it
actually happened; an admin can set `event_gw` via `edit_trade` to re-file a
misfiled row. **This same rule must be honored by the eventual storage migration**
(see the "Post-GW38 activity" entry below) — the display grouping and the storage
alignment need to agree on where a post-GW38 trade belongs.

**Confirmed 2026-08-18: future picks are deliberately season-agnostic and are
NEVER migrated between league rows**, even once the storage migration below lands —
they're a standing multi-year outlook on pick ownership, not season-scoped history.

<details>
<summary>Original investigation (2026-08-17), kept for context</summary>

**Root cause — three separate pages, same structural mistake.**

- `/trades` → `get_trades(db, league)` filters `Trade.league_id == league.id`. All 25/26
  trades live on the old league row; the new row has none.
- `/transactions` → `get_transactions(db, league)` filters `Gameweek.league_id == league.id`.
  No gameweeks yet on the new row → empty.
- `/picks` → `get_future_picks(db, league)` queries `FuturePick.league_id` and
  `Trade.league_id`, both on the current row → empty.

**What the user expects (confirmed 2026-08-17).**

- **Trades**: show all trades from every season, not just the current one. Trades are a
  permanent record of who dealt with whom; there is no reason to hide last season's.
- **Transactions**: same — cross-season add/drop history. Scoping to one league row makes
  the page blank for the entire preseason, and loses prior history forever once a season
  ends.
- **Picks**: forward-looking view of pick trades for the next ~5 years. These picks are
  already entered on the old league row; scoping to the new row hides them. The correct
  scope is all pick trades with `pick_season_year >= current_season_year`, regardless of
  which league row stores them.

**Fix sketch.**

- `get_trades`: remove the `league_id` filter; join to `League` to get the display
  league name per row; order by `created_at` desc. Already has a `league` arg for manager
  name resolution — thread `db` and query all leagues' managers, or accept a name map
  keyed by `league_id`.
- `get_transactions`: same — drop the `league_id` filter on Gameweek; group by season
  with a year header in the template. Alternatively, render per-season with the
  current season first (matching the trades page's newest-first ordering).
- `get_future_picks`: drop the `league_id` filters on `FuturePick` and `Trade.league_id`;
  filter to `pick_season_year >= current_year` to keep the view forward-looking. The
  `pick_ownership` call already passes `league` for manager name resolution — may need the
  same cross-league name map as trades.

</details>

---

### History page is scoped to current league row

**Priority:** `P2` — surfaces after every rollover.
**Status:** `done`. Completed 2026-08-18.

**Fix applied.** Removed `league_id` filter from all 5 queries across 3 functions:
- `get_history`: `SeasonHistory`, `ManagerHonors`, `HistoricalStanding`
- `_cups_by_season`: `CupMatch`
- `_discovery_by_season`: `DiscoveryResult`

All 5 queries now read across all league rows, not just the current one.

**Deduplication — "first wins" needed a defined `first`.** `seasons` (by year) and
`honors` (by manager name) dedupe; `standings`/`cups`/`discovery` MERGE their groups by
year, as specified. The two deduping queries now join `League` and order by
`League.season_year DESC`, so the **newest league row wins**, then re-sort for display.
Without that join the winner was whatever order Postgres returned — in practice heap
order, so the *stale* row won. This matters exactly where the duplicate arises: around a
rollover, when the same history is imported onto both the outgoing and incoming row and
the incoming one is the correction. Pinned by
`test_history_dedupes_duplicate_years`, and mutation-tested (dropping the join fails it).

**Note for whoever touches these tests:** `SeasonHistory.year`, `HistoricalStanding.year`,
`CupMatch.season` and `DiscoveryResult.season` are **VARCHAR**, not Integer — only
`League.season_year` is an int. The first cut of the tests compared `'2025' == 2025` and
silently passed only because they were being SKIPPED for want of `TEST_DATABASE_URL`.
Functions retain their `league` parameter for signature stability; it is noted as unused
in docstrings. Tests added in `tests/test_history_cross_season.py` verify cross-row
reads and deduplication. The template (`templates/history.html`) is unchanged; the
query result shapes are identical.

---

### FPL entry ids are NOT stable across seasons — identity carry silently did nothing

**Priority:** `P0` — every manager's login is broken and every keeper clock on the
26/27 row is missing. It also BLOCKS the 2026-draft row migration.
**Status:** `open`. Found 2026-08-18 by `scripts/migrate_2026_draft.py`'s dry run,
which aborted rather than move anything.

**The finding.** `managers.fpl_manager_id` (FPL's `entry_id`) is documented throughout
this codebase as the stable cross-season identity — `advance_season` carries
display_name/password_hash/keeper seeds by it, `_goalie_team_history` keys on it, the
login session stores it, and every cross-row bridge added in Items 1/2/4a uses it.
Production says it is not stable:

| row | entry ids |
|---|---|
| 25/26 (fpl 1754) | 5520, 5687, 17902, 21768, 43908, 192955, 247171, 248583, 264571, 268927 |
| 26/27 (fpl 11818) | 58528 … 58537 (contiguous, freshly issued) |

Overlap: **zero**. FPL issued every manager a brand-new entry for the new season.

**What that silently broke at the rollover.** `advance_season` matches
`new_mgrs.get(om.fpl_manager_id)` and `continue`s when it misses, so both carries were
complete no-ops and nothing logged a warning:

- **display_name** — all ten are NULL on the 26/27 row (confirmed).
- **password_hash** — all ten are NULL, so **every manager's login is broken**; the
  session also stores the old entry id, which now matches no current manager row.
- **KeeperSeed** — **0 rows** on the 26/27 row against 152 on the 25/26 row, so every
  kept player's clock is gone. `_derive_keeper_status` will derive fresh clocks for
  them, which is wrong in both directions (a spent keeper becomes keepable again).

**There is no UI to repair it.** `display_name`'s only writer anywhere in the app is
`advance_season`'s carry — the one that failed. Setting the ten person names needs an
admin surface or a one-off script before anything else can proceed.

**Order of work.**
1. Set `display_name` on the ten 26/27 managers. The mapping is NOT mechanically
   derivable — FPL team names changed too, and while some are obvious ("Fighting
   Franckes", "Pep's Scraps") others are genuinely ambiguous ("Le Roi De Coupe" →
   "Le Féez Nuts"?). A commissioner has to supply it.
2. Reset passwords (the existing `/admin/health` reset button clears the hash so each
   manager sets a new one), or carry the old hashes across once the mapping exists.
3. Re-run the keeper carry so the 26/27 seeds exist with the clock ticked.
4. Then run `scripts/migrate_2026_draft.py --match display`.

**Wider implication to decide.** Every cross-row bridge in the codebase keys on
`fpl_manager_id` — `_goalie_team_history`, `_in_progress_bridge` (Item 1),
`_manager_bridge` and `discovery_linked` (Item 4a). All of them silently return
nothing across a rollover boundary rather than failing. `display_name` is the only
identity the league actually owns; consider making it the bridge everywhere, with a
health check that fails when any current manager lacks one.

---

### 2026 draft board inaccessible after rollover

**Priority:** `P2` — reference data that managers will want to consult.
**Status:** `open`. Found 2026-08-17.

After rollover, `/draft/2026` on the new current league row renders empty (all
`DraftPick` rows are on the old 25/26 league row). The draft board is a permanent record
— who drafted whom, in what round — and should remain browsable as part of the season
archive.

**Where it should live (confirmed 2026-08-17).** Under `/seasons` / `/season/{id}` —
the same place historical standings and scores live. The season detail page should link
to (or embed) the draft board for that season. The `/draft/{year}` route resolves via
the current league, which is why it goes blank; a season-scoped route that passes the
old league row to `get_draft_board` would serve it correctly from the archived data.

**Fix sketch.** Add a `GET /season/{fpl_league_id}/draft/{year}` route (or extend the
existing `/season/{id}` template to include a draft section) that resolves the league by
`fpl_league_id` rather than `is_current`, then calls `services.get_draft_board(db,
old_league, year, "main")`. No data migration needed — the picks are already there.

---

### Post a message to Discord when a trade is recorded

**Priority:** `P3` — new feature, requested 2026-08-15. **To explore, not yet designed.**
**Status:** `open`

**The ask.** When a trade is posted in the app, announce it in the league's Discord.

**Where it would hook.** Every trade write already emits an audit record, so the action
names are the natural inventory of what would fire — `trade.record`
(`services.record_trade`, the public `/trade` form), `trade.player` and `trade.pick`
(the commissioner draft-page entries), `trade.goalie_team`, plus `trade.edit` and
`trade.delete`. `record_audit` is the one call all of them already make, which makes it
the obvious single choke point — worth checking whether that is a feature (one hook, and
other event types come free later) or a trap (audit is a write-path primitive; coupling
it to an outbound HTTP call gives every future audited action a network dependency).

**Things to think about before designing it:**

- **A Discord webhook needs no bot** — a URL in env and a JSON POST. Follows the existing
  secret-handling pattern in `SECURITY.md`; the URL is a credential, so env only.
- **It must not be able to fail a trade.** The write is the real work and the
  announcement is a side effect; a Discord outage or a rotated webhook must not roll back
  or 500 the trade. Fire-and-forget, or queue it.
- **This is the first *outbound* call in a request handler.** CLAUDE.md's architecture
  rule ("don't add live FPL calls into request handlers") is about inbound sync, but the
  reasoning — request latency and an external service's availability leaking into ours —
  applies identically here.
- **`sync_trades` is the sharp edge.** Trades also arrive from the FPL feed, and sync is
  idempotent and re-runs constantly. Announcing on sync without an already-posted marker would
  re-post the same trade on every run, and a historical backfill would dump the entire
  trade history into the channel at once. Needs a persisted per-trade flag, not an
  in-memory guard.
  
- **What the message should say** is a real question, not a detail: `get_trades` already
  assembles the human-readable shape the site renders, so reuse it rather than
  re-deriving. Decide whether pick trades and club trades read well in one line, and
  whether commissioner *edits* and *deletes* should announce at all or stay silent.
- **Adjacent asks it should not quietly grow into**: keeper submissions, draft picks
  going on the clock, waiver moves. Worth knowing whether this is "trades" or "a
  notifications feature" before building the first one.

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
