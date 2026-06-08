"""League-custom rules engine.

Pure functions over already-stored (canonical) data — no DB or network access —
so the league's non-obvious rules are independently testable and never mutate
FPL-sourced rows. See CLAUDE.md for the rule definitions.
"""


class RuleViolation(Exception):
    """Raised when an admin action would break a league rule. Endpoints map this
    to HTTP 400 with the message."""


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


def zero_minute_count(player_points: list[dict]) -> int:
    """Number of rostered players (whole squad) who played 0 minutes in a GW.

    `player_points` is the JSONB list stored on gameweek_points: dicts with a
    `minutes` (int) key. A missing/None minutes is treated as 0.
    """
    return sum(1 for p in (player_points or []) if (p.get("minutes") or 0) == 0)


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
}

# ---- Keepers ----
# Keeper state is tracked as YEARS REMAINING (imported from the league sheet):
# 0 = maxed out, can't be kept; >0 = can be kept that many more seasons.
# A waiver/FA pickup starts fresh with this many years remaining.
KEEPER_FRESH_REMAINING = 2
# Of a manager's keepers, at most this many may be waiver-acquired (from 2025).
KEEPER_MAX_WAIVER = 2


def keeper_status(
    started_with_manager: bool, traded_in: bool, dropped: bool, seed_remaining,
    fresh: int = KEEPER_FRESH_REMAINING,
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
    fresh cap (so a dropped drafted player can't keep his full clock). A player
    held from the draft ('draft') or acquired by trade ('trade') carries the
    imported remaining (fresh if none)."""
    if dropped or (not started_with_manager and not traded_in):
        prev = seed_remaining if seed_remaining is not None else fresh
        return ("waiver", min(prev, fresh))
    if started_with_manager:
        return ("draft", seed_remaining if seed_remaining is not None else fresh)
    return ("trade", seed_remaining if seed_remaining is not None else fresh)


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

    ineligible = [s["player"] for s in selections if not s.get("eligible")]
    if ineligible:
        errors.append("ineligible (4-year limit / dropped): " + ", ".join(ineligible))

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


def generate_draft_slots(
    r1_order: list,
    reverse_order: list,
    keeper_counts: dict,
    roster_size: int = ROSTER_SIZE,
) -> list[dict]:
    """Ordered (round, manager) pick slots BEFORE any pick trades.

    Round 1 uses `r1_order` (commissioner-set); rounds 2+ use `reverse_order`
    (reverse standings). Keepers are free: a manager with K keepers makes
    roster_size-K picks, i.e. holds a slot in rounds 1..(roster_size-K) and drops
    out of the latest rounds once their 15-man roster is full. Manager keys are
    opaque (ids or names). Returns dicts {round, manager} in overall pick order.
    """
    picks_needed = {m: roster_size - keeper_counts.get(m, 0) for m in r1_order}
    max_round = max(picks_needed.values(), default=0)
    slots = []
    for rnd in range(1, max_round + 1):
        order = r1_order if rnd == 1 else reverse_order
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
    collected = structure["last_place_fine"] + other_fines + fines_pool
    if recipients.get("league_1") is not None and collected:
        items.append((recipients["league_1"], "Fines collected", round(collected, 2)))
    if recipients.get("last_place") is not None:
        items.append((recipients["last_place"], "Last-place fine", -float(structure["last_place_fine"])))
    for mgr_key, amount in fines.items():
        if amount:
            items.append((mgr_key, "Fine(s)", -float(amount)))

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
