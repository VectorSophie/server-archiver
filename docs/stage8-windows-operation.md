# Running `archive live` unattended on Windows

`run_live.bat` starts `archive live` with `pythonw.exe` (no console
window) and `cd`s into the repo first so relative paths (`config.json`)
resolve. If `pythonw.exe` isn't on your PATH, edit the batch file and
give it the full path (e.g. `C:\Python314\pythonw.exe`).

## Register it in Task Scheduler

1. Open Task Scheduler -> **Create Task...** (not "Create Basic Task" --
   the full dialog has the settings below).
2. **General** tab: name it (e.g. "Archive Live"). Check **Run whether
   user is logged on or not** if you want it running even when locked.
3. **Triggers** tab -> New -> **At log on** (or **At startup** for a
   service-like always-on box).
4. **Actions** tab -> New -> **Start a program** -> Program/script:
   the full path to `run_live.bat` in this repo.
5. **Settings** tab: uncheck **Stop the task if it runs longer than...**
   (it's meant to run indefinitely). Leave "If the task is already
   running" at its default ("Do not start a new instance") -- belt and
   suspenders alongside the lock file described below.

## What happens if it's already running

`archive live` takes an exclusive lock on `<data_dir>/.live.lock` on
startup. A second launch (a manual run while the scheduled one is still
up, or Task Scheduler double-firing) exits immediately with a message
instead of writing to the same shards concurrently. Closing or killing
the running process releases the lock automatically -- there's no lock
file to manually delete after a crash.

## Logs

`pythonw.exe` has no console, so stdout/stderr are redirected to
`logs/live-YYYYMMDD.log` (next to `config.json`) before anything else
runs -- one file per calendar day. Nothing deletes old log files
automatically; clean up `logs/` by hand occasionally if it grows large.

## Syncing to the deployed remote mirror

`run_sync.bat` runs `archive sync` (see `docs/superpowers/specs/` for
the sync design), pushing any changed archive files to the deployed
VM. Register it the same way as above, except the trigger should
repeat on an interval -- in the Triggers tab, after picking **On a
schedule**, check **Repeat task every** and set it (30 minutes is a
reasonable default). This task is quick and exits on its own each run,
so it doesn't need "run whether logged on or not" unless you want it
to work while the machine is locked.

Requires `sync_remote_host`, `sync_remote_user`, `sync_remote_data_dir`,
and `sync_ssh_key_path` set in `config.json` (see
`config.example.json`); `archive sync` prints which key is missing and
exits non-zero if any aren't set, rather than silently doing nothing.
