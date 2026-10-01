# Web search app — design spec

Status: approved by user 2026-10-01 (web-app sub-project only; sync/VM-deploy/DNS are
separate, later specs).

## 1. Purpose & scope

The original server-archiver spec (`2026-09-29-server-archiver-design.md`) explicitly
put "a web service, or public data publication" out of scope. This spec supersedes that
line for one narrow case: a **private, auth-gated** search UI for league members, never
a public one. The "never publish private server content" principle from the original
spec is unchanged — every route in this app requires a login; no route ever serves
archived content without authentication.

This spec covers only the self-contained, locally-testable piece: a Flask app,
`archive serve`, that serves a black-background terminal-style search page over HTTP,
backed directly by the existing `archiver.search.search()` used by the CLI's `find`
command. Explicitly **out of scope** for this spec:
- Syncing data to a remote host.
- Oracle Cloud VM provisioning, nginx, certbot/TLS, systemd.
- DNS (dynv6) setup.
- Any multi-user administration UI (user management stays CLI-only, run by the owner).

Those become their own spec(s) once this piece is built and once the user has Oracle
Cloud and dynv6 accounts set up (manual steps outside what this project can automate).

## 2. Visual design

Already approved via an Artifact mockup during brainstorming: pure black background,
monospace (OpenCode font, a user-supplied `.otf`), a single search bar with a blinking
cursor, results streaming in below as raw terminal-style log lines (dim gray
timestamp/channel/author, white message content) rather than boxed "cards." A small
operator badge top-right shows the logged-in codename. No other chrome.

## 3. Components

New files, all under the existing `archiver/` package (no new top-level package):

- `archiver/credentials.py` — credential storage and verification.
- `archiver/webapp.py` — Flask app factory and routes.
- `archiver/static/` — the production front-end: `index.html`, `app.css`, `app.js`,
  `OpenCode.otf`. Plain HTML/CSS/vanilla JS (no React/build step — this is a real served
  page, not an Artifact component), translating the approved mockup's look exactly.
- `credentials.example.json` — committed template (mirrors `config.example.json`'s
  pattern); `credentials.json` itself is real, machine-specific, and gitignored.

New CLI subcommands in `archiver/cli.py`:
- `archive serve [--host 127.0.0.1] [--port 8000]`
- `archive useradd <codename>`

## 4. Credentials

`credentials.json` (gitignored), shape:

```json
{
  "secret_key": "<64 hex chars, generated once>",
  "users": {
    "<codename>": "<bcrypt hash>"
  }
}
```

`archiver/credentials.py`:
- `verify_login(codename: str, password: str, path: Path) -> bool` — loads the file,
  looks up `codename` (case-sensitive), `bcrypt.checkpw` against the stored hash.
  Returns `False` (never raises) for a missing file, missing codename, or malformed
  JSON — a login failure, not a crash, regardless of cause.
- `add_user(codename: str, password: str, path: Path) -> None` — loads the file if it
  exists (creating `{"secret_key": secrets.token_hex(32), "users": {}}` if not), bcrypt-
  hashes `password` with a fresh salt, sets `users[codename]`, writes the file back.
  Overwrites an existing codename's password silently (re-running `useradd` for the same
  name is how you reset a password).
- `load_secret_key(path: Path) -> str` — returns `secret_key` from the file, creating
  the file via `add_user`'s "no file yet" path if necessary (so `archive serve` run
  before any `useradd` still gets a stable secret key rather than crashing).

`archive useradd <codename>` prompts for the password via `getpass.getpass` (never
echoed, never passed as a CLI argument, never logged) and calls `add_user`. This command
is the only way credentials are created or changed — there is no web-facing account
management of any kind, matching "only my account can useradd."

## 5. Web app routes

`archiver/webapp.py` exposes `create_app(config) -> Flask`, consumed by both
`archive serve` (local dev, Flask's built-in server) and, in a later spec, gunicorn on
the deployed VM (`archiver.webapp:create_app` imported directly — this factory is the
one piece of this spec that the later deployment spec depends on, so its name and
signature are load-bearing).

- `GET /login` — serves `static/index.html`'s login view (same black/mono look, just a
  codename + password form instead of the search bar).
- `POST /login` — form fields `codename`, `password`. On `verify_login` success, sets
  `session['codename'] = codename` and redirects to `/`. On failure, re-renders the
  login view with a generic "invalid codename or password" message (never reveals which
  of the two was wrong).
- `GET /logout` — clears the session, redirects to `/login`.
- `GET /` — if `session` has no `codename`, redirect to `/login`. Otherwise serve the
  search page.
- `POST /api/search` — requires `session['codename']` (else `401 {"error": "not
  authenticated"}`, which the frontend turns into a redirect to `/login`). Body:
  `{"query": "<raw query string>"}`. Calls
  `archiver.search.search(catalog_conn, data_dir, query, limit=200)` — capped, unlike
  the CLI's uncapped `export` command, because this is an interactive UI where 200
  results is already more than one screenful. Returns
  `{"results": [{"time": "...", "channel": "...", "author": "...", "content": "..."},
  ...], "count": N}`, or `{"error": "<message>"}` with `400` for a malformed query
  (reusing the same `ValueError` the CLI's `find` already catches from
  `tokenize_query`/`search`).

A `before_request` hook enforces the login gate for every route except `/login` and
static files, rather than repeating the session check in each view function — one place
to audit, consistent with the project's existing preference for centralizing a check
over duplicating it per caller (the same reasoning already applied to search's `from:`
filter and report's dirty-detection).

## 6. Session security

- `app.secret_key` = `credentials.py`'s `load_secret_key()` — generated once, stored
  next to the credentials, never regenerated on restart (restarting the server would
  otherwise invalidate every open session).
- Cookie flags: `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE="Strict"`
  always. `SESSION_COOKIE_SECURE` is a `config.json` key, `secure_cookies`, defaulting
  to `true`; `archive serve` run locally over plain `http://127.0.0.1` needs it set to
  `false` in `config.json` for the cookie to be sent at all (a `Secure` cookie is
  silently dropped by the browser over plain HTTP) — this is a one-line local-dev
  toggle, not a feature flag meant to ship enabled in production. The later VM-deploy
  spec sets it back to `true` once real TLS is in front of the app.
- No "remember me," no password reset flow, no email anywhere in this system — codename
  + password, set and reset only via `archive useradd` run by the owner.

## 7. Testing

Flask's `app.test_client()` — no real server process needed for tests. Covers:
- `verify_login`/`add_user` round-trip (correct password succeeds, wrong password
  fails, unknown codename fails, missing credentials file fails rather than raising).
- `/login` POST: correct credentials redirect to `/` and set a session cookie; wrong
  credentials re-render the login page without setting one.
- `/` without a session redirects to `/login`; with a valid session (test client sets
  the session directly, no need to log in via HTTP in every test) serves the search
  page.
- `/api/search` without a session returns `401`; with a session, returns the expected
  JSON shape for a seeded catalog+shard fixture (reusing the `_seed`-style fixture
  pattern already established in `tests/test_snapshot.py`); a malformed query (e.g. an
  unterminated quote, the same case `find` already handles) returns `400` with an error
  message, not a stack trace.

## 8. Global constraints

- No new top-level dependency beyond `flask` and `bcrypt` (both added to
  `requirements.txt`); no frontend build tooling (no npm, no bundler) — the frontend is
  committed source, served as-is.
- Every route that can return archived content requires a valid session; this is
  checked in exactly one place (the `before_request` hook), not per-route.
- `credentials.json` is never committed, matching `config.json`'s existing
  `.gitignore` treatment; `credentials.example.json` is the committed template.
- `create_app(config)` is a stable name/signature — the later VM-deployment spec imports
  it directly via gunicorn.
- Flask's debug mode (`app.run(debug=True)`) is never enabled, including for local
  `archive serve` runs — Flask's interactive debugger allows arbitrary code execution
  to anyone who can reach it, which is a standing risk even on `127.0.0.1` on a
  multi-user machine.
