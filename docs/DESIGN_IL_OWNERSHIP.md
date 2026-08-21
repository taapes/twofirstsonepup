# Design — absence ownership (injury list + international list)

**Status:** design agreed 2026-08-20. **Item 6b (§4, §10) and Item 6c (§6) are both
BUILT**, 2026-08-20/21, uncommitted. See `docs/SESSION_PLANS.md`.
Deviations from this document, both recorded there: the admin historical IL backfill is
exempt from §10's roster-ownership guard (that route exists because the snapshot *can't*
confirm the placement), and "season over" is the phase or the gameweek, not the gameweek
alone — `current_gameweek` returns None without deadline dates, which would have made
the §4.4 enforcement silently inert. For 6c: the minutes-persistence logic was split into
a plain, testable `services.record_absentee_minutes(db, league, live_stats, gw_number)`
rather than left inline in `sync.py`, so it can be unit-tested without an HTTP call or
the configured database.
**Scope:** the next mid-season absence case, on a live, syncing season. Retroactive
repair of the frozen 25/26 season is **explicitly out of scope** (decided 2026-08-18).
**Reading order:** this document assumes `CLAUDE.md` (the two-truths boundary, the trade
overlay, the IL/international bullet) and the `docs/BACKLOG.md` entry "Review IL-driven
keeper restoration end to end".

---

## 1. The problem

When a manager places player X on the injury list and drops him in FPL, adding
replacement Y, the synced `rosters` table shows Y and not X. **Exactly one reader knows
X is still held** — `_derive_keeper_status`, via the IL-coverage union at
`services.py:3247-3248`. Every other reader of ownership disagrees.

Each past incident was fixed by bolting a bespoke boolean onto whichever reader broke
first. There are **three** such carve-outs today, each found live, one incident at a
time:

| Where | Commit | What it patched |
|---|---|---|
| `services.py:3248` | — | the IL-coverage union into `final_candidates` |
| `services.py:5062` | `e503afd` | `or s.is_discovery` in `effective_keeper_selections` (slot math) |
| `services.py:3298` | `958cce8` | the `discovery_only` union in `_derive_keeper_status` (display) |

The last two are the "Kudus workaround": a real IL situation was encoded as
`KeeperSelection.is_discovery=True`, because the IL path didn't work — and that costume
then needed two fixes of its own, in two different functions, on two different nights.

**The root cause is not any one reader.** It is that there is no single place that
answers "who really holds this player, accounting for trades AND absences" the way
`player_ownership` / `effective_owner` already do for trades alone. That overlay pattern
generalises naturally; it just never was generalised.

---

## 2. Settled league rules

These were decided by the commissioner on 2026-08-20 and are **not** open for
re-litigation during implementation.

1. **Ownership is additive.** A manager holds their 15 FPL-rostered players **plus**
   everyone out on an absence. Each replacement is an ordinary, keeper-eligible roster
   player — not a placeholder.
2. **The IL is capped at one player per manager. The international list is UNCAPPED.**
   However many of a manager's players are called up to AFCON / the Asia Cup are all
   covered, each with its own replacement. The league cannot control call-ups, so a cap
   would arbitrarily punish whoever drafted those internationals. Effective squad size
   is therefore **15 + (≤1 IL) + N intl** — 18 during a three-call-up AFCON window.
3. **A roster is always exactly 15. A player can never be dropped without a
   replacement**, so a manager is never at 14. (This holds in v2 as well.) It is why
   the returning-player case needs no roster arithmetic from us — the manager makes the
   swap in FPL and the sync sees a 15-man squad either way.
4. **The returning player may displace ANYONE.** There is no requirement that the
   original replacement be the one dropped. *(Decided 2026-08-20, replacing an earlier
   rule that the replacement's slot must be tracked and released — see §5 for why that
   rule was withdrawn.)* Mid-season the manager simply makes the swap in FPL. At season
   end the roster is frozen, so **the manager names the player who leaves** — see rule 5.
5. **An absence open after GW38 must be resolved down to 15**, by one of the two paths
   `requirements.md:87` already names:
   - **Release** the absentee — he goes to free agency, the frozen 15 stands.
   - **Return** the absentee — he is held, and the manager **must name one of the frozen
     15 to drop**. This is the only manager-designated release in the system.

   Unresolved is not an option: keepers are capped at 5 either way, so a manager left
   holding 16 would be choosing 5 from 16 while everyone else chooses 5 from 15.
6. **A returning player must be re-added immediately**, and the site must alert when he
   isn't — the signal is the absent player logging **minutes** for his club while still
   off the manager's roster. For the IL the alert is suppressed until the 4-GW minimum
   stay has elapsed (**the minimum stay holds even if he recovers sooner**); for the
   international list it applies as soon as he plays.
7. **No validation that an international absence is genuine.** It is commissioner-checked,
   on the honour system, exactly as today.

---

## 3. Semantics end to end (design question 1)

### While X is on the absence list

FPL-canonical `rosters` shows 15, including replacement Y. The overlay adds X. Effective
squad = 16.

| Surface | Shows |
|---|---|
| `/my-team`, `/teams` | 16 — the 15 synced plus X, badged as on IL/international |
| Players tab (`player_portal`) | X's owner column = the manager (today it reads "nobody"); the existing `on_il` badge is unchanged |
| `/transactions` | "dropped X, added Y" — **the raw FPL truth, deliberately unchanged** |
| Anti-tanking | X is not in `GameweekPoints.player_points` at all (he is off FPL's roster), so he contributes nothing. Y's zeros count normally. Unchanged. |
| Keeper derivation | X is a candidate (already true today); his clock is protected because the absence explains the roster gap |
| Slot math (`effective_keeper_selections`) | X counts as a keeper **without** the `is_discovery` costume |
| `/admin/health` 15-man check | still 15 — **deliberately raw**, it validates the sync |

### When X returns mid-season

The manager re-adds X in FPL and drops whoever they choose (rule 4) — the roster is
always 15, so one always goes out (rule 3). The next sync shows X on the roster; `reconcile_absences` (`services.py:2171`) closes the
entry automatically. The overlay stops adding X exactly as the snapshot starts carrying
him — a clean handoff with no window where he is counted twice or not at all.

### When the absence is still open after GW38

There is no next snapshot — `advance_season` sets `sync_locked` and the GW38 roster is
frozen — so the manager cannot make the swap in FPL. **The site must therefore let them
resolve it, and must not let them skip it** (rule 5).

The end-of-season prompt already exists (`templates/my_team.html:54`, "Season over — add
this player back to your roster (Return) or Release them"), and its two buttons resolve
very differently today:

| Button | Calls | Result | Resolves to 15? |
|---|---|---|---|
| Release | `return_from_il(via='waiver')` → `'waived'` | X → free agency, frozen 15 stands | **yes**, already |
| Return | `return_from_il(via='manual')` → `'returned'` | X held (§4.1), frozen 15 also held | **no** — leaves 16 |

**The Return branch is the gap.** It must additionally ask *who leaves*, and store that
choice — see §4.4. Leaving it unresolved is a real competitive advantage, not a cosmetic
one: keepers are capped at 5 regardless, so 16 candidates means picking 5 from 16 while
everyone else picks 5 from 15.

### Is it "16 held" or "15 with the slot lent"?

**16 held.** Rule 1. The same-position requirement exists to stop a manager gaining a
*positional* advantage from an absence, not to make the replacement a non-entity. The
replacement is a real acquisition with a real keeper clock. What stops a manager
stockpiling bodies is rule 3 (the roster is always 15, so a returning player always costs
someone) and rule 6 (he must be brought back once he is playing again) — not any
restriction on *who* leaves.

---

## 4. The design

### 4.1 One predicate, one helper

Extract `_absence_held(db, league, last_n) -> {(manager_id, player_id)}`, answering
**"who holds him now"**. This is deliberately a *different question* from
`_absence_cover` (`services.py:3100`), which answers **"was he excused that week"**.

The non-forking constraint in `CLAUDE.md` is satisfied not by sharing one function but by
being honest that there are two questions over the **same rows**, and by giving the
ownership question exactly **one** implementation — consumed by both `_owner_maps` **and**
`services.py:3248`.

**Predicate:** held iff `status == 'active'`, **or** (`status == 'returned'` **and**
`last_n <= end_gw`). `'waived'` is never held past `end_gw`. NULL status is not held.

> **Why not just `status == 'active'`?** Because it forks from `_absence_cover` on every
> season-end return, and the fork is silent and permanent. `_absence_cover` is
> range-based and status-blind — `(e.end_gw or last_n)` means a `'returned'` entry with
> `end_gw >= last_n` is **still covered**. At GW38 that never expires, because `last_n`
> stops advancing. A status-based fold would then disagree with the coverage-based
> candidacy union at `3248`, and it lands on the worst possible pair: `submit_keepers`
> validates against `_derive_keeper_status` (coverage → accepts X), while
> `effective_keeper_selections` validates against `effective_owner` (status → drops the
> selection). The manager submits X, the site accepts it, and they silently lose a draft
> pick — the exact failure class `services.py:5046-5061` already documents, arriving
> through a new door.
>
> `tests/test_il_keeper_visibility.py:166` *looks* like it pins this. It uses
> `end_gw=15`, well below `last_n`, so the case is untested.

**Switching `3248` to this helper also fixes a live bug.** `reconcile_absences` keys on
`(e.manager_id, e.player_id)`, so when X is dropped and claimed off waivers by a
*different* manager, the entry never auto-closes; `_absence_cover` keeps covering
`(m, X)` and X becomes a keeper candidate for **both** managers. The fold's
"only if unowned" guard blocks the phantom. Leave `3248` on `_absence_cover` and this
bug survives the rewrite.

### 4.2 Fold order: absences BEFORE trades

The trade fold in `_owner_maps` is guarded fail-closed at `services.py:5015`:

```python
if owner.get(t.player_id) == t.from_manager:
```

X is off-roster, so `owner.get(X)` is `None`. **Folding absences after trades makes a
trade of an absent player permanently unappliable**: the guard never fires, then the
absence fold writes `owner[X] = sender`. `/admin/health`'s "site trades applied" check
goes red forever with no way to clear it, and the receiving manager never inherits the
keeper clock.

Fold absences **first** and the two compose with no carve-out at all: `owner[X] = m`,
then the trade guard `owner.get(X) == m` succeeds → `owner[X] = n`. `_derive_keeper_status`'s
existing `hist` vs `owner` split (`services.py:3420-3438`) then handles the traded
absentee exactly as it handles a traded rostered player, by reuse rather than by a new
branch.

### 4.3 Fold mechanics

- **Guard `if pid not in owner`.** Written unconditionally, the fold would let a
  mis-entered absence *steal* a rostered player from another manager. Comment it the way
  `services.py:5012-5014` comments the trade guard.
- **Order the query `.order_by(start_gw, id)`.** `player_ownership` is called twice
  within a single `player_portal` request (`services.py:2076` and again via
  `_derive_keeper_status` at `3426`); a nondeterministic winner would make two panels on
  one page disagree.
- **Naturally N-ary.** A set of `(manager, player)` pairs handles an uncapped
  international list with no structural change. It does need explicit tests — every
  existing fixture has at most one absence per manager.
- **IL and international fold identically.** Same shape, same rules, same treatment in
  `_absence_cover` today.

### 4.4 One subtraction, and the manager names it

There is exactly **one** subtraction in this design, and it exists only to serve rule 5's
Return branch: at season end the manager returns X and names one of the frozen 15 to
drop. It is **manager-designated, never derived** — which is the whole difference between
this and the withdrawn succession rule (§5).

**Storage: a nullable `released_player_id` FK on `InjuryList` / `InternationalList`.**
Not a `roster_releases` table. There is at most one release per absence resolution, the
absence row already exists, and keeping the decision on the row that caused it means it
cannot be orphaned. Set it in `return_from_il` / `return_from_intl`; fold it into
`_owner_maps` as a subtraction **after** the additive fold.

**Required only at season end.** Mid-season the manager makes the swap in FPL and the
sync sees it, so `released_player_id` stays NULL and cessation is exactly right. The
Release/waive path never needs it either — X simply goes to nobody. So the field is NULL
in the overwhelming majority of rows.

**Enforcement.** An unresolved absence must not be allowed to reach keeper selection,
because that is where the extra candidate would be cashed in:

- `submit_keepers` refuses for a manager with an unresolved post-GW38 absence. Prefer
  this manager-scoped block to a global one — it stops exactly the person who owes a
  decision and leaves everyone else alone.
- `advance_season` refuses while any absence is unresolved, in the same fail-loudly shape
  as its pairing check, with the same `force=True` hatch.
- A `data_health` check lists them.

**What this does NOT reintroduce.** No `roster_releases` table, no general "release any
player for any reason" primitive, no admin release form, and no automatic release of the
replacement. A release exists only as the counterpart of a season-end Return.

> **An earlier draft of this document argued no subtraction was needed at all.** That was
> wrong, and the error is worth recording: it assumed a manager left holding 16 candidates
> was harmless because "released" and "not kept" both end in the draft pool. They do — but
> the keeper cap is 5 either way, so the sixteenth candidate is an extra *choice*, and
> `requirements.md:87` already required a post-GW38 resolution. The lesson is that the
> squad has to balance for competitive reasons even where no downstream reader would
> crash.

---

## 5. Withdrawn: slot succession

An earlier version of rule 4 said the returning player must displace **the replacement's
slot**, followed forward through any subsequent drop-and-replace. It was withdrawn on
2026-08-20 as **too hard to automate for the value it returns**, and the reasoning is kept
so it isn't proposed again.

The tracking itself was tractable: there is no add/drop write path in this codebase
(`rosters` is written only by `sync.sync_rosters`; add/drops are *derived* by diffing
consecutive snapshots), so it would have been a post-sync hook in `reconcile_absences`,
advancing `replacement_id` to whichever same-position player arrived as the old one left.

**The killer was ambiguity.** FPL does not record add/drops as paired transactions — we
reconstruct them by diffing rosters week to week. A manager who drops two midfielders and
adds two midfielders in the same gameweek produces a diff with no fact in it saying which
arrival replaced which departure. Guessing wrong would silently release the wrong player
at season end with nothing downstream to catch it, so the design would have had to stop
and ask the commissioner — a manual step on an uncommon-but-not-rare event, to enforce a
rule with no observable consequence (§4.4).

**What replaced it is rule 5's Return branch (§4.4): the manager NAMES who leaves.**
That keeps the outcome the withdrawn rule was after — the squad resolves to 15 — while
removing the part that could not be automated, namely inferring *which* player the league
would have designated. Deriving is a guess; asking is a fact.

Withdrawing it did dissolve two things: the post-sync succession hook, and the "nobody to
displace" edge case (which rule 3 makes impossible anyway — a roster is always 15).

---

## 6. Return-required alert (rule 6)

**The data is already fetched and thrown away.** `sync_gameweek_points` (`sync.py:742`)
pulls `/event/{gw}/live`, whose `elements` map carries minutes for **every** player in the
game — but only persists minutes for players in each manager's picks
(`player_points`, `sync.py:766-773`). An absent player is on nobody's roster, so his
minutes are fetched and discarded. This is the same shape as `_upsert_pl_teams`, which
CLAUDE.md already describes. **The alert needs no new HTTP call.**

**Do not solve this by widening `player_points`** to include absent players.
`rules.zero_minute_count` iterates that list, and it must keep meaning "FPL's lineup",
not "our notion of the squad". Persist the fact on the absence row instead — a
`last_played_gw` is the smallest thing that works, and it is the only new stored field in
this whole design.

**Firing the alert.** The absent player has logged minutes and is still off the manager's
roster — gated, **for the IL only**, on `il_return_eligible_gw` (`services.py:1870`)
having passed. Surface it through the channels that already exist rather than inventing
one: `flagged_actions` (`services.py:2211`, already the "you owe the league an action" nag
on the homepage) and a `data_health` check.

If the manager *does* re-add him, `reconcile_absences` closes the entry and the alert
disappears on the same sync.

---

## 7. Does `place_on_il` feed the overlay automatically? (design question 2)

**Yes, and with no new writes.** The overlay is *derived* from the `injury_list` /
`international_list` rows on read. `place_on_il` continues to write exactly one row plus
an audit row and to touch `rosters` never — the two-truths boundary is preserved
unchanged. Placing on the IL feeds ownership because the fold reads the row, not because
anything was written into a roster.

**What `return_from_il` unwinds:** nothing, structurally. Setting `end_gw` and `status`
makes the predicate in §4.1 stop returning the pair, and the overlay stops adding X — at
season end, only once `last_n` passes `end_gw`, which is what keeps him a keeper candidate
for the season just played (§4.1). There is no unwinding step to get wrong, which is the
main advantage of deriving rather than storing.

**One caveat the fold makes urgent.** `place_on_il` does not check that the injured player
is on the manager's roster, and `_resolve_player` resolves globally. Today the blast
radius is keeper candidacy and a tanking excuse; after the fold it is **full ownership**,
including draft slot math. See §10.

---

## 8. Is a release path required? (design question 3)

**A narrow one, yes. A general primitive, no.**

Mid-season, ownership is purely **additive** and its unwind is **cessation** — no release
needed. The single exception is rule 5's Return branch at season end, where the roster is
frozen and the manager must name a player to drop to get back to 15 (§4.4). That is one
nullable column, not a subsystem.

The four consequences the retired G. Jesus entry named, resolved:

1. **Keeper clock.** Handled by the subtraction itself rather than by feeding `_dropped`:
   the named player is no longer owned at the final gameweek, so he is not in
   `final_candidates` and nobody can keep him.
2. **Draft availability.** **Moot.** `search_players`' taken-oracle reads
   `KeeperSelection` + `DraftPick`, never ownership — confirmed independently by two
   audits. He is draftable already. Recorded here so it is not rebuilt.
3. **`/admin/health` 15-man check stays RAW.** It validates the *sync*. The comment at
   `services.py:6901` is the precedent, and the model for every "deliberately raw" note in
   §9.
4. **Admin form.** **Not needed** — the manager names the player on their own My Team
   page, as part of the Return they are already performing. Admin can act for them via
   the existing `can_act_as` bypass.

What is still **not** built: a `roster_releases` table, a release reason, releasing a
player outside an absence resolution, or any automatic choice of who leaves.

---

## 9. Reader audit (design question 4)

Every reader of ownership, with a verdict. **Anything reading raw must carry a comment
saying why**, in the style of `services.py:6901` — the absence of such a comment is what
let three carve-outs accumulate unnoticed.

### Consumes the new layer

| Reader | Location | Surface |
|---|---|---|
| `get_rosters` | `services.py:1025` | `/v1/.../rosters` |
| `get_my_team` | `services.py:1153` | `/my-team` |
| `get_upcoming_matchups` | `services.py:1296` | `/my-team/upcoming` |
| `player_portal` | `services.py:2053` | Players tab owner column |
| `_derive_keeper_status` (ownership half) | `services.py:3426` | keepers, `/teams`, draft prep |
| `effective_keeper_selections` | `services.py:5020` | draft slot math |
| `_effective_roster_pids` / `_squad_players` | `services.py:5067` / `1048` | the shared funnel |
| `manager_assets` | `services.py:6686` | **newly** — see §10 |

### Deliberately raw, reason recorded

| Reader | Location | Why raw |
|---|---|---|
| `get_transactions` | `services.py:1974` | Diffs consecutive snapshots. An overlay has no gameweek to attribute a move to, and this is the raw FPL truth by definition. |
| `_roster_presence_and_il_coverage`, `_dropped` | `services.py:3125`, `3341` | Roster **history** must not follow ownership. Re-keying presence to a new owner makes `_dropped` read False and silently un-caps the keeper clock. |
| `_tanking_counts_by_manager` | `services.py:1422` | Reads `GameweekPoints.player_points` JSON, not `rosters`. Excusal is keyed on the manager's actual FPL squad. |
| `data_health` 15-man check | `services.py:6897` | Validates the sync. A legal trade already leaves 14/16. |
| `data_health` keeper-seed check | `services.py:6935` | Raw today with **no** recorded reason — 6b must either justify it or fold it in. |
| `reconcile_absences` | `services.py:2182` | It *is* the sync's handoff; overlaying its input would make it self-referential. |
| `search_players` | `services.py:5659` | Has no ownership notion at all: "taken" means kept-or-drafted. |
| `sync_rosters` | `sync.py:664` | The only writer. |

---

## 10. Supporting corrections

- **Lift the international cap.** Drop the one-active-entry guard in `place_on_intl`
  (`services.py:1928`), correct the model docstring (`models.py:393`, which claims "One
  active entry per manager"), and move the place form in `templates/my_team.html:110` off
  its `{% elif %}` so it renders alongside an existing absence — today a manager with one
  player away cannot add a second even if the guard is lifted. **Keep `place_on_il`'s
  cap.** `CLAUDE.md:411` is already correct; it says one replacement per *absence*.
- **Close the `place_on_il` write path.** Require the injured player to be on the
  manager's *effective* roster, and add the `injured.id == replacement.id` refusal that
  `place_on_intl` already has at `services.py:1920`. Without this, after the fold any
  manager can name any un-rostered player as injured and the site will say they hold him.
- **Block the rollover on open absences.** `advance_season` must refuse while any entry
  is active, in the same fail-loudly shape as its pairing check (`services.py:685-694`),
  with the same `force=True` hatch. **The absence overlay is not self-retiring** the way
  the trade overlay is (`player_ownership`'s docstring, `services.py:4966-4971`): nothing
  closes an entry when the player leaves the PL, is claimed by someone else, or the
  manager simply never clicks return. That difference is the single most important thing
  to state in the code comments.
- **New `data_health` checks:** an active entry whose player is rostered by someone else;
  one player named by two managers' entries; NULL-status entries.
- **`manager_assets`** (`services.py:6686`) — route players through
  `_effective_roster_pids`, the shape `_squad_players` already uses. It is the only
  `Roster` join in the repo, it sits on the **trade-entry form**, and it overlays picks
  but not players — so a commissioner-traded player still shows on the seller's side.
  After the fold it would also hide an absent player, who is very often exactly the asset
  a manager wants to trade.
- **`player_portal`** (`services.py:2071`) — replace the hand-rolled, byte-for-byte copy
  of `effective_owner` with a call to it.

---

## 11. Keeper interaction — zero manual steps (design question 5)

The Šeško/Kudus shape, replayed under this design:

1. Manager IL's X in-season and drops him in FPL. One row written. **No row surgery.**
2. X is a keeper candidate — already true today via `3248`, now through the same helper
   the overlay uses, so candidacy and ownership cannot disagree.
3. X counts against the manager's keeper cap and **charges a draft slot**, because
   `effective_keeper_selections` sees him as owned. **No `is_discovery` costume.**
4. At season end the manager resolves the absence on their own My Team page — Release X,
   or Return X and name who leaves. The squad balances to 15. **No row surgery, no
   commissioner.** They cannot reach keeper selection without doing it (§4.4).
5. If the manager parks X mid-season and never brings him back, the alert (§6) names him.

Every step that previously required a commissioner is now derived. **There are no
remaining manual steps** — the only judgement call in the earlier draft (ambiguous slot
succession) disappeared when the succession rule was withdrawn (§5).

---

## 12. `is_discovery` and migration (design question 6)

**Both Kudus-era carve-outs stay, unambiguously.** `e503afd` (`services.py:5062`) and
`958cce8` (`services.py:3292-3298`) are *correct discovery-draft logic*: an off-roster
discovery keeper is the normal case for the discovery draft, not evidence of a lost
player. What this design removes is the **need to misuse the flag** for an IL situation —
X is now genuinely owned, so no costume is required. Preserve the LABEL-vs-FLAG
distinction at `rules.py:444-449`: `"discovery"` the acquisition label is not
`KeeperSelection.is_discovery` the bonus-slot flag.

**Nothing changes for frozen seasons.** IL rows carry no `league_id` and are scoped only
through `Manager.league_id`, so entries cannot leak across league rows.
`_manager_bridge` (`services.py:4176`) translates an absence-derived owner like any other
manager uuid, so `effective_keeper_selections` keeps working across a rollover. Archived
seasons re-read on their own row are unaffected.

---

## 13. Spec corrections this design requires

- `docs/requirements.md:77-88` — "One IL player per manager" is right for the IL, but the
  international list is uncapped and the effective squad is **15 + (≤1 IL) + N intl**.
  Rules 3, 4 and 5 are new and belong in the spec — in particular **a roster is always
  15 and a player cannot be dropped without a replacement**, which is stated nowhere today
  and is what makes the returning-player case need no arithmetic from us.
- `CLAUDE.md`'s IL/international bullet — same, plus the new ownership overlay.
- `models.py:393` — the docstring's "One active entry per manager" is wrong today.
- **"15-man" copy** at `templates/teams.html:5`, `templates/my_team.html:6`,
  `templates/my_team_upcoming.html:29` — now wrong on its face.

---

## 14. Open questions — all closed 2026-08-20

No open questions remain. For the record, the three that were raised and how they closed:

1. **Ambiguous succession** — dissolved. The rule that created it was withdrawn (§5), and
   the manager now names the departing player rather than the system inferring him.
2. **A returning player with nobody to displace** — impossible. A roster is always 15 and
   a player cannot be dropped without a replacement (rule 3).
3. **Enforcement** — **alert loudly, fine by hand.** Confirmed alert-only. Note this is
   not merely a preference: the action happens in the FPL app, so there is no request of
   ours to block and enforcement *cannot* be technical. The alert names the manager, the
   player, and how long he has been playing while parked, on the homepage
   (`flagged_actions`) and `/admin/health`. If the commissioner wants it to cost money,
   the existing manual ledger does it — `services.add_fine` (`services.py:1563`) takes any
   amount and a free-text reason, on `/admin/standings`. **Do not wire an automatic
   fine**; nothing else in the app converts a flag into money except last place, and an
   automatic one would misfire on sync timing.

---

## 15. Build sequencing

Two sessions.

- **Item 6b — the ownership fold + season-end resolution.** `_absence_held`, fold order,
  the `released_player_id` subtraction and the Return-names-who-leaves form, the
  `submit_keepers` block, the reader audit, `manager_assets` / `player_portal`, the
  uncapped international list, the `place_on_il` guard, the rollover block. Self-contained, and it is what retires the three carve-outs.
- **Item 6c — the return-required alert.** Persist the absent player's minutes in
  `sync_gameweek_points` and flag when he is playing but still off the roster. Touches
  `sync.py` and adds the only new stored field (`last_played_gw`).

6c is small and independent — it could ride along with 6b, but it is the only piece
touching the sync path, so it is kept separate to keep 6b's blast radius on the read side.

**Scope note.** Withdrawing the succession rule removed the post-sync succession hook, the
release table, the admin release form and the ambiguity-resolution UI. What remains of
that area is small and lands in 6b: one nullable column, one subtraction, a dropdown on
an existing form, and the `submit_keepers` block.

### Test gaps to close (both sessions)

`tests/test_trade_overlay.py` is the template — it already has a test-per-reader shape
(`test_my_team_gains_and_loses_the_player`,
`test_the_fpl_roster_health_check_still_reads_the_snapshot`,
`test_the_trade_does_not_invent_a_gameweek_transaction`), which is exactly the shape that
would have caught all three historical carve-outs.

Currently untested, and the reason several of these bugs survived:

- `place_on_il`'s one-active-entry guard.
- `/il/release`'s `via="waiver"` semantics — `'waived'` is written but never read anywhere.
- `reconcile_absences` — no test at all.
- Candidacy when `end_gw >= last_n` (§4.1). The test that looks like it covers this,
  `tests/test_il_keeper_visibility.py:166`, uses `end_gw=15`.
- `place_on_intl` / `return_from_intl` as service functions — the international list is
  currently only exercised as a fixture inside `test_anti_tanking.py`, which is why the
  wrong cap survived.
- **Multi-absence fixtures.** Every existing fixture has at most one absence per manager.
