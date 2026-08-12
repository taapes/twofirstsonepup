"""Draft preparation model — predict keepers, then who's left and when they go.

Deliberately NOT in rules.py. Everything there is a codified league rule that the app
enforces; this is a heuristic that estimates what ten humans will choose. Keeping the
two apart means nobody later mistakes a model output for a rule.

Pure: plain records in, plain dicts out, no database and no imports from services.

The model in one paragraph. Rank a player by how much he beats a freely-available
player at his own position (value over replacement), where "freely available" is
pegged to STARTER depth rather than roster depth — because a second goalkeeper never
plays and so is worth almost nothing, which no single scalar baseline can express on
its own. Saturation weights carry the rest of that idea: within a squad, the first
keeper counts fully and the second barely counts at all. A manager's keeper set is
then the legal set maximising that squad value, and the draft is simulated by walking
the real slot order and having each manager take the best player they still have room
for.
"""

import itertools

from rules import (
    KEEPER_MAX_SELECTIONS,
    KEEPER_MAX_WAIVER,
    ROSTER_SIZE,
    SQUAD_POSITION_LIMITS,
    XI_POSITION_MINIMUMS,
)

# How many of each position a manager actually STARTS. The replacement baseline is
# pegged here, not to SQUAD_POSITION_LIMITS: with 2 GKP x 10 managers the baseline
# lands on the 21st goalkeeper, which in a 10-team league is the point where projected
# minutes fall off a cliff (18 keepers project 109+, the 19th projects 72, the 23rd
# projects 6). A baseline sitting on a cliff swings 50 points when one club changes its
# depth chart. At starter depth every baseline sits on a flat part of its curve.
STARTER_DEMAND = {"GKP": 1, "DEF": 4, "MID": 4, "FWD": 2}   # sums to 11, the XI

# What each successive squad slot at a position is worth, as a fraction of a starter.
# One entry per slot in SQUAD_POSITION_LIMITS. The second goalkeeper is a bench-warmer;
# the fourth defender rotates in. Without these a naive model keeps 13 goalkeepers
# across the league, because it scores a backup keeper's projected season points in
# full even though he'll never be picked.
SLOT_WEIGHTS = {
    "GKP": (1.0, 0.15),
    "DEF": (1.0, 1.0, 1.0, 0.75, 0.20),
    "MID": (1.0, 1.0, 1.0, 0.75, 0.20),
    "FWD": (1.0, 0.75, 0.20),
}

POSITIONS = tuple(SQUAD_POSITION_LIMITS)


class Rec:
    """One player, as the model sees him. Frozen and comparable so ordering is total."""

    __slots__ = ("player_id", "name", "position", "points", "acquisition", "eligible")

    def __init__(self, player_id, name, position, points, acquisition=None, eligible=False):
        self.player_id = player_id
        self.name = name
        self.position = position
        self.points = float(points)
        self.acquisition = acquisition
        self.eligible = eligible

    def __repr__(self):  # pragma: no cover - debugging only
        return f"<Rec {self.name} {self.position} {self.points}>"


def _sort_key(rec, value_by_id):
    """Total and explicit, so the simulation is deterministic under any input order.
    Two players with identical value and points must not swap between runs."""
    return (-value_by_id[rec.player_id], -rec.points, rec.name, str(rec.player_id))


def replacement_levels(pool, *, teams=10, demand=None):
    """{position: points of the best freely-available player there}.

    The (demand x teams)-th best is the last one who gets started somewhere, so the
    NEXT one is what you can always get. Returns the level plus the index and pool
    size used, because a baseline is only trustworthy if you can see where it landed.
    """
    demand = demand or STARTER_DEMAND
    out, diag = {}, {}
    for pos in POSITIONS:
        pts = sorted((r.points for r in pool if r.position == pos), reverse=True)
        if not pts:
            out[pos], diag[pos] = 0.0, {"index": None, "pool": 0}
            continue
        idx = min(demand.get(pos, 0) * teams, len(pts) - 1)
        out[pos] = pts[idx]
        diag[pos] = {"index": idx + 1, "pool": len(pts)}
    return out, diag


def squad_value(members, replacement):
    """Saturation-weighted value of a set of players.

    Best-first within each position, each slot scaled by SLOT_WEIGHTS. Players beyond
    the positional limit contribute nothing — which is the point: they wouldn't be
    rostered. A player below replacement contributes a negative, so padding a set with
    filler makes it worse, not neutral.
    """
    total = 0.0
    for pos in POSITIONS:
        got = sorted((m.points for m in members if m.position == pos), reverse=True)
        for i, pts in enumerate(got[: SQUAD_POSITION_LIMITS[pos]]):
            total += SLOT_WEIGHTS[pos][i] * (pts - replacement[pos])
    return total


def player_values(pool, replacement):
    """{player_id: value over replacement at his position} — the big-board ranking."""
    return {r.player_id: r.points - replacement[r.position] for r in pool}


def predict_keepers(candidates, replacement, *, max_keepers=KEEPER_MAX_SELECTIONS,
                    max_waiver=KEEPER_MAX_WAIVER):
    """The legal keeper set a manager most likely picks, by squad value.

    Brute force over every legal subset. C(15,5) = 3003, so this is microseconds — and
    it is *correct*, which greedy is not: greedy is provably optimal for a separable
    objective over this constraint shape (a uniform matroid intersected with a laminar
    partition matroid), but squad_value is submodular because of the saturation
    weights, and greedy has no guarantee there. See the test that constructs a case
    where they disagree.

    Returns the chosen set, its value, the `margin` over the next-best legal set (a
    thin margin means the manager could easily go the other way), and which constraint
    bound. Only ELIGIBLE candidates are considered; the caller filters out anyone who
    has left the Premier League, since they can't be kept at all.
    """
    pool = [c for c in candidates if c.eligible]
    best, best_val, runner_up = (), float("-inf"), float("-inf")
    for size in range(0, min(max_keepers, len(pool)) + 1):
        for combo in itertools.combinations(pool, size):
            waivers = sum(1 for c in combo if c.acquisition == "waiver")
            if waivers > max_waiver:
                continue
            val = squad_value(combo, replacement)
            if val > best_val:
                best, best_val, runner_up = combo, val, best_val
            elif val > runner_up:
                runner_up = val
    chosen = sorted(best, key=lambda c: (-c.points, c.name))
    binding = []
    if len(chosen) == max_keepers:
        binding.append("count")
    if sum(1 for c in chosen if c.acquisition == "waiver") == max_waiver:
        binding.append("waiver")
    return {
        "keepers": chosen,
        "value": best_val if best_val > float("-inf") else 0.0,
        # How much better the chosen set is than the next-best legal one. Near zero
        # means a coin flip, so those players may well hit the draft pool after all.
        "margin": (best_val - runner_up) if runner_up > float("-inf") else None,
        "binding": binding,
    }


def _need(counts, limits):
    return {p: limits[p] - counts.get(p, 0) for p in POSITIONS}


def simulate_draft(slots, available, rosters, replacement, *, roster_size=ROSTER_SIZE):
    """Walk the pick slots and hand each manager the best player they can still use.

    `slots` is [{"pick", "round", "manager"}] in order, already resolved to the manager
    who OWNS the pick (trades applied). `rosters` is {manager: [Rec]} seeded with each
    manager's predicted keepers — seeding matters, because a manager who keeps three
    midfielders has only two midfield slots left.

    Three rules keep it honest:
      - a manager never exceeds SQUAD_POSITION_LIMITS;
      - once his squad reaches `roster_size` his remaining slots are FORFEITED and the
        players in them stay in the pool for everyone else;
      - when his remaining picks equal his unmet XI minimums he may only take positions
        he still needs. Without that last rule the simulation happily spends every pick
        on the highest-value outfielder available and leaves a manager with no
        goalkeeper, unable to field a legal XI.
    """
    values = player_values(available, replacement)
    pool = sorted(available, key=lambda r: _sort_key(r, values))
    squads = {m: list(v) for m, v in rosters.items()}
    counts = {
        m: {p: sum(1 for r in v if r.position == p) for p in POSITIONS}
        for m, v in squads.items()
    }
    remaining = {}
    for s in slots:
        remaining[s["manager"]] = remaining.get(s["manager"], 0) + 1

    out = []
    for s in slots:
        m = s["manager"]
        remaining[m] -= 1
        row = {"pick": s["pick"], "round": s["round"], "manager": m,
               "player": None, "value": None, "reason": None}
        if len(squads.setdefault(m, [])) >= roster_size:
            row["reason"] = "forfeited"   # squad already full; the slot lapses
            out.append(row)
            continue
        have = counts.setdefault(m, {p: 0 for p in POSITIONS})
        room = [p for p in POSITIONS if have[p] < SQUAD_POSITION_LIMITS[p]]
        unmet = [p for p in POSITIONS if have[p] < XI_POSITION_MINIMUMS[p]]
        # +1 because `remaining` was already decremented for the pick being made now
        if unmet and len(unmet) >= remaining[m] + 1:
            room = [p for p in unmet if p in room] or room
        usable = [r for r in pool if r.position in room]
        # What else was on the board at this slot, in the order this manager would
        # have considered them. Recorded here rather than recomputed later because
        # only the simulation knows the pool state at each pick.
        row["alternatives"] = usable[1:6]
        if not usable:
            row["reason"] = "no eligible player"
            out.append(row)
            continue
        pick = usable[0]
        pool.remove(pick)
        squads[m].append(pick)
        have[pick.position] += 1
        row.update({"player": pick, "value": values[pick.player_id],
                    "reason": "need" if pick.position in unmet else None})
        out.append(row)
    return {"picks": out, "squads": squads, "undrafted": pool}
