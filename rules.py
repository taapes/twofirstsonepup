"""League-custom rules engine.

Pure functions over already-stored (canonical) data — no DB or network access —
so the league's non-obvious rules are independently testable and never mutate
FPL-sourced rows. See CLAUDE.md for the rule definitions.
"""

# Anti-tanking: a manager is flagged when their starting XI for a gameweek
# contains a run of this many consecutive 0-minute players (ordered by lineup
# position). Tunable in one place because the spec wording is ambiguous.
ANTI_TANKING_MIN_RUN = 3


def longest_zero_minute_run(player_points: list[dict]) -> int:
    """Longest run of consecutive STARTING players with 0 minutes.

    `player_points` is the JSONB list stored on gameweek_points: dicts with
    `is_starting` (bool) and `minutes` (int), ordered by lineup position.
    Bench players (is_starting False) are ignored, not counted as a break —
    they sort after the XI, so they never interleave the starters anyway.
    """
    longest = run = 0
    for p in player_points:
        if not p.get("is_starting"):
            continue
        if (p.get("minutes") or 0) == 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def is_anti_tanking_infraction(
    player_points: list[dict], min_run: int = ANTI_TANKING_MIN_RUN
) -> bool:
    """True if the lineup trips the anti-tanking threshold."""
    return longest_zero_minute_run(player_points) >= min_run
