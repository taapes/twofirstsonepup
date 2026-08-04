"""v2 in-app scoring engine (pure).

The league's own GW-scoring math, independent of FPL's precomputed team totals:
given a manager's submitted lineup (starting XI + ordered bench) and each player's
real-match minutes/points/stats, apply FPL Draft auto-substitutions and sum the
resolved XI. Pure functions over already-fetched data — no DB or network — so the
engine is unit-testable and can be validated against FPL's numbers before we trust
it (see the v2 roadmap). FPL stays the source of raw player points/minutes; the
lineup and the resulting score become app-owned.

FPL Draft rules encoded here: 15-man squad, 11 starters in a legal formation
(exactly 1 GK; 3-5 DEF; 2-5 MID; 1-3 FWD), 4 ordered bench subs, auto-subs for
starters who record 0 minutes, no captain and no chips.

Position codes match `players.position` / `services._POSITION_ORDER`:
GKP, DEF, MID, FWD.
"""

from collections import Counter

XI_SIZE = 11
SQUAD_SIZE = 15
GK = "GKP"

# (min, max) count per position in a legal starting XI.
FORMATION = {
    "GKP": (1, 1),
    "DEF": (3, 5),
    "MID": (2, 5),
    "FWD": (1, 3),
}


def legal_formation(positions: list[str]) -> bool:
    """True if a list of 11 position codes is a legal FPL Draft formation:
    exactly 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD, 11 total."""
    if len(positions) != XI_SIZE:
        return False
    counts = Counter(positions)
    if sum(counts.values()) != XI_SIZE:
        return False
    return all(lo <= counts.get(pos, 0) <= hi for pos, (lo, hi) in FORMATION.items())


def apply_auto_subs(
    starters: list,
    bench: list,
    pos_by_pid: dict,
    minutes_by_pid: dict,
) -> list:
    """Resolve the XI that actually scores after auto-substitutions.

    `starters` is the 11 submitted starters (order preserved); `bench` the 4 subs
    in the manager's chosen priority order. A starter who played 0 minutes is
    replaced by the earliest bench player (in bench order) whose introduction keeps
    the formation legal. The goalkeeper is a special case — only the bench GK can
    replace the starting GK. A non-playing starter with no legal replacement simply
    stays in (and scores 0), matching FPL. Returns the resolved list of 11 pids.
    """
    def played(pid) -> bool:
        return (minutes_by_pid.get(pid) or 0) > 0

    xi = list(starters)
    used: set = set()

    # Goalkeeper: bench GK for starting GK only.
    for i, pid in enumerate(xi):
        if pos_by_pid.get(pid) == GK and not played(pid):
            for b in bench:
                if b not in used and pos_by_pid.get(b) == GK and played(b):
                    xi[i] = b
                    used.add(b)
                    break
            break  # exactly one GK slot

    # Outfield: bench players in priority order fill non-playing starters, but only
    # when the resulting formation stays legal.
    for b in bench:
        if b in used or pos_by_pid.get(b) == GK or not played(b):
            continue
        for i, s in enumerate(xi):
            if pos_by_pid.get(s) == GK or played(s):
                continue
            trial = list(xi)
            trial[i] = b
            if legal_formation([pos_by_pid.get(p) for p in trial]):
                xi[i] = b
                used.add(b)
                break

    return xi


def score_lineup(starters: list, bench: list, players_by_pid: dict) -> dict:
    """Score a manager's lineup for one gameweek.

    `players_by_pid[pid]` is a dict with keys `pos` and (optional, default 0)
    `minutes`, `points`, `goals`, `assists`, `clean_sheets`. Applies auto-subs and
    sums the resolved XI. `team_*` totals (over the resolved XI) mirror the cup
    tiebreak fields on `gameweek_points`. Returns
    `{total, team_goals, team_assists, team_clean_sheets, resolved_xi}`.
    """
    pos_by = {p: players_by_pid[p]["pos"] for p in (*starters, *bench)}
    mins_by = {p: (players_by_pid[p].get("minutes") or 0) for p in (*starters, *bench)}
    xi = apply_auto_subs(starters, bench, pos_by, mins_by)

    def s(pid, key):
        return players_by_pid[pid].get(key) or 0

    return {
        "total": sum(s(p, "points") for p in xi),
        "team_goals": sum(s(p, "goals") for p in xi),
        "team_assists": sum(s(p, "assists") for p in xi),
        "team_clean_sheets": sum(s(p, "clean_sheets") for p in xi),
        "resolved_xi": xi,
    }


def h2h_result(home_total, away_total) -> str:
    """Head-to-head outcome for one match: 'home', 'away', or 'draw'. Unlike the
    knockout `rules.match_winner`, league H2H allows draws. League standings are
    built from these via `rules.h2h_standings`."""
    h, a = home_total or 0, away_total or 0
    if h > a:
        return "home"
    if a > h:
        return "away"
    return "draw"
