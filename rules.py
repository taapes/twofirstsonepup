"""League-custom rules engine.

Pure functions over already-stored (canonical) data — no DB or network access —
so the league's non-obvious rules are independently testable and never mutate
FPL-sourced rows. See CLAUDE.md for the rule definitions.
"""


class RuleViolation(Exception):
    """Raised when an admin action would break a league rule. Endpoints map this
    to HTTP 400 with the message."""

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
# Seeding is fixed by H2H standings through this gameweek (cups start after it).
CUP_SEED_THROUGH_GW = 28
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


def match_winner(score_a, score_b, seed_a: int, seed_b: int):
    """Knockout winner: higher 2-GW total wins; ties break to the better (lower)
    seed. Returns "a" or "b". Treats missing scores as 0."""
    a, b = score_a or 0, score_b or 0
    if a != b:
        return "a" if a > b else "b"
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
    "pup_cup_winner": 150,  # flat, not a percentage
}

# ---- Keepers ----
# A player can be kept at most this many seasons; in the (MAX+1)th year they must
# return to the draft. Dropping to FA/waivers resets the clock.
KEEPER_MAX_YEARS = 4
# Of a manager's keepers, at most this many may be waiver-acquired (from 2025).
KEEPER_MAX_WAIVER = 2


def keeper_continuity(presence_gws: set, il_gws: set, last_gw: int) -> dict | None:
    """Whether a player was held continuously through the season end.

    `presence_gws`: GWs the player was on the manager's roster. `il_gws`: GWs he
    was on that manager's injury list (an explained absence). Returns None if he
    isn't on the roster at `last_gw` (not a keeper candidate), else the first GW
    held and whether continuous (gaps allowed only when covered by the IL).
    A non-continuous hold means he was dropped and re-acquired -> clock resets.
    """
    if last_gw not in presence_gws:
        return None
    first = min(presence_gws)
    for gw in range(first, last_gw + 1):
        if gw not in presence_gws and gw not in il_gws:
            return {"first_gw": first, "continuous": False}
    return {"first_gw": first, "continuous": True}


def classify_acquisition(first_gw: int, traded_in: bool, continuous: bool) -> str:
    """How the player came to the manager: 'draft' (rostered from GW1), 'trade'
    (arrived via a trade), or 'waiver' (arrived mid-season, or re-acquired after a
    drop). Only waiver keepers count toward KEEPER_MAX_WAIVER."""
    if not continuous:
        return "waiver"
    if traded_in:
        return "trade"
    return "draft" if first_gw == 1 else "waiver"


def keeper_years_used(prior_years, continuous: bool, was_kept_this_season: bool) -> int:
    """Seasons the player has been kept entering the next selection. `prior_years`
    is the Option-B seed (None if never a keeper before). A dropped (non-continuous)
    player resets to 0. A continuously-held player who was a keeper this season
    (has a seed) counts seed+1; one drafted fresh this season counts 0."""
    if not continuous:
        return 0
    if was_kept_this_season:
        return (prior_years or 0) + 1
    return 0


def keeper_eligible(years_used: int, max_years: int = KEEPER_MAX_YEARS) -> bool:
    """Eligible to be kept again only if kept fewer than max_years seasons."""
    return years_used < max_years


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
) -> dict:
    """Compute each manager's payout. `recipients` maps slot -> manager key
    (league_1/2/3, cup_1/2/3, pup_cup, last_place); missing/None slots are
    skipped. Percentage slots pay a share of the base pot; pup_cup is flat; the
    last-place fine (+ other fines) is added to league_1, and last_place is shown
    owing the fine. Returns {manager: {"total", "breakdown":[{label, amount}]}}.
    """
    pot = structure["entry_fee"] * num_managers
    items: list[tuple] = []  # (manager, label, amount)
    for slot, pct in structure["pct"].items():
        items.append((recipients.get(slot), _PAYOUT_LABELS[slot], round(pot * pct, 2)))
    items.append((recipients.get("pup_cup"), "Pup Cup winner", float(structure["pup_cup_winner"])))

    bonus = structure["last_place_fine"] + other_fines
    if recipients.get("league_1") is not None and bonus:
        items.append((recipients["league_1"], "Last-place fine + other fines", round(bonus, 2)))
    if recipients.get("last_place") is not None:
        items.append((recipients["last_place"], "Last-place fine", -float(structure["last_place_fine"])))

    out: dict = {}
    for manager, label, amount in items:
        if manager is None:
            continue
        entry = out.setdefault(manager, {"total": 0.0, "breakdown": []})
        entry["total"] = round(entry["total"] + amount, 2)
        entry["breakdown"].append({"label": label, "amount": amount})
    return out
