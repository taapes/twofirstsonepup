# FPL Draft Keeper — User Guide

Two sites run the same app:

- **Live site** (production) — the real league. What's available changes with the
  **season phase**; right now we're in the **off-season**.
- **Demo site** (sandbox) — a copy of the league data where **every feature is
  unlocked** so you can try anything. Changes here never affect the live site, and the
  demo may be reset at any time.

---

## TL;DR

**Live (now — off-season):** sign in with your team password → set/change **keepers**,
make **trades**, and browse standings, **winnings/payouts**, **transactions**, past
**seasons/history**, and **future picks**. Draft, discovery, My Team, cups, and the live
scoreboard are hidden until their part of the season.

**Demo (anytime):** click your team to jump in — **no password** — and use **everything
at once**: draft + discovery boards (with autodraft queues), trades, keepers, My Team
stats, injury & AFCON lists, cups, scoreboard, payouts. It's a safe sandbox.

---

## Signing in

- Go to the site → **"Who are you?"** → pick your team.
  - **Live:** enter your password. First time, you set one. Forgot it? Ask the
    commissioner to reset it, then set a new one.
  - **Demo:** clicking your team logs you straight in — no password.
- **Commissioner:** the **Admin** button (password login). Admin can do anything and
  act for any team.
- You can only edit **your own team** (admin excepted). Use **Log out** to switch teams.

---

## Live site — what you can do right now (off-season)

Everyone (after signing in):

- **Home** — league standings (your row highlighted), **Winnings** (league/cup/pup
  payouts, the weekly pool, fines, and your overall net vs. buy-in), anti-tanking
  **flags**, the **injury list**, and any **ineligible players**.
- **Keepers** — select or change your keepers for next season. Up to **5** (a 6th if you
  have a discovery keeper), at most **2** waiver-acquired; the discovery slot can be any
  player. Editable all off-season.
- **Trades** — propose a trade (you must be one of the two teams). Any players + future
  picks, no cap.
- **Transactions** — weekly add/drops across the season.
- **Teams** — every manager's roster + their locked keepers.
- **History / Seasons** — past champions, and a read-only view of any past season's
  standings, winnings, and cups.
- **Picks** — the future draft-pick grid (who owns which future picks).

Hidden until their phase opens: **My Team / Upcoming** (pre-season on), **Draft**
(draft phase), **Discovery** (October), **Cups** (after GW28), **Scores** (in-season).

### Commissioner (admin) — live

- **Adjust** — manual standings deltas, **fines**, and **side pots** (team-sale clause,
  ad-hoc; the weekly pool is automatic).
- **Cups** — generate + score the Cup/Pup brackets, the **Pupmunity Shield**, and a
  per-match score override (for double-gameweeks).
- **Health** — data checks, **lock** toggles, **manager password resets**, and the
  **league phase** controls (start draft, close discovery, set/pin the phase).
- **Season rollover** (`/admin/season`) — at the new season, point the app at the new
  FPL draft league; it carries forward logins + keeper clocks and snapshots the player pool.

### How the season unfolds (live)

The site changes itself as the year progresses:

1. **Off-season** (now) — keepers + trades open; last season shown, locked.
2. **Draft** (admin starts) — keepers lock, the **draft board** opens.
3. **Pre-season** (new league synced) — **My Team / Upcoming** appear.
4. **Season** (GW1) — live scores; the site updates around match times.
5. **Discovery draft** (Oct 1) — the **discovery** board opens.
6. **Trade deadline** (Feb 1) — trades close.
7. **Cup season** (after GW28) — the **Cups** view opens.

---

## Demo site — all features unlocked

Same as above, **plus everything that's normally phase-gated is available at once**:

- **Draft** — the live draft board: search players (sort by price / last-season points /
  team; already-taken & ineligible players are flagged), **draft** to the on-the-clock
  slot, enter pick/player **trades**, and set the round-1 order. Boards refresh live
  across devices. Build an **autodraft queue** (the "+Q" button); the commissioner can
  approve a queued pick for an absent manager.
- **Discovery draft** — the snake discovery board (same tools + queue).
- **My Team** — your squad with rich stats (form, points-per-game, goals/assists/clean
  sheets, ICT, ownership, a recent-form sparkline), availability dots, keeper badges, and
  a status box for anti-tanking risk + your injury / AFCON players.
- **Upcoming** — your next 3 head-to-head matchups with both squads and each player's
  real-life fixture + difficulty.
- **Injury list** — place an injured player on the IL (same-position replacement,
  4-GW minimum), then Return or Release.
- **International list** — send a player to AFCON / Asia Cup with a same-position
  replacement (keeps keeper eligibility); re-add them when their nation is out.
- **Cups** (public bracket view) and **Scores** (live H2H scoreboard).

In the demo: click any team to play as them, switch freely, and don't worry about
mistakes — it's a sandbox and resets to a fresh copy of the league when needed.
