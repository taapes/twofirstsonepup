"""The discovery draft's per-pick 24h clock — pure, no DB.

House rule, confirmed with the commissioner 2026-09-09:

  1. Opens Sept 2, 10am PACIFIC LOCAL TIME every year (see test_phase_advance.py for
     that half — this file only covers the per-pick clock once the window is open).
  2. Each manager gets 24 hours once on the clock.
  3. The next clock starts at the earlier of: the previous manager actually picking,
     or their 24h expiring.
  4. A manager can't lose a pick, but the draft moves on without them.
  5. A missed slot stays fillable until the very last pick of the draft is made.

The whole point of `rules.discovery_clock` is that none of this needs a background
job — recomputing it later, even much later, gives the identical answer, which is
what makes a LATE catch-up fill (rule 5) safe: it can never move an already-passed
deadline earlier.
"""

import datetime as dt

from rules import DISCOVERY_PICK_CLOCK_HOURS, discovery_clock

T0 = dt.datetime(2026, 9, 2, 17, 0, tzinfo=dt.timezone.utc)  # an arbitrary open instant
HOUR = dt.timedelta(hours=1)
CLOCK = dt.timedelta(hours=DISCOVERY_PICK_CLOCK_HOURS)


def _c(picks=None, **kw):
    base = dict(anchor_pick=1, anchor_at=T0, picks=picks or {}, total_picks=20)
    base.update(kw)
    return discovery_clock(**base)


def test_pick_one_is_on_the_clock_from_the_open_instant():
    r = _c(now=T0 + HOUR)
    assert r == {"pick": 1, "deadline": T0 + CLOCK, "missed": []}


def test_the_deadline_is_exactly_24h_after_the_anchor():
    assert _c(now=T0)["deadline"] == T0 + CLOCK


def test_a_pick_made_on_time_passes_the_baton_immediately_not_at_the_deadline():
    """Rule 3: the earlier of the two events. A pick at +2h must hand off right then,
    not make pick 2 wait out the rest of pick 1's 24 hours."""
    r = _c(picks={1: T0 + 2 * HOUR}, now=T0 + 3 * HOUR)
    assert r["pick"] == 2
    assert r["deadline"] == T0 + 2 * HOUR + CLOCK
    assert r["missed"] == []


def test_an_expired_clock_advances_without_a_pick():
    """Rule 4: the draft moves on. Checked well past pick 1's deadline with pick 1
    still empty."""
    r = _c(now=T0 + CLOCK + HOUR)
    assert r["pick"] == 2
    assert r["deadline"] == T0 + CLOCK + CLOCK
    assert r["missed"] == [1]


def test_consecutive_expiries_keep_advancing():
    """Nobody home for a while: the clock walks forward slot by slot, none of them
    filled, each one contributing to `missed`."""
    r = _c(now=T0 + 3 * CLOCK + HOUR)
    assert r["pick"] == 4
    assert r["missed"] == [1, 2, 3]


def test_a_late_catchup_fill_does_not_move_an_already_passed_deadline():
    """The central safety property. Pick 1 expires unfilled; later it's filled LATE
    (at +30h, well past its own +24h deadline). Pick 2's deadline — fixed the moment
    pick 1's window closed — must read identically before and after that late fill.
    """
    before = _c(now=T0 + 26 * HOUR)
    after = _c(picks={1: T0 + 30 * HOUR}, now=T0 + 40 * HOUR)
    assert before["pick"] == after["pick"] == 2
    assert before["deadline"] == after["deadline"]
    assert before["missed"] == [1]
    assert after["missed"] == [], "the late fill clears it from the missed list"


def test_a_pick_filed_after_its_deadline_still_counts_as_filled(): 
    """The pick lands; whether it was in time or not, it stops being 'missed'."""
    r = _c(picks={1: T0 + 30 * HOUR}, now=T0 + 40 * HOUR)
    assert 1 not in r["missed"]


def test_missed_only_lists_slots_strictly_before_the_current_one():
    """A slot that hasn't started yet is neither 'current' nor 'missed' — it's just
    not reached."""
    r = _c(now=T0 + HOUR)
    assert r["missed"] == []
    assert r["pick"] == 1  # slots 2+ haven't started; not "missed"


def test_the_draft_completes_when_every_slot_from_the_anchor_is_filled():
    picks = {1: T0 + HOUR, 2: T0 + HOUR + 2 * HOUR}
    r = _c(picks=picks, total_picks=2, now=T0 + 5 * HOUR)
    assert r == {"pick": None, "deadline": None, "missed": []}


def test_the_draft_can_complete_even_with_missed_slots_backfilled_late():
    """Completion just means every slot from the anchor onward has an entry — it
    doesn't require every pick to have been on time."""
    picks = {1: T0 + 30 * HOUR, 2: T0 + HOUR + HOUR}  # 1 was late, 2 was on time
    r = _c(picks=picks, total_picks=2, now=T0 + 40 * HOUR)
    assert r["pick"] is None and r["missed"] == []


# ---- the anchor cutover — the live 2026 bootstrap this feature ships into --------
def test_a_pick_before_the_anchor_is_invisible_to_the_clock():
    """The real 2026 case: pick 1 (Kevin S) happened before the clock rule existed.
    Setting anchor_pick=2 must make pick 1 simply never asked about — no deadline
    backdated onto it, and it never appears in `missed` even though it has no
    picked_at recorded at all."""
    r = discovery_clock(anchor_pick=2, anchor_at=T0, picks={}, total_picks=20,
                        now=T0 + dt.timedelta(minutes=1))
    assert r["pick"] == 2
    assert r["deadline"] == T0 + CLOCK
    assert r["missed"] == [], "pick 1 must not appear — it's before the anchor"


def test_the_anchor_pick_itself_gets_the_full_fresh_window():
    """Kevin T's clock starts exactly at the anchor instant, not backdated to
    whenever pick 1 (which the anchor skips past) happened to be made."""
    r = discovery_clock(anchor_pick=2, anchor_at=T0, picks={}, total_picks=20,
                        now=T0 + CLOCK - dt.timedelta(minutes=1))
    assert r["pick"] == 2, "still within the fresh 24h window, one minute from expiry"
