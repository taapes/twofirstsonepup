"""League-custom rules engine.

Pure functions over already-stored (canonical) data — no DB or network access —
so the league's non-obvious rules are independently testable and never mutate
FPL-sourced rows. See CLAUDE.md for the rule definitions.
"""

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
