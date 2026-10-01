# server-archiver — design spec

Status: approved by user 2026-09-29, incorporating corrections from the same date.

## 1. Purpose & scope

A public GitHub project, developed and run continuously on one Windows machine, that
archives every extant bot-accessible message from one private Discord friend-group
server (existing since ~2025-10-20) into local SQLite. Token, message data, reports,
logs, and backups live only on that machine; the source code is public.

In scope: text/announcement channels, forum/media channel posts (title, tags, starter
message, replies), active and archived threads (public and — where permitted — private),
text chat inside voice channels, bot/webhook/system/attachment-only/unusual messages,
full available text and Markdown, timestamps, edits, replies, embeds, stickers, polls,
reactions (aggregate), mentions, message flags, and attachment *metadata* only (never
attachment bytes, never call/voice audio).

Out of scope: moderation/role/join-leave/channel-edit history, cloud sync, telemetry, a
web service, or public data publication.

## 2. Process & module architecture

One Python process (`discord.py`), launched via `pythonw`, matching pfpscraper's quiet
background-bot convention. Inside one `discord.Client`:

1. **Gateway / live capture** — started immediately on connect, before backfill, so no
   message can fall into a race gap.
2. **Backfill worker** — a background `asyncio.Task` kicked off after `on_ready`,
   bounded by `asyncio.Semaphore(3)` (configurable) so it never starves the gateway
   heartbeat or the rate-limit bucket shared with pfpscraper (same bot, same token).
3. **Scheduler loop** — checked once a minute: recent-window revisits, snapshot
   triggers, dirty-report regeneration, lock heartbeat.

Modules: `archiver/discord_io.py` (Discord API/gateway glue), `archiver/store.py`
(SQLite access, upserts, checkpoints), `archiver/backfill.py`, `archiver/snapshot.py`,
`archiver/search.py`, `archiver/report.py`, `archiver/cli.py`, `run.py` (entry point).

Config lives in `config.json` (paths via `pathlib`, no hardcoded username/drive),
`.env` holds `DISCORD_TOKEN` only, parsed manually like pfpscraper does (no new
dependency). The existing `.env` in this folder is reused as-is, never modified.

## 3. Single-instance guarding (corrected)

**Not** a PID file with a staleness heuristic — a stale PID file cannot distinguish
"process crashed, safe to reclaim" from "process alive under a reused PID," which is a
real failure mode on Windows process-ID reuse.

Instead: an OS-held exclusive lock on a lock file, via stdlib `msvcrt.locking()`
(`LK_NBLCK`) on a fixed byte in `<data_dir>\archiver.lock`. The OS releases this lock
automatically the instant the process exits for any reason, including a crash or `taskkill`
— no heuristic, no reclaim logic, no dependency beyond the standard library. A second
launch attempt fails the lock acquisition immediately and exits with a clear log message.

## 4. Storage

`catalog.sqlite`:
- `channels` — stable Discord ID → name, type, parent category ID, archived flag.
- `category_names` — category ID → current display name (renames relabel in place;
  they never move existing data).
- `users`, `user_nicknames` — append-only, only nicknames actually observed; no
  historical names invented for periods before observation started.
- `channel_month_shard` — (channel_id, yyyymm) → shard file. A channel that later
  moves Discord category keeps its already-written months in their original shard;
  only new writes go to the new category's shard. No migration, no duplication.
- `coverage` — per channel/thread: status (`pending`/`crawling`/`complete`/
  `inaccessible`/`failed`), oldest/newest archived message, message count, backfill
  checkpoint, live checkpoint, last_checked_utc, gap_reason.
- `snapshots` — tarball path, sha256, row count, created_utc, partial flag.

Per category, per month (`<Category>\<YYYY-MM>.sqlite`):
`messages`, `attachments` (metadata only: filename, content_type, size, width, height,
duration, description), `reactions` (message_id, emoji, is_custom, animated, count —
aggregate only; per-reactor lists need one extra API call per emoji per message and are
out of scope, documented as a known limitation), `mentions_user`, `mentions_role`,
`stickers`, `polls` + `poll_answers`, `embeds` + `embed_fields`, and an append-only
`events` table recording every edit and delete with its own timestamp (a delete row is
written only from an explicit gateway delete event carrying a message ID — see §6 on
why absence is never treated as deletion). `messages_fts` (see §7) backs search.

All timestamps stored UTC. Asia/Seoul (stdlib `zoneinfo`; the `tzdata` package is a
required dependency on Windows, which has no system IANA database) is used for month
bucketing and all displayed times.

Every write is `INSERT ... ON CONFLICT(id) DO UPDATE` (except partial-edit updates,
§6) — replaying any page, from backfill or live catch-up, is always safe.

### 4.1 Cross-file commit protocol (corrected)

A single 100-message history page can span a month/category boundary (e.g. messages
from 2025-10-31 and 2025-11-01), meaning one page can require writes to two shard files
plus a checkpoint update in `catalog.sqlite` — three separate SQLite connections/files.
SQLite (WAL or not) makes no atomicity guarantee across separate database connections,
even with `ATTACH DATABASE`, once files are opened as independent connections.

Commit order, per page:
1. Write and commit message rows to each affected shard, one shard-connection
   transaction per shard. These writes are idempotent upserts — safe to redo.
2. Only after **every** affected shard has committed successfully, update the
   `coverage` checkpoint for that channel in `catalog.sqlite` and commit.
3. If the process crashes between step 1 and step 2, the checkpoint still points at
   the previous page on restart. The page is replayed in full: shard rows upsert
   cleanly (no duplication, no double-counting), and the checkpoint then advances.

Test required: simulate a crash after shard A commits but before shard B commits (or
before the checkpoint commit), restart, and assert the replayed page produces no
duplicate rows and the checkpoint correctly advances exactly once.

## 5. Discovery & backfill

Discovery never trusts a cached channel list — it is re-run at startup and
periodically. Per guild: `fetch_channels()`, then per text/announcement/forum/media
channel: active threads, `archived_threads(private=False)` paginated to exhaustion,
`archived_threads(private=True)` if the bot has `Manage Threads`, else
`archived_threads(private=True, joined=True)` as a reduced-coverage fallback (recorded
explicitly in `coverage.gap_reason`). Voice channels use `history()` identically to
text channels. A channel/thread that disappears from a discovery pass is marked
`inaccessible`, never deleted from `coverage` — losing visibility must not erase prior
findings.

Backfill proceeds oldest-first from each channel's checkpoint,
`history(limit=100, after=Object(id=checkpoint), oldest_first=True)`, committed per
§4.1 (corrected from an earlier `before=checkpoint` draft: reading discord.py's actual
`history()` implementation shows `oldest_first=True` always paginates via its internal
`after`-based strategy, defaulting to the oldest message in the channel when `after`
isn't given — passing `before` alongside it only filters an upper bound, it is not a
resume cursor, and using it as one would silently restart from the channel's beginning
on every call instead of resuming). `coverage.backfill_checkpoint` therefore holds the
newest message ID successfully archived so far — a high-water mark moving forward in
time, not an "oldest reached" cursor. Each channel/thread is wrapped in its own
try/except: a 403 or other API failure sets `status=inaccessible`/`failed` with the
error recorded as `gap_reason`, and backfill moves on to the next channel rather than
aborting the entire crawl. Rate limits are handled by discord.py itself
(already-installed dependency; no custom limiter).

## 6. Live capture & offline recovery (corrected)

`on_message` writes new messages immediately. Edits and deletes are handled via
`on_raw_message_edit` / `on_raw_message_delete` (not the cache-dependent
`on_message_edit`/`on_message_delete`), because Discord's raw gateway payload for an
edit may contain **only the fields that changed**, not the full message.

- An edit payload is applied as a targeted SQL `UPDATE ... SET <only present columns>`,
  never a full-row replace. A field absent from the payload leaves the stored value
  untouched; a complete stored row is never overwritten with a partial one.
- A delete event (which carries only a message ID) writes a `deleted_at` marker to the
  `events` table and flags the message row. **Absence of a message during any scan or
  rescan is never itself treated as evidence of deletion** — deletion is recorded only
  from an explicit delete event with an ID. This is stated in `doctor`/coverage output
  and the README: *messages deleted while the archiver was offline cannot be detected
  or reconstructed by a later rescan; only deletions observed live, or deletions
  discoverable because Discord itself no longer returns the message on a subsequent
  backfill/live fetch of that exact page, are recorded, and the two are not the same
  guarantee — a gap in a rescan is reported as "not reconfirmed," never as "deleted."*
- The live checkpoint (high-water mark) advances **only** when a channel's contiguous
  message-creation history is confirmed up to a point — i.e. from `on_message` on newly
  created messages, or from a `history(after=checkpoint)` catch-up page during startup.
  It is never advanced because an edit or delete event was processed, regardless of
  that event's message timestamp. Processing an edit/delete on an old message says
  nothing about whether messages between the last checkpoint and that message's
  timestamp were actually captured.

Startup catch-up sequence, every launch: rediscover channels/threads (picks up threads
created while offline) → `history(after=live_checkpoint)` on every `complete` channel →
a fresh pass over a configurable recent window (default 3 days) to catch missed
edits/reactions after a reconnect gap (safe to re-run — idempotent) → any
pending/failed channel resumes backfill.

Tests required: (a) a partial raw-edit payload must not null out fields the stored row
already has; (b) processing an edit or a delete for an old message must leave the live
checkpoint unchanged.

## 7. Search

Hand-written filter grammar — `from:`, `in:`, `during:`, `after:`, `before:`, `has:`,
`file:`, `ext:`, quoted phrases via stdlib `shlex` — no parser dependency.

### 7.1 Substring matching (corrected)

`unicode61` FTS tokenization does not satisfy arbitrary-position substring matching
(including inside Korean words, which have no whitespace-delimited stems it can rely
on), so it cannot be the primary mechanism.

At Stage 1 (schema & fixtures), the build must **test** whether the Python
`sqlite3` module's bundled SQLite was compiled with the trigram FTS5 tokenizer
(`CREATE VIRTUAL TABLE ... USING fts5(..., tokenize='trigram')` — introduced in SQLite
3.34+, not guaranteed present in every Python build). Two outcomes:

- **Trigram available**: used for queries of **3 or more characters** — correct for
  substrings inside English and Korean words alike, indexed, fast. A trigram tokenizer
  cannot match anything shorter than 3 characters by construction (a trigram is 3
  characters), so this is a hard floor, not a tuning choice — confirmed empirically in
  Stage 1 (`archiver/fts.py`'s test suite: a 2-character Korean query against a trigram
  index returns nothing, even though the index and triggers are working correctly).
- **Trigram unavailable, or query under 3 characters even when trigram *is* available**:
  fall back to a scoped `LIKE '%fragment%'` scan, applied only within the
  already-filtered result set (channel/date/sender predicates narrow the candidate
  rows via indexed columns first; only the remaining rows get the `LIKE` content scan).
  This is correct for any fragment length and any language but O(n) over the filtered
  set — documented plainly in the README and `--help` output as slower on
  large, unfiltered date ranges. Stage 5 (search) must route on query length, not just
  on trigram availability: length >= 3 and trigram available -> FTS `MATCH`; anything
  else -> `LIKE` fallback.

Typo tolerance, if built, stays a clearly separate opt-in from substring matching —
never blended into the same result ranking.

Tests required: a fragment from the middle of a Korean word, and a fragment from the
middle of an English word, must both match under whichever backend is active.

Output: compact/paginated by default; `--full`, `--context N` (crosses month-shard
boundaries correctly), `--json`.

## 8. Reports

`server.md` + one per category, regenerated only when a per-scope `report_dirty` flag
in the catalog is set, cleared on regen. Counting rules (e.g. "image" = any attachment
`content_type` starting `image/`) are stated explicitly in the generated report text,
since labels overlap by design. Korean/English word rankings use simple
whitespace/punctuation tokenization, explicitly labeled as such (no linguistic
segmentation dependency). Bot/system messages are excludable from word rankings via
config without being excluded from the archive itself. The coverage section is diffed
against the previous report run to surface newly-`inaccessible` channels.

## 9. Snapshots, verify, restore

`sqlite3.Connection.backup()` (true consistent snapshot API — never a raw copy of a
live database file mid-write) → temporary `.tar.gz` on the same volume → sha256 +
manifest JSON (`month`, `category`, `sha256`, `row_count`, `created_utc`, `partial`) →
atomic rename replacing the prior snapshot pair. Triggered daily for the current month,
and immediately whenever an older month's shard receives a late write (never
recompressed per-message). `verify` re-extracts, checks the hash against the manifest,
and runs `PRAGMA integrity_check`. `restore` verifies before ever touching a live shard
path and refuses to overwrite without `--force` or a different target.

## 10. Windows operation

### 10.1 Manual launch
`run.bat` uses the pfpscraper pattern (`start "" pythonw run.py`) for quiet
double-click/Startup-folder use.

### 10.2 Task Scheduler (corrected)
`run.bat`'s detached `start` pattern must **not** be the Task Scheduler action target:
`cmd.exe` launches the detached `pythonw` child and exits immediately, so Task
Scheduler's own process tracking ends the moment `cmd.exe` returns — it has nothing
left to supervise, and "restart on failure" never fires because the *task itself*
reports success. The documented Task Scheduler action instead points directly at the
long-running process:
- Program: full path to `pythonw.exe`
- Arguments: full path to `run.py`
- Start in: the project directory
- Trigger: "At log on"
- Settings: "If the task fails, restart every N minutes," configured in the task's own
  Settings tab.

This way Task Scheduler's tracked process *is* the archiver process, so its own
restart-on-failure logic works correctly. `run.bat` remains available for interactive
manual starts but is documented as unsuitable for the scheduled task.

### 10.3 Coexistence
`doctor` prints the authenticated bot's name/ID and confirms Message Content Intent is
actually granted and the data folder is writable — never the token. Running alongside
pfpscraper (same bot, same token) is expected; both share Discord's rate-limit budget,
handled by discord.py per process independently (no cross-process coordination needed
at this scale).

## 11. Testing

`pytest` + `pytest-asyncio`, hand-built fake discord.py objects carrying only the
attributes the code actually reads (not blanket `MagicMock`, which silently tolerates
missing-attribute bugs). Required fixtures, beyond standard coverage: multi-page
archived-forum pagination; a private-thread 403 that doesn't abort the crawl; a crash
between one shard's commit and the checkpoint commit (§4.1); a partial raw-edit payload
that must not null out existing fields (§6); an edit/delete on an old message that must
not move the live checkpoint (§6); live-vs-backfill duplicate delivery of the same
message ID; Seoul-midnight month-boundary placement; two users sharing a display name
(search disambiguation); an edit landing in an already-snapshotted month (triggers
re-snapshot + dirty report); a mid-word Korean fragment and a mid-word English fragment
under the active search backend (§7.1); snapshot/restore round-trip integrity (corrupt
a byte, confirm `verify` catches it).

## 12. Known, stated limitations

- Messages deleted while the archiver was offline cannot be reconstructed from a later
  rescan; absence is not proof of deletion (§6).
- Previously deleted messages (deleted before this archiver ever saw them), old edit
  history from before archiving began, and historical nicknames from before
  observation began cannot be reconstructed.
- Per-reactor reaction lists are not captured, only aggregate counts per emoji.
- Private threads are covered only to the extent Discord's API and the bot's granted
  permissions allow; reduced coverage is recorded explicitly in `coverage.gap_reason`,
  never silently.
- The `LIKE`-fallback search path (if trigram FTS is unavailable) is correctness-first,
  not performance-first, on large unfiltered date ranges.

## 13. Reference-project findings

**Adopted:** pfpscraper's quiet-bot conventions (manual `.env` parsing, `pythonw` +
`run.bat`, `client.run(token, log_handler=None)`, UTF-8 file logging, on-ready
reconciliation). discrawl's `doctor` command shape and its use of FTS5. ArchiveBox's
per-snapshot manifest+checksum pattern, reused for `verify`/`restore`.

**Adapted:** discrawl's periodic re-scan interval, simplified to one config value (no
multi-guild offset scheduling needed for a single server). discrawl's
include/exclude-category config, shrunk for a single known server.

**Rejected / designed around, with cause:**
- discrawl #27/#30 — a single 403 on one channel or private-thread-archive crawl
  aborted the *entire* sync; this design wraps every channel/thread in its own
  try/except (§5).
- discrawl #85 — a resumed backfill clobbered the live checkpoint, causing quadratic
  resumed syncs; this design keeps backfill and live checkpoints strictly separate
  (§4, §6).
- discrawl #25 — edited messages left duplicate FTS rows; this design keys all
  upserts, never blind-inserts.
- nemocrys/discord-archive #3 — a stale channel cache silently hid channels on
  subsequent runs; this design never trusts a cached discovery result (§5).
- nemocrys/discord-archive #2 — a restricted channel reported as indistinguishable
  from an empty one; this design's five-state coverage status (§4) exists specifically
  to prevent that.
- Tyrrrz/DiscordChatExporter discussion #999 confirms it has **no automatic bulk
  discovery** of threads/forum posts at all — users must manually gather thread IDs.
  This project's paginated, exhaustive discovery (§5) is the concrete gap it closes
  relative to the most popular existing tool.

## 14. Three additional ideas

1. **Build now** — a startup log line that diffs fresh `coverage` discovery against the
   prior run and flags any channel that just went `inaccessible`. Nearly free given the
   report-diffing already planned (§8); catches silent access loss immediately rather
   than waiting for the next report.
2. **Later** — `archive export <channel> --format json|md` for a portable per-channel
   or per-thread dump. Cheap to add once core storage/search exist; not required for
   the archive itself to be complete.
3. **Reject** — a separate periodic deep integrity checker on the *live* shards (beyond
   what `verify` already does on snapshots). Speculative maintenance-tool sprawl for a
   single-user archive; risks locking out the writer for no proven benefit; `verify`
   on snapshots already covers the real need.

## 15. Staged build order

1. Schema & fixtures — catalog + shard schema, FTS trigram-availability test, synthetic
   fixture data, migration scaffolding.
2. Full discovery — paginated channel/thread/forum-post discovery, `coverage` table,
   `doctor`/`coverage --preflight`.
3. Historical backfill — oldest-first crawl, §4.1 commit protocol, per-channel failure
   isolation.
4. Live capture & recovery — gateway handlers, §6 partial-edit and checkpoint rules,
   startup catch-up sequence.
5. Search — filter grammar, §7.1 trigram/LIKE backend, `--full`/`--context`/`--json`,
   `archive find`.
6. Reports — `server.md` + per-category reports, dirty-flag regen, coverage diffing.
7. Snapshots & restore — backup/verify/restore, manifest+checksum, atomic replace.
8. Windows background operation — `run.bat`, corrected Task Scheduler setup (§10.2),
   OS-held single-instance lock (§3), coexistence with pfpscraper.

Each stage gets its own implementation plan via the writing-plans skill, in order.
