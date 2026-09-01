# Security notes & runbook

Small private league app (≈10 users) on Render + Neon, session-cookie auth.

## Environment variables (set in the Render dashboard, never commit)
- `DATABASE_URL` — Neon connection string (`?sslmode=require`).
- `SECRET_KEY` — signing key for session cookies. **Required in prod**: the app
  refuses to start (`RuntimeError`) if `APP_ENV=prod` and `SECRET_KEY` is the dev
  default. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `SESSION_HTTPS_ONLY` — set to `1` in prod so session cookies are HTTPS-only
  (Render serves TLS). Leave unset/`0` for local http dev.
- `ADMIN_PASSWORD` — commissioner login (falls back to `SYNC_AUTH_TOKEN`).
- `SYNC_AUTH_TOKEN` — `X-Auth-Token` for the cron `POST /admin/sync` and programmatic
  `/admin/*` JSON. Compared timing-safe.
- `APP_ENV` — `prod` (default) / `test`. `test` shows a site-wide banner.
- `DISCORD_WEBHOOK_URL` — optional. Incoming webhook for the public trade-announcement
  channel. **The URL is the whole credential** — anyone holding it can post to that
  channel as the app, so treat it exactly like a password and never commit it. Rotate
  by deleting the webhook in Channel Settings → Integrations and making a new one.
- `DISCORD_ALERT_WEBHOOK_URL` — optional. Incoming webhook for the PRIVATE commissioner
  channel. Point it somewhere only the commissioner can read: it carries flagged
  actions and failed health checks, i.e. named managers and their infractions.

- `DISCORD_BOT_TOKEN` — optional, for INBOUND (reading announcements). A bearer secret
  with no scoping beyond the permissions granted at invite: anyone holding it can act
  as the bot in every guild it has joined. Rotate by regenerating in the Developer
  Portal, which invalidates the old one immediately. Discord also scans public repos
  for leaked tokens and auto-invalidates. Note it is NOT tied to your user account —
  the bot stays if you leave the server.
- `DISCORD_TRADE_CHANNEL_ID` / `DISCORD_IL_CHANNEL_ID` — optional. Not secret, but kept
  in env alongside the token so the whole feature is configured in one place.

- `ANTHROPIC_API_KEY` — optional, for the AI gameweek review. A **billable** credential:
  anyone holding it spends your money, so it is the one secret here whose leak costs
  directly rather than exposing data. Rotate in the Anthropic Console, which revokes the
  old key immediately. The review itself is worth ~$0.04 a gameweek, but a leaked key is
  not bounded by our usage — `rules.MAX_AI_CALLS_PER_GW` caps only what *this app* will
  spend. Never sent anything but the prompt: league scores, standings and the
  commissioner's manager notes. No passwords, no emails, no session data.

Reading requires the **MESSAGE CONTENT** privileged intent (it gates the REST API, not
just the gateway) plus `VIEW_CHANNEL` + `READ_MESSAGE_HISTORY` on each channel — and on
a private channel that means an explicit permission overwrite, since guild-level roles
do not reach it. Both misconfigurations fail SILENTLY (blank content, or an empty array
instead of a 403), so `/admin/health` probes for them.

The AI review is OFF when `ANTHROPIC_API_KEY` is unset, and **generation never posts**
— `ai_content.py` contains no sending code at all, and a review reaches Discord only when
the commissioner presses the button on the homepage. That split is the point: a model
cannot judge when a joke lands badly on a particular person in a particular week, and a
chat message cannot be unsent.

All Discord features are OFF when their variable is unset — no config UI, no database
flag — so a fresh checkout, the test suite and the demo sandbox are silent by default.
Neither needs a bot, a Developer Portal app, a privileged intent or Manage Server.

## Rotate exposed secrets (do once)
The `.env` file's values were committed in early history (`ccb61a9`, `dd92cd6`) — it's
gitignored now, but those values are burned. Rotate all of them:
1. **Neon DB password** — Neon console → reset role password → update `DATABASE_URL`
   in Render (and local `.env`).
2. **SECRET_KEY** — generate a new one (above) → set in Render. (Rotating invalidates
   existing login sessions — everyone re-logs-in; fine.)
3. **SYNC_AUTH_TOKEN** — new random token → update Render **and** the GitHub Actions
   cron secret that calls `/admin/sync`.
4. **ADMIN_PASSWORD** — set a strong value (replace the old `sports`).
5. (Optional) scrub history with `git filter-repo` if the repo is ever made public.

## In-app protections (built)
- Per-manager PBKDF2 passwords (`auth.hash_password`); admin password + token compared
  with `hmac.compare_digest`.
- Hard identity gate (`GateMiddleware`); per-manager write authorization (`can_act_as`).
- Secure/`same_site=lax` cookies (HTTPS-only in prod); SECRET_KEY start-up guard.
- Security headers (X-Frame-Options, X-Content-Type-Options, Referrer-Policy, CSP, HSTS
  on https); Jinja autoescape; error responses are `text/plain`; bounded numeric input.
- Editing lock (`writes_locked`) + keeper-freeze (`keepers_locked`).

## Not done (deferred, acceptable for a 10-user private app)
- CSRF tokens on forms (relying on `same_site=lax` cookies).
- Rate limiting / lockout on login endpoints.
- Admin action audit log beyond the standings-adjustment + fine logs.
