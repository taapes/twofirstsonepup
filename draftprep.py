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
    OUTFIELD_POSITION_LIMITS,
    OUTFIELD_SQUAD_SIZE,
    OUTFIELD_XI_MINIMUMS,
    ROSTER_SIZE,
    SQUAD_POSITION_LIMITS,
    XI_POSITION_MINIMUMS,
    goalie_teams_on,
)


class Shape:
    """The squad a draft is being simulated INTO — positions, limits and weights.

    A parameter rather than module constants because the league now has two squad
    shapes and both have to be simulable: the FPL fifteen, and the goalie-team
    thirteen-plus-a-club. The archived seasons were drafted under the first one and
    have to keep simulating that way.

    `starter_demand` is where the replacement baseline is pegged — NOT at `limits`.
    Under the FPL shape, 2 GKP x 10 managers puts the baseline on the 21st
    goalkeeper, which in a 10-team league is exactly where projected minutes fall off
    a cliff (18 keepers project 109+, the 19th projects 72, the 23rd projects 6). A
    baseline sitting on a cliff swings 50 points when one club changes its depth
    chart. At starter depth every baseline sits on a flat part of its curve.

    `slot_weights` is what each successive squad slot at a position is worth as a
    fraction of a starter, one entry per slot in `limits`. The second goalkeeper is a
    bench-warmer; the fourth defender rotates in. Without these a naive model keeps 13
    goalkeepers across the league, because it scores a backup keeper's projected season
    points in full even though he'll never be picked.

    `reserved_slots` is how many of a manager's picks are NOT spent on a player from
    this pool — one, under the goalie-team rule, for the club.
    """

    __slots__ = ("positions", "limits", "xi_minimums", "squad_size",
                 "starter_demand", "slot_weights", "reserved_slots")

    def __init__(self, limits, xi_minimums, squad_size, starter_demand, slot_weights,
                 reserved_slots=0):
        self.positions = tuple(limits)
        self.limits = limits
        self.xi_minimums = xi_minimums
        self.squad_size = squad_size
        self.starter_demand = starter_demand
        self.slot_weights = slot_weights
        self.reserved_slots = reserved_slots


FPL_SHAPE = Shape(
    limits=SQUAD_POSITION_LIMITS,
    xi_minimums=XI_POSITION_MINIMUMS,
    squad_size=ROSTER_SIZE,
    starter_demand={"GKP": 1, "DEF": 4, "MID": 4, "FWD": 2},   # sums to 11, the XI
    slot_weights={
        "GKP": (1.0, 0.15),
        "DEF": (1.0, 1.0, 1.0, 0.75, 0.20),
        "MID": (1.0, 1.0, 1.0, 0.75, 0.20),
        "FWD": (1.0, 0.75, 0.20),
    },
)

# Goalkeepers leave the model entirely: you don't draft one, you draft a club, and
# clubs are valued separately (see goalie_team_values). What's left is thirteen
# outfielders and one reserved slot, and a ten-man outfield XI to peg the baseline to.
GOALIE_TEAM_SHAPE = Shape(
    limits=OUTFIELD_POSITION_LIMITS,
    xi_minimums=OUTFIELD_XI_MINIMUMS,
    squad_size=OUTFIELD_SQUAD_SIZE,
    starter_demand={"DEF": 4, "MID": 4, "FWD": 2},   # sums to 10, the outfield XI
    slot_weights={
        "DEF": (1.0, 1.0, 1.0, 0.75, 0.20),
        "MID": (1.0, 1.0, 1.0, 0.75, 0.20),
        "FWD": (1.0, 0.75, 0.20),
    },
    reserved_slots=1,
)


def shape_for(mode) -> Shape:
    """The squad shape a league with this goalie_team_mode drafts into."""
    return GOALIE_TEAM_SHAPE if goalie_teams_on(mode) else FPL_SHAPE


# Back-compat aliases for the FPL shape. Prefer `shape.positions` etc. at call sites
# that know their league — these are the defaults, not the truth.
STARTER_DEMAND = FPL_SHAPE.starter_demand
SLOT_WEIGHTS = FPL_SHAPE.slot_weights
POSITIONS = FPL_SHAPE.positions


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


def replacement_levels(pool, *, teams=10, demand=None, shape=FPL_SHAPE):
    """{position: points of the best freely-available player there}.

    The (demand x teams)-th best is the last one who gets started somewhere, so the
    NEXT one is what you can always get. Returns the level plus the index and pool
    size used, because a baseline is only trustworthy if you can see where it landed.
    """
    demand = demand or shape.starter_demand
    out, diag = {}, {}
    for pos in shape.positions:
        pts = sorted((r.points for r in pool if r.position == pos), reverse=True)
        if not pts:
            out[pos], diag[pos] = 0.0, {"index": None, "pool": 0}
            continue
        idx = min(demand.get(pos, 0) * teams, len(pts) - 1)
        out[pos] = pts[idx]
        diag[pos] = {"index": idx + 1, "pool": len(pts)}
    return out, diag


def squad_value(members, replacement, *, shape=FPL_SHAPE):
    """Saturation-weighted value of a set of players.

    Best-first within each position, each slot scaled by the shape's slot weights.
    Players beyond the positional limit contribute nothing — which is the point: they
    wouldn't be rostered. A player below replacement contributes a negative, so padding
    a set with filler makes it worse, not neutral.
    """
    total = 0.0
    for pos in shape.positions:
        got = sorted((m.points for m in members if m.position == pos), reverse=True)
        for i, pts in enumerate(got[: shape.limits[pos]]):
            total += shape.slot_weights[pos][i] * (pts - replacement[pos])
    return total


def player_values(pool, replacement):
    """{player_id: value over replacement at his position} — the big-board ranking."""
    return {r.player_id: r.points - replacement[r.position] for r in pool}


def goalie_team_values(clubs, *, teams=10):
    """(values, replacement) for goalie teams — the club big board.

    `clubs` are Recs whose `points` is the club's AGGREGATE projected keeper points,
    because the whole keeper room is what a manager gets. Same value-over-replacement
    idea as players, with the baseline at the best club still available once every
    manager has one.

    Deliberately NOT a position inside `simulate_draft`. Twenty clubs for ten managers
    is no scarcity and no depth curve to saturate, so a simulated "gone by pick N" for
    clubs would be noise dressed up as a prediction. A ranking is honest; a draft
    simulation of them would not be.
    """
    pts = sorted((c.points for c in clubs), reverse=True)
    if not pts:
        return {}, 0.0
    rep = pts[min(teams, len(pts) - 1)]
    return {c.player_id: c.points - rep for c in clubs}, rep


def predict_keepers(candidates, replacement, *, max_keepers=KEEPER_MAX_SELECTIONS,
                    max_waiver=KEEPER_MAX_WAIVER, shape=FPL_SHAPE):
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
            val = squad_value(combo, replacement, shape=shape)
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
    return {p: limits[p] - counts.get(p, 0) for p in limits}


def simulate_draft(slots, available, rosters, replacement, *, shape=FPL_SHAPE,
                   reserved_spent=None, roster_size=None):
    """Walk the pick slots and hand each manager the best player they can still use.

    `slots` is [{"pick", "round", "manager"}] in order, already resolved to the manager
    who OWNS the pick (trades applied). `rosters` is {manager: [Rec]} seeded with each
    manager's predicted keepers — seeding matters, because a manager who keeps three
    midfielders has only two midfield slots left.

    Four rules keep it honest:
      - a manager never exceeds the shape's positional limits;
      - once his squad reaches the shape's squad size his remaining slots are
        FORFEITED and the players in them stay in the pool for everyone else;
      - when his remaining picks equal his unmet XI minimums he may only take positions
        he still needs. Without that last rule the simulation happily spends every pick
        on the highest-value forward available and leaves a manager unable to field a
        legal XI;
      - `shape.reserved_slots` of his picks go on something that isn't in this pool —
        the goalie team — and are neither filled nor counted as picks he can spend.

    Which slot is the reserved one is an ASSUMPTION, not a prediction: we take his
    last. A manager who grabs a club in round 3 instead shifts the pick numbers around
    him, and no model can know when he'll do it. The count is what matters, and the
    count is right either way.

    `reserved_spent` is the set of managers who have ALREADY taken their goalie team —
    live mode, where it's a fact rather than a forecast. Without it the simulation
    holds back a slot they no longer owe and hands them one outfielder too few.
    """
    if roster_size is not None:      # legacy positional override, kept for callers
        shape = Shape(shape.limits, shape.xi_minimums, roster_size,
                      shape.starter_demand, shape.slot_weights, shape.reserved_slots)
    positions = shape.positions
    values = player_values(available, replacement)
    pool = sorted(available, key=lambda r: _sort_key(r, values))
    squads = {m: list(v) for m, v in rosters.items()}
    counts = {
        m: {p: sum(1 for r in v if r.position == p) for p in positions}
        for m, v in squads.items()
    }

    # Each manager's last `reserved_slots` picks are the goalie team. Held out of
    # `remaining` too, or the XI-minimums rule below would think he has one more
    # outfield pick than he really does and let him leave a hole in his XI.
    reserved_picks = set()
    if shape.reserved_slots:
        spent = set(reserved_spent or ())
        by_manager: dict = {}
        for s in slots:
            by_manager.setdefault(s["manager"], []).append(s["pick"])
        for manager, picks in by_manager.items():
            if manager in spent:
                continue
            reserved_picks.update(picks[-shape.reserved_slots:])

    remaining = {}
    for s in slots:
        if s["pick"] in reserved_picks:
            continue
        remaining[s["manager"]] = remaining.get(s["manager"], 0) + 1

    out = []
    for s in slots:
        m = s["manager"]
        row = {"pick": s["pick"], "round": s["round"], "manager": m,
               "player": None, "value": None, "reason": None}
        if s["pick"] in reserved_picks:
            row["reason"] = "goalie team"
            row["alternatives"] = []
            out.append(row)
            continue
        remaining[m] = remaining.get(m, 0) - 1
        if len(squads.setdefault(m, [])) >= shape.squad_size:
            row["reason"] = "forfeited"   # squad already full; the slot lapses
            out.append(row)
            continue
        have = counts.setdefault(m, {p: 0 for p in positions})
        room = [p for p in positions if have[p] < shape.limits[p]]
        unmet = [p for p in positions if have[p] < shape.xi_minimums[p]]
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
