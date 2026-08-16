"""League-custom rules engine.

Pure functions over already-stored (canonical) data — no DB or network access —
so the league's non-obvious rules are independently testable and never mutate
FPL-sourced rows. See CLAUDE.md for the rule definitions.
"""


class RuleViolation(Exception):
    """Raised when an admin action would break a league rule. Endpoints map this
    to HTTP 400 with the message."""


# ---- League feed identity ----
# An FPL Draft league id is only unique WITHIN a season: FPL recycles the numeric
# ids, so once our season is over `/league/{id}/details` can start returning a
# completely different league. Upserting that feed into our season row merges a
# stranger's managers, standings and fixtures into our history. Before sync writes
# to a league row that already has managers, it must prove the feed is still ours.
#
# Identity is the set of FPL `entry_id`s: a keeper league keeps the same people
# year to year, and entry_id is stable per person across seasons.
MIN_ENTRY_OVERLAP = 0.5


def verify_league_feed(
    stored_entry_ids,
    fetched_entry_ids,
    *,
    stored_season_year: int | None = None,
    fetched_season_year: int | None = None,
    min_overlap: float = MIN_ENTRY_OVERLAP,
) -> tuple[bool, str]:
    """Pure check that a fetched `/league/{id}/details` payload still describes the
    league we have stored. Returns `(ok, reason)`; `reason` is "" when ok.

    A league row with no managers yet is a fresh season row — nothing to compare,
    so anything is accepted. Otherwise the feed must (a) name the same season and
    (b) still contain at least `min_overlap` of the managers we already know.
    """
    stored = {str(e) for e in stored_entry_ids if e is not None}
    fetched = {str(e) for e in fetched_entry_ids if e is not None}

    if not stored:
        return True, ""  # fresh league row (first sync / new season)
    if not fetched:
        return False, "feed returned no league entries"

    if (
        stored_season_year is not None
        and fetched_season_year is not None
        and stored_season_year != fetched_season_year
    ):
        return False, (
            f"feed is season {fetched_season_year}, stored league is "
            f"{stored_season_year} (FPL reused league id for a new season)"
        )

    overlap = len(stored & fetched) / len(stored)
    if overlap < min_overlap:
        return False, (
            f"only {len(stored & fetched)}/{len(stored)} known managers present "
            f"in the feed ({overlap:.0%} < {min_overlap:.0%}) — this looks like a "
            f"different league"
        )
    return True, ""


# ---- League phase lifecycle ----
# Macro phases stored on the league row. In-season sub-states (discovery /
# post-trade-deadline / cup) are NOT separate macro values — they're the
# `discovery_open` flag plus values derived from the date/GW (see phase_features),
# so a stored phase can never contradict the calendar.
PHASE_OFFSEASON = "offseason"
PHASE_DRAFT = "draft"
PHASE_PRESEASON = "preseason"
PHASE_IN_SEASON = "in_season"
PHASES = (PHASE_OFFSEASON, PHASE_DRAFT, PHASE_PRESEASON, PHASE_IN_SEASON)

# Calendar anchors for in-season derived sub-states.
TRADE_DEADLINE_MONTH, TRADE_DEADLINE_DAY = 2, 1   # Feb 1: trades close
DISCOVERY_OPEN_MONTH, DISCOVERY_OPEN_DAY = 10, 1  # Oct 1: discovery window opens
CUP_START_GW = 28  # cups become available once GW28 has finished


def phase_features(
    macro: str,
    *,
    trades_off: bool = False,
    cups_available: bool = False,
    discovery_open: bool = False,
    gw_logic: bool = False,
) -> dict:
    """Pure map of a league phase -> which features are available. `macro` is one of
    PHASES; the keyword flags are the in-season derived sub-state (computed from the
    calendar/GW by services.phase_context). Returns booleans consumed by routes/nav.
    """
    if macro == PHASE_OFFSEASON:
        return {
            "trades_allowed": True, "keepers_editable": True, "draft_available": False,
            "discovery_available": False, "my_team_available": False,
            "cups_available": False, "prior_locked": True, "gw_logic_active": False,
        }
    if macro == PHASE_DRAFT:
        return {
            "trades_allowed": True, "keepers_editable": False, "draft_available": True,
            "discovery_available": False, "my_team_available": False,
            "cups_available": False, "prior_locked": True, "gw_logic_active": False,
        }
    if macro == PHASE_PRESEASON:
        return {
            "trades_allowed": True, "keepers_editable": False, "draft_available": False,
            "discovery_available": False, "my_team_available": True,
            "cups_available": False, "prior_locked": False, "gw_logic_active": False,
        }
    if macro == PHASE_IN_SEASON:
        return {
            "trades_allowed": not trades_off, "keepers_editable": False,
            "draft_available": False, "discovery_available": discovery_open,
            "my_team_available": True, "cups_available": cups_available,
            "prior_locked": False, "gw_logic_active": True,
        }
    raise ValueError(f"unknown phase {macro!r}")


def keepers_revealed(macro: str, keepers_locked: bool) -> bool:
    """Are every manager's keeper selections public yet?

    A selection is private to the manager who made it (and the commissioner) for
    exactly as long as it can still be changed, and public the moment it can't —
    otherwise you could read the league's picks and then choose your own.

    Two things end the private window: the commissioner flipping
    `leagues.keepers_locked` at the keeper deadline, or the phase leaving the
    offseason. `services.enter_draft_phase` does both, so starting the draft reveals
    them on its own.

    Deliberately the negation of `keepers_editable` rather than a list of phases, so
    reveal and editability cannot drift apart when a phase is added. The artifact of
    that: keepers aren't editable IN SEASON either, so a commissioner who enters
    someone's selection mid-season publishes it immediately. Accepted — enumerating
    phases instead would re-hide last offseason's picks the moment the season starts,
    which is worse.
    """
    return bool(keepers_locked) or not phase_features(macro)["keepers_editable"]


def next_phase(
    macro: str,
    *,
    gw38_done: bool,
    gw1_started: bool,
    today,
    season_year: int,
    discovery_open: bool,
    discovery_done: bool,
):
    """Pure auto-advance decision (no DB). Returns `(new_macro, open_discovery)` where
    `open_discovery` is True only when the Oct-1 discovery window should auto-open this
    tick (else None). Only the time/GW-driven transitions live here; admin-confirmed
    moves (offseason→draft, draft→preseason, closing discovery) are explicit elsewhere.
    """
    import datetime as _dt

    new_macro = macro
    if macro == PHASE_IN_SEASON and gw38_done:
        new_macro = PHASE_OFFSEASON            # season ended
    elif macro == PHASE_PRESEASON and gw1_started:
        new_macro = PHASE_IN_SEASON            # GW1 kicked off

    open_discovery = None
    if (
        new_macro == PHASE_IN_SEASON
        and not discovery_open
        and not discovery_done
        and today >= _dt.date(season_year, DISCOVERY_OPEN_MONTH, DISCOVERY_OPEN_DAY)
    ):
        open_discovery = True
    return new_macro, open_discovery


# How long after a PL kickoff we treat a match as "live" (90' + half-time + stoppage
# + a margin for bonus/stat settling), so we keep refreshing scores while games run.
LIVE_FIXTURE_WINDOW_HOURS = 2.5


def decide_sync(*, full_today: bool, live_fixture: bool, gw_starts_today: bool) -> str:
    """Pure sync-cadence decision → 'full' | 'live' | 'skip'. The cron fires often;
    this decides what (if anything) to actually do:
      - 'full'  : nothing has run today yet, or a GW deadline is today (capture
                  standings/schedule/lineups) — run the whole pipeline.
      - 'live'  : a PL match is in its live window now — refresh rosters/points/fixtures.
      - 'skip'  : today's full sync is done and nothing is live — do nothing.
    """
    if not full_today or gw_starts_today:
        return "full"
    if live_fixture:
        return "live"
    return "skip"


# Anti-tanking (across gameweeks): a manager is flagged when, for >= MIN_WEEKS
# consecutive gameweeks, each of those gameweeks has >= MIN_ZERO_PLAYERS rostered
# players (the entire 15-man squad, not just the XI) who recorded 0 real-match
# minutes. The specific players may differ week to week. Thresholds live here
# because the spec wording is custom/ambiguous.
ANTI_TANKING_MIN_ZERO_PLAYERS = 3
ANTI_TANKING_MIN_WEEKS = 3

# A squad must carry two goalkeepers and only one of them can be his club's starter,
# so ONE keeper at 0 minutes is roster construction, not neglect, and is forgiven.
# Every club fields a keeper every week, so TWO at zero means neither is starting
# anywhere — that is exactly what the rule is looking for, and then both count.
ANTI_TANKING_FREE_ZERO_GKS = 1


def zero_minute_count(
    player_points: list[dict],
    *,
    excused: frozenset[int] | set[int] = frozenset(),
    goalkeeper_ids: frozenset[int] | set[int] = frozenset(),
    free_zero_gks: int = ANTI_TANKING_FREE_ZERO_GKS,
) -> int:
    """Number of rostered players (whole squad) who played 0 minutes in a GW.

    `player_points` is the JSONB list stored on gameweek_points: dicts with a
    `minutes` (int) key. A missing/None minutes is treated as 0.

    `excused` holds FPL element ids that cannot be held against the manager this GW —
    a club with no fixture, and players covered by the injury or international list.
    They are removed BEFORE the goalkeeper allowance, so a backup keeper whose club is
    blank is excused as a blank and the allowance still shields the other one.

    `goalkeeper_ids` marks which element ids are keepers; up to `free_zero_gks` of them
    at 0 minutes are forgiven, all-or-nothing (see ANTI_TANKING_FREE_ZERO_GKS).

    Defaults excuse nothing, so a caller with no season context gets the raw count.
    """
    zeros = [
        p for p in (player_points or [])
        if (p.get("minutes") or 0) == 0 and p.get("fpl_id") not in excused
    ]
    gk_zeros = sum(1 for p in zeros if p.get("fpl_id") in goalkeeper_ids)
    if 0 < gk_zeros <= free_zero_gks:
        return len(zeros) - gk_zeros  # the lone non-starting keeper is forgiven
    return len(zeros)  # no keepers at zero, or more than the allowance -> all count


def tanking_windows(
    gw_zero_counts: dict[int, int],
    min_players: int = ANTI_TANKING_MIN_ZERO_PLAYERS,
    min_weeks: int = ANTI_TANKING_MIN_WEEKS,
) -> list[list[int]]:
    """Find runs of consecutive gameweeks that trip the anti-tanking rule.

    `gw_zero_counts` maps gameweek number -> count of 0-minute rostered players.
    Returns each maximal run (list of consecutive GW numbers, length >= min_weeks)
    where every GW in the run has count >= min_players. "Consecutive" means GW
    numbers differing by exactly 1 (a missing GW breaks the run).
    """
    qualifying = sorted(gw for gw, c in gw_zero_counts.items() if c >= min_players)
    windows: list[list[int]] = []
    run: list[int] = []
    for gw in qualifying:
        if run and gw == run[-1] + 1:
            run.append(gw)
        else:
            run = [gw]
        if len(run) == min_weeks:
            windows.append(run.copy())  # new qualifying window
        elif len(run) > min_weeks:
            windows[-1] = run.copy()  # extend the current window
    return windows


def is_anti_tanking_infraction(
    gw_zero_counts: dict[int, int],
    min_players: int = ANTI_TANKING_MIN_ZERO_PLAYERS,
    min_weeks: int = ANTI_TANKING_MIN_WEEKS,
) -> bool:
    return bool(tanking_windows(gw_zero_counts, min_players, min_weeks))


def current_tanking_streak(
    gw_zero_counts: dict[int, int],
    min_players: int = ANTI_TANKING_MIN_ZERO_PLAYERS,
) -> int:
    """Length of the trailing run of consecutive gameweeks (ending at the latest
    GW present) where >= min_players rostered players posted 0 minutes. Used to warn
    a manager they're approaching the anti-tanking threshold (a streak of
    min_weeks trips it). 0 = the latest GW doesn't qualify."""
    if not gw_zero_counts:
        return 0
    gws = sorted(gw_zero_counts)
    streak = 0
    prev = None
    for gw in gws:
        if gw_zero_counts.get(gw, 0) >= min_players:
            streak = streak + 1 if prev is not None and gw == prev + 1 else 1
        else:
            streak = 0
        prev = gw
    return streak


# ---- Injury list ----
# An IL'd player must stay on the IL for at least this many gameweeks before
# returning; SEASON_LAST_GW forces an automatic return at season end regardless.
MIN_IL_STAY_GWS = 4
SEASON_LAST_GW = 38

# Two diagnostic heuristics, not league rules — tune freely.
# How close to the final GW a roster gap has to start to be worth a
# commissioner's attention as a possible unrecorded IL/international absence
# (see services.unexplained_roster_gaps). A gap that started long before this
# is almost certainly just an ordinary mid-season drop.
ROSTER_GAP_REVIEW_WINDOW = 5
# How many CONSECUTIVE gameweeks a player must have held down immediately
# before the gap for it to be worth flagging. Checked against real prod data:
# without this, ordinary end-of-season streaming (a player picked up and
# dropped again within a week or two, completely routine) swamped the list —
# 53 flagged cases, nearly all with a 1-3 GW run. A genuine "established on the
# roster, then vanished" absence (the Šeško case: 36 straight GWs) looks
# nothing like that. This cuts the noise down to the cases actually worth a
# human's two minutes.
ROSTER_GAP_MIN_TENURE = 8


def il_same_position(injured_position, replacement_position) -> bool:
    """The IL replacement must play the same position as the injured player."""
    return (
        injured_position is not None
        and replacement_position is not None
        and injured_position == replacement_position
    )


def il_can_return(
    start_gw: int,
    return_gw: int,
    min_stay: int = MIN_IL_STAY_GWS,
    last_gw: int = SEASON_LAST_GW,
) -> bool:
    """Whether a player IL'd at `start_gw` may return at `return_gw`.

    Normal/waiver returns require the minimum stay (>= min_stay GWs elapsed);
    a return at or after the season's last GW is automatic and overrides it.
    """
    if return_gw >= last_gw:
        return True
    return (return_gw - start_gw) >= min_stay


# ---- Cups ----
# Seeding is fixed by H2H standings through this gameweek; the cup itself starts the
# following gameweek (GW28). Qualification: top 6 -> Cup, bottom 4 -> Pup Cup.
CUP_SEED_THROUGH_GW = 27
CUP_SIZE = 6  # top 6 -> Cup; remaining bottom 4 -> Pup Cup


def h2h_standings(results: list[tuple]) -> list:
    """Rank managers by head-to-head record. `results` is a list of finished
    matches as (home, away, home_points, away_points). Returns manager keys
    ordered best-first by (3*wins + draws) desc, then points-for desc.

    Used to seed cups from standings as of a cutoff gameweek.
    """
    from collections import defaultdict

    tbl: dict = defaultdict(lambda: {"w": 0, "d": 0, "l": 0, "pf": 0})
    for home, away, hp, ap in results:
        tbl[home]["pf"] += hp
        tbl[away]["pf"] += ap
        if hp > ap:
            tbl[home]["w"] += 1
            tbl[away]["l"] += 1
        elif ap > hp:
            tbl[away]["w"] += 1
            tbl[home]["l"] += 1
        else:
            tbl[home]["d"] += 1
            tbl[away]["d"] += 1

    def points(r: dict) -> int:
        return 3 * r["w"] + r["d"]

    return sorted(
        tbl.keys(),
        key=lambda k: (-points(tbl[k]), -tbl[k]["pf"], str(k)),
    )


def match_winner(score_a, score_b, seed_a: int, seed_b: int,
                 tiebreak_a=None, tiebreak_b=None):
    """Knockout winner: higher 2-GW total wins. Ties break by the league's cup
    tiebreakers in order — total goals, then assists, then clean sheets (team totals
    over the match, passed as `tiebreak_*` = (goals, assists, clean_sheets)) — and
    finally the better (lower) seed. Returns "a" or "b"; missing values treated as 0."""
    a, b = score_a or 0, score_b or 0
    if a != b:
        return "a" if a > b else "b"
    if tiebreak_a is not None and tiebreak_b is not None:
        for ta, tb in zip(tiebreak_a, tiebreak_b):
            if (ta or 0) != (tb or 0):
                return "a" if (ta or 0) > (tb or 0) else "b"
    return "a" if seed_a < seed_b else "b"


# ---- Payouts ----
# Percentages are of the base pot (entry_fee * num managers). Entry fee rises by
# season (25/26 $125, 26/27 $150, 27/28 $175, 28/29 $200) — override per season.
# The last-place fine and any other fines are added to the league winner.
PAYOUT_STRUCTURE = {
    "entry_fee": 125,
    "last_place_fine": 125,
    "pct": {
        "league_1": 0.40,
        "league_2": 0.15,
        "league_3": 0.05,
        "cup_1": 0.25,
        "cup_2": 0.10,
        "cup_3": 0.05,
    },
    "pup_cup_winner": 150,  # flat fallback if entrant count is unknown
    "pup_entry": 25,        # each Pup entrant pays this; winner takes the pool
    "shield_entry": 25,     # Pupmunity Shield: each of the 2 teams pays this -> winner
    "weekly_prize": 10,     # highest score each GW wins this (split on ties)
    "weekly_entry": 42.18,  # each manager's annual weekly-pool buy-in
}

# ---- Keepers ----
# Keeper state is tracked as YEARS REMAINING (imported from the league sheet):
# 0 = maxed out, can't be kept; >0 = can be kept that many more seasons.
# A draft- or trade-acquired player starts fresh with this many years remaining.
KEEPER_FRESH_DRAFT = 4
# A waiver/FA pickup starts fresh with FEWER years — one less than a draft pick.
KEEPER_FRESH_WAIVER = 3
# Of a manager's keepers, at most this many may be waiver-acquired (from 2025).
KEEPER_MAX_WAIVER = 2


KEEPER_ACQUISITIONS = ("draft", "waiver", "trade")


def keeper_status(
    started_with_manager: bool, traded_in: bool, dropped: bool, seed_remaining,
    fresh_draft: int = KEEPER_FRESH_DRAFT,
    fresh_waiver: int = KEEPER_FRESH_WAIVER,
    acquisition: str | None = None,
    traded_from: str | None = None,
) -> tuple:
    """-> (acquisition, years_remaining).
      - started_with_manager: on this manager's start-of-season (GW1) roster,
      - traded_in: arrived via a trade,
      - dropped: had a gap in this manager's tenure not covered by the IL (i.e.
        was dropped to FA and re-acquired),
      - seed_remaining: the player's imported years-remaining (None if not a prior
        keeper / acquired in-season).

    A player **dropped and re-acquired** — or any FA/waiver pickup — is flagged
    'waiver' with remaining capped at the LOWER of the prior remaining and the
    waiver fresh cap (so a dropped drafted player can't keep his full clock, and
    can't keep more than a genuine waiver pickup would get). A player held from
    the draft ('draft') or acquired by trade ('trade') carries the imported
    remaining (the draft fresh cap if none) — a full year MORE than a waiver
    pickup starts with, since a draft/trade acquisition was never on the open
    wire.

    `acquisition` is the commissioner asserting how the player was really acquired,
    overriding all of the above. It has to lift the waiver clock cap too, not just
    swap the label: the same missing evidence that mislabels a player 'waiver' also
    caps their clock, so correcting only the label would leave them short a keeper
    year they never actually lost.

    `traded_from` is the label the SENDER held this player under. A trade changes who
    owns a player and nothing else — the clock transfers unchanged (the caller passes
    the sender's remaining as `seed_remaining`) and so does the label, so a waiver
    pickup still eats one of the receiver's two waiver keeper slots. Without it a
    trade silently re-labelled the player 'trade' and reset the clock to the draft
    fresh cap, which both GAVE years to a player with fewer left and TOOK them from
    a seeded one.
    """
    if acquisition:
        fresh = fresh_waiver if acquisition == "waiver" else fresh_draft
        remaining = seed_remaining if seed_remaining is not None else fresh
        if acquisition == "waiver":
            remaining = min(remaining, fresh_waiver)
        return (acquisition, remaining)
    if dropped or (not started_with_manager and not traded_in):
        prev = seed_remaining if seed_remaining is not None else fresh_waiver
        return ("waiver", min(prev, fresh_waiver))
    if started_with_manager:
        return ("draft", seed_remaining if seed_remaining is not None else fresh_draft)
    return (traded_from or "trade",
            seed_remaining if seed_remaining is not None else fresh_draft)


def keeper_eligible(years_remaining: int) -> bool:
    """Can be kept again only if at least one keeper year remains."""
    return years_remaining > 0


# Base keeper limit per season; a valid discovery keeper raises it by one.
KEEPER_MAX_SELECTIONS = 5


def validate_keeper_selection(
    selections: list[dict],
    has_discovery_keeper: bool = False,
    max_base: int = KEEPER_MAX_SELECTIONS,
    max_waiver: int = KEEPER_MAX_WAIVER,
) -> list[str]:
    """Validate a proposed keeper set. `selections`: list of dicts with `player`
    (name), `eligible` (bool), `acquisition` ('draft'/'trade'/'waiver'), and
    `is_discovery` (bool). Returns a list of human-readable violations (empty =
    valid). Rules: at most max_base keepers (+1 with a discovery keeper); all must
    be eligible (clock < 4); at most max_waiver waiver-acquired (discovery keepers
    excluded — they come from the discovery draft, not waivers)."""
    errors = []
    limit = max_base + (1 if has_discovery_keeper else 0)
    if len(selections) > limit:
        errors.append(f"{len(selections)} keepers selected, limit is {limit}")

    # Grouped by REASON. A selection may carry one (a goalkeeper under the goalie-team
    # rule is ineligible for a reason that has nothing to do with a clock), and a
    # manager told "4-year limit / dropped" about their goalkeeper goes looking for a
    # bug that isn't there.
    by_reason: dict = {}
    for s in selections:
        if not s.get("eligible"):
            by_reason.setdefault(
                s.get("reason") or "4-year limit / dropped", []
            ).append(s["player"])
    for reason, names in by_reason.items():
        errors.append(f"ineligible ({reason}): " + ", ".join(names))

    waiver = [
        s for s in selections
        if s.get("acquisition") == "waiver" and not s.get("is_discovery")
    ]
    if len(waiver) > max_waiver:
        errors.append(
            f"{len(waiver)} waiver keepers ({', '.join(s['player'] for s in waiver)}), "
            f"max {max_waiver}"
        )

    if has_discovery_keeper and not any(s.get("is_discovery") for s in selections):
        errors.append("discovery keeper allowance used but no keeper marked discovery")
    return errors


# ---- Drafts ----
ROSTER_SIZE = 15

# FPL squad shape. Canonical FPL rules, not league-custom — the app has never enforced
# them (record_pick doesn't check position), but anything reasoning about what a squad
# will look like has to, or goalkeepers end up drafted in round 2.
SQUAD_POSITION_LIMITS = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}   # sums to ROSTER_SIZE
# The floor a starting XI has to satisfy; what a manager must still have room for when
# their remaining picks run down.
XI_POSITION_MINIMUMS = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}


# ---- Goalie teams ----
# From 2026 the league drafts a CLUB instead of individual goalkeepers: a manager
# takes one Premier League club and owns every keeper at it. A squad is then 13
# outfielders + 1 goalie team, so a manager makes 14 picks, not 15.
#
# The mode lives on `leagues` (per-season) rather than as a module constant on
# purpose. `services.get_draft_board` regenerates the slot list on EVERY read with no
# season parameter, so a global 15 -> 14 would silently truncate every archived board
# at /season/{fpl_league_id}. 'off' is the pre-2026 behaviour, unchanged.
GOALIE_TEAM_MODES = ("off", "redraft", "keeper")
GOALIE_TEAM_SLOTS = 1

# The outfield half of SQUAD_POSITION_LIMITS. Note it already sums to 13 — FPL's
# outfield shape is exactly what the new rule asks for, so nothing about outfielders
# changes; only the goalkeeper pair collapses into one club slot.
OUTFIELD_POSITIONS = ("DEF", "MID", "FWD")
OUTFIELD_POSITION_LIMITS = {p: SQUAD_POSITION_LIMITS[p] for p in OUTFIELD_POSITIONS}
OUTFIELD_XI_MINIMUMS = {p: XI_POSITION_MINIMUMS[p] for p in OUTFIELD_POSITIONS}
OUTFIELD_SQUAD_SIZE = sum(OUTFIELD_POSITION_LIMITS.values())   # 13


def goalie_teams_on(mode: str | None) -> bool:
    """Is the goalie-team rule in force for this league?"""
    return mode in ("redraft", "keeper")


def goalie_team_keepable(mode: str | None) -> bool:
    """May a goalie team be carried into next season as one of the <=5 keepers?

    Only under 'keeper'. Under 'redraft' every manager drafts a club afresh each
    year, which is the default because it needs no keeper clock at all.
    """
    return mode == "keeper"


def draft_picks_per_manager(mode: str | None, roster_size: int = ROSTER_SIZE) -> int:
    """How many draft slots a manager holds before keepers are subtracted.

    15 with the rule off; 13 outfielders + 1 goalie team = 14 with it on. This is
    what `generate_draft_slots` should be handed — it is a count of PICKS, which is
    no longer the same number as a roster size.
    """
    if not goalie_teams_on(mode):
        return roster_size
    return OUTFIELD_SQUAD_SIZE + GOALIE_TEAM_SLOTS


def generate_draft_slots(
    r1_order: list,
    reverse_order: list,
    keeper_counts: dict,
    picks_per_manager: int = ROSTER_SIZE,
    overrides: dict | None = None,
) -> list[dict]:
    """Ordered (round, manager) pick slots BEFORE any pick trades.

    Round 1 uses `r1_order` (commissioner-set); rounds 2+ use `reverse_order`
    (reverse standings). Keepers are free: a manager with K keepers makes
    picks_per_manager-K picks, i.e. holds a slot in rounds 1..(picks_per_manager-K)
    and drops out of the latest rounds once they have no slots left. Manager keys are
    opaque (ids or names). Returns dicts {round, manager} in overall pick order.

    `picks_per_manager` is deliberately NOT called `roster_size` any more: once the
    goalie-team rule is on, a manager makes 14 picks for a 15- or 16-man squad, so the
    two stopped being the same number. Callers get it from
    `draft_picks_per_manager(league.goalie_team_mode)`.

    `overrides` lets the commissioner replace the derived order for rounds 2+:
    `{None: [...]}` is a base order for every round from 2 on, and `{N: [...]}`
    overrides round N specifically. Round 1 is never overridable here — it has its
    own order already. Precedence: round override, then base, then reverse standings.

    An override is applied as-is, including one that lists a manager twice or omits
    one: the commissioner may deliberately hand a slot to someone. The keeper filter
    below still applies, so nobody picks in a round they have no roster space for.
    """
    overrides = overrides or {}
    picks_needed = {m: picks_per_manager - keeper_counts.get(m, 0) for m in r1_order}
    max_round = max(picks_needed.values(), default=0)
    slots = []
    for rnd in range(1, max_round + 1):
        if rnd == 1:
            order = r1_order
        else:
            order = overrides.get(rnd) or overrides.get(None) or reverse_order
        for m in order:
            if picks_needed.get(m, 0) >= rnd:
                slots.append({"round": rnd, "manager": m})
    return slots


_PAYOUT_LABELS = {
    "league_1": "1st place — League",
    "league_2": "2nd place — League",
    "league_3": "3rd place — League",
    "cup_1": "Cup winner",
    "cup_2": "Cup runner-up",
    "cup_3": "Cup 3rd place",
}


def compute_payouts(
    recipients: dict,
    num_managers: int,
    structure: dict = PAYOUT_STRUCTURE,
    other_fines: float = 0.0,
    fines: dict | None = None,
    pup_pool: float | None = None,
    extra: dict | None = None,
) -> dict:
    """Compute each manager's payout. `recipients` maps slot -> manager key
    (league_1/2/3, cup_1/2/3, pup_cup, last_place); missing/None slots are
    skipped. Percentage slots pay a share of the base pot; pup_cup is flat. The
    league winner (league_1) COLLECTS the pool of fines: the last-place fine, any
    `other_fines` aggregate, and the per-manager `fines` dict (manager key ->
    dollars owed); each fined manager is shown owing their fine, and last_place
    owes the last-place fine. `net` is the payout minus the buy-in (entry fee) —
    the overall winnings. Returns {manager: {"total", "net", "breakdown":[...]}}.
    """
    fines = fines or {}
    pot = structure["entry_fee"] * num_managers
    items: list[tuple] = []  # (manager, label, amount)
    for slot, pct in structure["pct"].items():
        items.append((recipients.get(slot), _PAYOUT_LABELS[slot], round(pot * pct, 2)))
    pup_amount = float(pup_pool if pup_pool is not None else structure["pup_cup_winner"])
    items.append((recipients.get("pup_cup"), "Pup Cup winner", pup_amount))

    fines_pool = sum(fines.values())
    # Both sides read the SAME figure: crediting the last-place fine to the winner while
    # nobody is resolvable as last place would pay 1st $125 out of thin air.
    last_place_owed = (
        structure["last_place_fine"] if recipients.get("last_place") is not None else 0
    )
    collected = last_place_owed + other_fines + fines_pool
    if recipients.get("league_1") is not None and collected:
        items.append((recipients["league_1"], "Fines collected", round(collected, 2)))
    if recipients.get("last_place") is not None:
        items.append((recipients["last_place"], "Last-place fine", -float(last_place_owed)))
    for mgr_key, amount in fines.items():
        if amount:
            items.append((mgr_key, "Fine(s)", -float(amount)))
    # arbitrary side-pot lines (e.g. the Pupmunity Shield): {manager: [(label, amount)]}
    for mgr_key, lines in (extra or {}).items():
        for label, amount in lines:
            items.append((mgr_key, label, float(amount)))

    out: dict = {}
    for manager, label, amount in items:
        if manager is None:
            continue
        entry = out.setdefault(manager, {"total": 0.0, "breakdown": []})
        entry["total"] = round(entry["total"] + amount, 2)
        entry["breakdown"].append({"label": label, "amount": amount})
    for entry in out.values():
        entry["net"] = round(entry["total"] - structure["entry_fee"], 2)
    return out
