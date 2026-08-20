# Draft-day test plan & runbook — 2026 draft (executed 2026-08-15/16)

This document is written to be executed **verbatim** by an operator or model with no
prior context. Follow the steps in order. Every step has an **Expected** line.

## Execution rules (read first, non-negotiable)

1. **If actual output differs from Expected: STOP.** Append the step number and the
   exact actual output to the results log, then ask the user before continuing. Do not
   improvise fixes, do not retry with variations.
2. **Never point a destructive command at the production database.** The prod URL lives
   in `.env` (a Neon host). The rehearsal works because `db.py` uses
   `load_dotenv()` with `override=False`: an **exported** `DATABASE_URL` beats `.env`.
   Every phase that writes anything begins with a GUARD step; run it every time.
3. Run everything from the repo root: `cd /Users/tpettis/dev/personal/fpl`.
4. Use **one shell** for the whole rehearsal (Phases 1–2). The exported env vars are
   the safety mechanism; a new shell loses them and falls back to prod.
5. Results log: append one line per step to `snapshots/rehearsal-results.md`
   (`snapshots/` is gitignored) in the form `P2.6 PASS` or `P2.6 FAIL: <actual>`.
6. Do not commit, push, or edit any repo file. Do not run `git` beyond `git status`.
7. Expected refusals come in two shapes — read this or you will mis-score cases:
   - **200 + banner**: a refused *pick* returns HTTP 200 with the board HTML containing
     `⚠ <message>`. This is a PASS when the message matches.
   - **Non-2xx, invisible**: 403/423/400 responses carry a plain-text body and, in a
     real browser, HTMX ignores them (nothing visibly changes). In this plan you call
     them with `curl`, so you verify the **HTTP status code and body text** directly.

---

## Phase 0 — Code gate (automated suite)

**P0.1** Confirm tree state:
```sh
cd /Users/tpettis/dev/personal/fpl && git status --short && git log --oneline -1
```
Expected: untracked lines only (`?? docs/...`), no ` M ` modified code files;
log line starts `e503afd` **or a later commit** (if later, note it in the log and
continue).

**P0.2** Confirm the test Postgres container is up:
```sh
docker ps --filter name=fpl-test-pg --format '{{.Status}}'
```
Expected: starts with `Up`. If empty:
`docker run -d --name fpl-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=fpltest -p 55432:5432 postgres:16`
then re-check.

**P0.3** Full suite:
```sh
find . -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
PATH="$PWD/.venv/bin:$PATH" \
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/fpltest \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q 2>&1 | tail -3
```
Expected: `565 passed` (or more), **`0 skipped` — the word "skipped" must not appear**,
no `failed`, no `error`. Any `skipped` = FAIL (means `TEST_DATABASE_URL` didn't take).
Any `FileNotFoundError: 'alembic'` = FAIL (means the PATH prefix was dropped).

---

## Phase 1 — Build the isolated rehearsal environment

**P1.1** Create the rehearsal database (separate from `fpltest`, which pytest truncates):
```sh
docker exec fpl-test-pg psql -U postgres -c "DROP DATABASE IF EXISTS fplrehearsal" \
 && docker exec fpl-test-pg psql -U postgres -c "CREATE DATABASE fplrehearsal"
```
Expected: `DROP DATABASE` then `CREATE DATABASE`.

**P1.2** Export the rehearsal env — **this exact block, in the shell you will keep**:
```sh
export DATABASE_URL=postgresql://postgres:test@localhost:55432/fplrehearsal
export APP_ENV=test ADMIN_PASSWORD=rehearsal SESSION_HTTPS_ONLY=0
export PATH="$PWD/.venv/bin:$PATH"
```
(`APP_ENV=test`, **never `demo`** — demo disables the phase/lock gating that half the
drill exists to test.)

**P1.3 GUARD** (repeat before any restore/write step if you ever doubt the shell):
```sh
.venv/bin/python -c "import db; u=db.DATABASE_URL; print(u); assert 'localhost:55432/fplrehearsal' in u, 'WRONG DATABASE — STOP'"
```
Expected: prints the localhost URL, no AssertionError. **AssertionError = STOP.**

**P1.4** Schema, then restore the snapshot:
```sh
alembic upgrade head 2>&1 | tail -1
.venv/bin/python snapshot.py restore snapshots/pre-draft-final-20260815.json
```
Expected: alembic ends at head with no traceback; then `restored 10001 rows from ...`.

**P1.5** Boot the app (background) and wait for health:
```sh
.venv/bin/uvicorn main:app --port 8000 >/tmp/rehearsal-uvicorn.log 2>&1 &
until curl -s http://127.0.0.1:8000/health | grep -q ok; do sleep 1; done; echo UP
```
Expected: `UP`.

**P1.6** Set up identities. `BASE` and a temp dir for cookie jars:
```sh
BASE=http://127.0.0.1:8000; J=$(mktemp -d)
# admin session
curl -s -c $J/admin -b $J/admin -o /dev/null -w '%{http_code}\n' \
  -X POST $BASE/admin/login -d password=rehearsal -d next=/admin/health
```
Expected: `303`.

**P1.7** Manager logins. The snapshot carries real password hashes, so reset each
drill manager (rehearsal DB only) and set a throwaway password. Managers used:
Kevin S `247171`, John `5520`, Gaby `248583`, Michael `268927`.
For EACH id `<ID>` (four times), run:
```sh
curl -s -b $J/admin -o /dev/null -w '%{http_code} ' \
  -X POST $BASE/admin/managers/reset-password -d fpl_manager_id=<ID>
curl -s -c $J/m<ID> -o /dev/null -w '%{http_code}\n' \
  -X POST $BASE/set-password -d manager_id=<ID> -d password=drill123 -d confirm=drill123
```
Expected each line: `303 303`.

---

## Phase 2 — Scripted mini-draft drill

Board JSON (used for assertions): `$BASE/v1/leagues/1754/draft/2026`.

**P2.1 Pre-start board shape**:
```sh
curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json,collections
b=json.load(sys.stdin)
c=collections.Counter(r['original_owner'] for r in b)
print('slots',len(b),'rounds',max(r['round'] for r in b))
print('first4',[r['owner'] for r in b[:4]])
print(dict(sorted(c.items())))"
```
Expected: `slots 111 rounds 15`; `first4 ['Kevin S', 'John', 'Gaby', 'Michael']`;
counts `Kevin S:15, Scott:15, John:11`, all others `10`.

**P2.2 Manager pick blocked before the draft starts** (phase is offseason):
```sh
curl -s -b $J/m247171 -w '\n%{http_code}\n' -X POST $BASE/draft/2026/pick -d player_fpl_id=439
```
Expected: body `The draft is locked by the commissioner.`, code `423`.

**P2.3 Queueing works before the phase; admin-without-identity cannot queue**:
```sh
curl -s -b $J/m248583 -o /dev/null -w '%{http_code}\n' \
  -X POST $BASE/draft/2026/queue/add -d player_fpl_id=27 -d draft_type=main
curl -s -b $J/admin -w '\n%{http_code}\n' \
  -X POST $BASE/draft/2026/queue/add -d player_fpl_id=27 -d draft_type=main
```
Expected: first `200` (Gaby queued G.Jesus). Second: body `Log in to queue picks.`,
code `403`.

**P2.4 Start the draft** (admin):
```sh
curl -s -b $J/admin -w '\n%{http_code}\n' -X POST $BASE/admin/phase/draft
docker exec fpl-test-pg psql -U postgres -d fplrehearsal -tAc \
  "select phase, keepers_locked from leagues"
```
Expected: EITHER code `303` OR code `502` with body starting
`error: Draft started, but the player refresh failed` — **both are PASS** (the 502 is
the live-FPL pool refresh failing offline; the phase change already committed).
**Never POST this twice.** The psql line must print `draft|t`.

**P2.5 Capture available players** (keepers are revealed now, so `/v1` filters them):
```sh
eval $(curl -s "$BASE/v1/leagues/1754/players?available_year=2026&limit=15" | .venv/bin/python -c "
import sys,json
rows=[r for r in json.load(sys.stdin) if r['fpl_id'] not in (439,27) and not r['taken']]
for n,r in zip(('AVAILQ','AVAIL1','AVAIL2'),rows[:3]):
    print(f'{n}={r[\"fpl_id\"]}; {n}_NAME=\"{r[\"name\"]}\"')")
echo "$AVAILQ $AVAILQ_NAME / $AVAIL1 $AVAIL1_NAME / $AVAIL2 $AVAIL2_NAME"
```
Expected: three ids with names, no blanks.

**P2.6 Kept player refused with a clean banner** (Kudus 512 is kept by Kevin T —
also verifies tonight's discovery-keeper fix end to end):
```sh
curl -s -b $J/m247171 -w '\n%{http_code}\n' -X POST $BASE/draft/2026/pick -d player_fpl_id=512 | grep -E '⚠|^[0-9]+$'
```
Expected: a `⚠` line containing `Kudus` and `kept`, then `200`. A `500` anywhere = FAIL.

**P2.7 Happy-path pick 1** (Kevin S, on the clock, takes Šeško 439):
```sh
curl -s -b $J/m247171 -o /dev/null -w '%{http_code}\n' -X POST $BASE/draft/2026/pick -d player_fpl_id=439
curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json; b=json.load(sys.stdin)
print(b[0]['player'], '| next on clock:', next(r['owner'] for r in b if not r['player']))"
```
Expected: `200`; then `Šeško | next on clock: John`.

**P2.8 Wrong actor blocked** (Kevin S tries to pick on John's turn):
```sh
curl -s -b $J/m247171 -w '\n%{http_code}\n' -X POST $BASE/draft/2026/pick -d player_fpl_id=$AVAIL1
```
Expected: body `It's not your pick to make.`, code `403`.

**P2.9 Logged-out HTMX gets a whole-page redirect**:
```sh
curl -s -D- -o /dev/null -H 'HX-Request: true' $BASE/draft/2026/board | grep -iE 'HTTP|hx-redirect'
```
Expected: `204` and `hx-redirect: /who`.

**P2.10 Pick 2** (John takes G.Jesus 27):
```sh
curl -s -b $J/m5520 -o /dev/null -w '%{http_code}\n' -X POST $BASE/draft/2026/pick -d player_fpl_id=27
```
Expected: `200`. (G.Jesus is now taken — that also arms P2.13.)

**P2.11 Double-pick / stale-board race** (Kevin S re-submits his own filled slot):
```sh
curl -s -b $J/m247171 -w '\n%{http_code}\n' -X POST $BASE/draft/2026/pick -d pick_number=1 -d player_fpl_id=$AVAIL1 | grep -E '⚠|^[0-9]+$'
```
Expected: `⚠ pick 1 has already been made`, then `200`. Verify nothing changed:
`curl -s $BASE/v1/leagues/1754/draft/2026 | grep -o '"player":"Šeško"'` → one match.

**P2.12 Departed player + accent search** (both in the draft search partial):
```sh
curl -s -b $J/m5520 "$BASE/draft/2026/search?q=Trossard" | grep -c "no longer in the Premier League"
curl -s -b $J/m5520 "$BASE/draft/2026/search?q=Sesko" | grep -c "Šeško"
```
Expected: `1` (or more) from each. The second line proves ASCII "Sesko" finds "Šeško"
through the real UI route.

**P2.13 Autodraft queue: taken player skipped, next available picked** (Gaby is on the
clock at pick 3; her queue holds G.Jesus — taken in P2.10 — so approve must skip him):
```sh
curl -s -b $J/m248583 -o /dev/null -w '%{http_code}\n' \
  -X POST $BASE/draft/2026/queue/add -d player_fpl_id=$AVAILQ -d draft_type=main
curl -s -b $J/admin -o /dev/null -w '%{http_code}\n' -X POST $BASE/draft/2026/approve-queued
curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json; print(json.load(sys.stdin)[2]['player'])"
```
Expected: `200`, `200`, then the player name equal to `$AVAILQ_NAME` (NOT `G.Jesus`).
Note: if approve-queued ever errors it returns `400` plain text and the board shows
nothing — that is the known UI shape, not a crash.

**P2.14 Admin overwrite replaces a slot but cannot take an unavailable player**:
```sh
# (a) refused: Šeško is already drafted; overwrite does not waive availability
curl -s -b $J/admin -w '\n%{http_code}\n' -X POST $BASE/draft/2026/pick -d pick_number=3 -d player_fpl_id=439 | grep -E '⚠|^[0-9]+$'
# (b) allowed: replace pick 3 with a genuinely available player
curl -s -b $J/admin -o /dev/null -w '%{http_code}\n' -X POST $BASE/draft/2026/pick -d pick_number=3 -d player_fpl_id=$AVAIL1
curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json; print(json.load(sys.stdin)[2]['player'])"
```
Expected: (a) `⚠ ... Šeško ...` + `200`; (b) `200`, then `$AVAIL1_NAME` at pick 3.

**P2.15 Wrong-pick correction: delete → clock jumps BACK → admin re-records**
(this is the live incident procedure, drilled exactly):
```sh
PICK1=$(docker exec fpl-test-pg psql -U postgres -d fplrehearsal -tAc \
  "select id from draft_picks where pick_number=1 and season_year=2026 and draft_type='main'")
curl -s -b $J/admin -o /dev/null -w '%{http_code}\n' \
  -X POST $BASE/admin/corrections/pick/delete -d pick_id=$PICK1
curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json; b=json.load(sys.stdin)
print('on clock:', next(r for r in b if not r['player'])['pick'], '| pick3:', b[2]['player'])"
curl -s -b $J/admin -o /dev/null -w '%{http_code}\n' -X POST $BASE/draft/2026/pick -d pick_number=1 -d player_fpl_id=439
```
Expected: `303`; then `on clock: 1 | pick3: <AVAIL1_NAME>` — the clock jumps BACKWARDS
to the reopened hole while later picks stay; then `200` re-recording Šeško, after which
the open pick is 4 again (verify:
`curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "import sys,json; print(next(r['pick'] for r in json.load(sys.stdin) if not r['player']))"`
→ `4`).

**P2.16 Pick trade: third party blocked, then a party succeeds**:
```sh
ROUND=$(curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json
print(next(r['round'] for r in json.load(sys.stdin)
      if r['round']>=5 and r['owner']=='John' and r['original_owner']=='John'))")
# (a) Kevin S is neither party -> forbidden
curl -s -b $J/m247171 -w '\n%{http_code}\n' -X POST $BASE/draft/2026/trade-pick \
  -d pick=5520:$ROUND -d to_fpl=248583 -d draft_type=main
# (b) John (a party) trades it to Gaby
curl -s -b $J/m5520 -o /dev/null -w '%{http_code}\n' -X POST $BASE/draft/2026/trade-pick \
  -d pick=5520:$ROUND -d to_fpl=248583 -d draft_type=main
curl -s $BASE/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json
r=next(x for x in json.load(sys.stdin) if x['round']==int(sys.argv[1]) and x['original_owner']=='John')
print(r['owner'], r['traded'])" $ROUND
```
Expected: (a) body `You must be one of the two managers in the pick trade.`, code
`403`; (b) `200`, and the final line prints `Gaby True`.

**P2.17 writes_locked blocks managers, not admin** (Michael is on the clock at pick 4):
```sh
# lock (keepers_lock=on preserves the keeper lock the draft start set — this form
# writes ALL THREE flags every submit; omitting one clears it)
curl -s -b $J/admin -o /dev/null -w '%{http_code}\n' -X POST $BASE/admin/lock -d lock=on -d keepers_lock=on
curl -s -b $J/m268927 -w '\n%{http_code}\n' -X POST $BASE/draft/2026/pick -d player_fpl_id=$AVAIL2
curl -s -b $J/admin -o /dev/null -w '%{http_code}\n' -X POST $BASE/draft/2026/pick -d pick_number=4 -d player_fpl_id=$AVAIL2
# unlock (lock omitted -> cleared; keepers stay locked)
curl -s -b $J/admin -o /dev/null -w '%{http_code}\n' -X POST $BASE/admin/lock -d keepers_lock=on
```
Expected: `303`; then body `The draft is locked by the commissioner.` code `423`;
then `200` (admin bypasses and records pick 4); then `303`.

**P2.18 Silent no-op is a no-op** (documented shape: a POST at a nonexistent slot
returns 200 with no banner and writes nothing):
```sh
docker exec fpl-test-pg psql -U postgres -d fplrehearsal -tAc "select count(*) from draft_picks where season_year=2026"
curl -s -b $J/m5520 -X POST $BASE/draft/2026/pick -d pick_number=9999 -d player_fpl_id=$AVAIL1 | grep -c '⚠' || true
docker exec fpl-test-pg psql -U postgres -d fplrehearsal -tAc "select count(*) from draft_picks where season_year=2026"
```
Expected: same count before and after (4), and `0` banner matches in between.

---

## Phase 3 — Prod read-only verification (run TOMORROW MORNING, before starting)

Use a **fresh shell with NO exports** (prod URL comes from `.env`). Read-only except
the final snapshot.

**P3.1** `python scripts/preflight_draft.py` — expected: every check PASS. If
`keeper selections submitted` still FAILs, the named managers have not submitted;
that is a people problem — **do not start the draft** until it passes or the
commissioner explicitly accepts the short submission.

**P3.2** Board shape:
`curl -s https://twofirstsonepup.onrender.com/v1/leagues/1754/draft/2026` — expect 15
rounds; slot counts change as the last keepers land (each new keeper −1 slot for that
manager). No manager may exceed 15.

**P3.3** Search: `.../v1/leagues/1754/players?q=Sesko` returns Šeško.

**P3.4** Final safety snapshot, immediately before clicking Start draft:
```sh
cd /Users/tpettis/dev/personal/fpl
.venv/bin/python -c "import db; u=db.DATABASE_URL; print(u); assert 'neon' in u.lower() or 'localhost' not in u, 'expected PROD here'"
.venv/bin/python snapshot.py save snapshots/pre-draft-start-20260816.json
```
Expected: prints the Neon URL, then `saved ... rows across 41 tables`.

**P3.5** Start the draft from `/admin/health` (browser). If the response is the plain
502 `Draft started, but the player refresh failed` — the draft IS started; do not
click again; run `POST /admin/sync?force=1` (with `X-Auth-Token`) before picking.

## Phase 4 — During-draft incident runbook (procedures drilled in Phase 2)

| Incident | Procedure | Drilled in |
|---|---|---|
| Wrong player recorded | `/admin/corrections` → delete that pick (the clock jumps BACK to the hole; later picks keep) → admin re-records the correct player at that `pick_number` from the board → clock returns to the tail | P2.15 |
| Manager absent | They pre-queue (`+Q`); when on the clock, admin clicks **Approve queued pick** — taken/kept players are skipped automatically. Or admin records for them directly | P2.13 |
| Manager reports "button did nothing" | Expected for ownership/lock refusals (403/423 don't repaint the page). Check whose turn it is on the board; the Draft button only renders for the on-clock manager | P2.8 |
| Refusal banner vanished | The `⚠` banner is wiped by the 7-second board poll — it was real; have them retry and read immediately | P2.6/P2.11 |
| Manager can't log in | `/admin/health` → reset password for that manager → they set a new one at `/who` | P1.7 |
| Board frozen for everyone | Hard-refresh; check `https://twofirstsonepup.onrender.com/health`; check Render status. Data is safe — picks are in the DB, the board is computed on read | — |
| Chaos / need to freeze | `/admin/health` → **check writes_locked AND keep keepers_locked checked** (the form writes all three flags on every save — an unchecked box CLEARS that lock) → admin can still act | P2.17 |
| Disaster, restart the draft | Lock writes first, then in a prod shell: GUARD (P3.4 assertion), then `python snapshot.py restore snapshots/pre-draft-start-20260816.json`. This erases EVERYTHING after that snapshot | P1.4 (mechanics) |
| Anything needing code | Do not deploy during the draft. Log it in the backlog; every procedure above is admin-UI only | — |

## Phase 5 — Teardown (after the rehearsal, same shell)

```sh
pkill -f "uvicorn main:app --port 8000"; sleep 1
docker exec fpl-test-pg psql -U postgres -c "DROP DATABASE IF EXISTS fplrehearsal"
unset DATABASE_URL APP_ENV ADMIN_PASSWORD SESSION_HTTPS_ONLY
curl -s https://twofirstsonepup.onrender.com/v1/leagues/1754/draft/2026 | .venv/bin/python -c "
import sys,json; b=json.load(sys.stdin)
print('prod picks recorded:', sum(1 for r in b if r['player']))"
```
Expected: `DROP DATABASE`, then `prod picks recorded: 0` — proof the rehearsal never
touched production.

---

## Quick-reference: routes, fields, guards, refusal shapes

| Action | Method + URL | Form fields | Who | Refusal |
|---|---|---|---|---|
| Start draft | POST `/admin/phase/draft` | — | admin | 303→login |
| Board page / poll | GET `/draft/2026`, `/draft/2026/board` | `?draft_type=main` | any logged-in | 303/204→`/who` |
| Search | GET `/draft/2026/search` | `?q=&position=&sort=` | any logged-in | — |
| Make pick | POST `/draft/2026/pick` | `player_fpl_id`, opt `pick_number` | on-clock mgr / admin (`overwrite`) | 423 lock / 403 not-your-pick / 200+`⚠` rule |
| Queue add/remove | POST `/draft/2026/queue/add\|remove` | `player_fpl_id`, `draft_type` | self (session), not phase-gated | 403 / 400 |
| Approve queued | POST `/draft/2026/approve-queued` | — | admin | 403 / 400 plain text |
| Trade pick | POST `/draft/2026/trade-pick` | `pick`=`<orig_fpl>:<round>`, `to_fpl`, `draft_type` | either party / admin | 423 / 403 / 400 |
| Trade player | POST `/draft/2026/trade-player` | `from_fpl`, `to_fpl`, `player_fpl_id` | either party / admin | same |
| Delete pick | POST `/admin/corrections/pick/delete` | `pick_id` | admin | 303→login / 400 |
| Locks | POST `/admin/lock` | `lock`, `keepers_lock`, `sync_lock` (checkboxes — **all three written every submit**) | admin | 303→login |
| Reset mgr password | POST `/admin/managers/reset-password` | `fpl_manager_id` | admin | 303→login |
| Admin login | POST `/admin/login` | `password`, `next` | — | 401 |
| Manager login / set | POST `/login` / `/set-password` | `manager_id`, `password`(,`confirm`) | — | 401 / 400 |

Useful ids: Kevin S `247171`, John `5520`, Gaby `248583`, Michael `268927`,
Kevin T `264571`, Scott `21768`. Players: Šeško `439`, G.Jesus `27`, Kudus `512`
(kept: Kevin T). Prod league key: `1754`.
