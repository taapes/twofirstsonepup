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
