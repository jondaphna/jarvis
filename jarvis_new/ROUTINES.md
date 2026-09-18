# Standing routines

The things Jarvis does without being asked: a morning briefing, a check on the
machine, a reminder at nine, a batch of Reel scripts every Monday. They are
defined in the settings panel, stored in SQLite, and run on the background
worker host — so they fire whether or not you are in a call, and firing one
costs the conversation nothing.

---

## The gap this filled

The older generation (`jarvis/`) has a perfectly good mission scheduler:
`jarvis.core.scheduler.MissionScheduler`, APScheduler, cron triggers, a run
history. It has never once run in this generation. It hangs off
`jarvis.core.assistant.Assistant`, and the LiveKit voice agent never
constructs that object. So a mission saved in the settings panel was written
to `%APPDATA%\JARVIS\missions\*.json`, was correct, and sat there.

That is why this is new code rather than a wire-up. The old scheduler wants to
own an event loop and an application object; this generation already has a
background host with a loop of its own, and the right thing to hang a ticker
off is that.

---

## Using it

### From the settings panel

The Rules tab lists every routine with its schedule in plain words, when it
next fires, when it last ran and what it said. Each one has a switch, a **Run
now** button, and a run history.

### What ships

A fresh database is seeded once with three:

| Routine | When | Action | On? |
| --- | --- | --- | --- |
| Good morning briefing | 07:30 daily | `briefing` | yes |
| Morning system check | 07:25 daily | `system_check` | yes |
| Evening wrap-up | 21:00 daily | `note` | no |

Seeding asks whether the table has *ever* had rows, not whether these
particular ids are present — so one you delete stays deleted.

Nothing seeded touches the outside world. Two read the local machine; the
third says nothing until you put something in its instruction box.

### How a briefing reaches you

It does not speak to an empty room. At 07:30 there is no call in progress, so
the routine writes what it would have said into `routine_runs` and leaves it
undelivered. The next time a call starts, that text is folded into the agent's
system prompt under `# While you were away`, and Jarvis leads with it.

It is marked delivered at that moment rather than after he mentions it. The
alternative is a briefing repeated in every call until it happens to come up.

---

## Writing a schedule

Five-field cron, plus the forms a person actually types. All of these are
accepted and stored as cron:

| You type | Stored as |
| --- | --- |
| `07:30`, `7:30`, `at 07:30`, `7.30` | `30 7 * * *` |
| `9:15 pm` | `15 21 * * *` |
| `@daily`, `@midnight` | `0 0 * * *` |
| `@hourly` | `0 * * * *` |
| `@weekly` / `@monthly` / `@yearly` | `0 0 * * 0` / `0 0 1 * *` / `0 0 1 1 *` |
| `every 15 minutes` | `*/15 * * * *` |
| `every 2 hours` | `0 */2 * * *` |
| `weekdays at 07:30` | `30 7 * * 1-5` |
| `weekends at 10:00` | `0 10 * * 0,6` |

Inside a field: `*`, `5`, `1-5`, `*/15`, `9-17/4`, `6,12,18`, and names
(`mon`, `monday`, `jan`). Sunday may be written `0` or `7`.

Two behaviours worth knowing:

- **Day-of-month and day-of-week are OR, not AND**, when both are restricted.
  `0 0 13 * 5` is the thirteenth *or* any Friday. That is Vixie cron's rule
  and it surprises everyone.
- **A schedule that can never fire returns nothing rather than hanging.**
  `0 0 30 2 *` is legal and impossible; the search gives up after five years.

Validation happens when you press save, not at four in the morning. A schedule
nobody can parse fails while you are still looking at the screen.

There is no APScheduler dependency. `src/routines/cron.py` is about two
hundred lines and is what `tests/test_routines_cron.py` spends seventy-nine
tests on.

---

## What a routine can do

| Action | What it does | What it needs |
| --- | --- | --- |
| `briefing` | The date, the machine, what the content engine did overnight, anything failing, plus your own instruction. Tidied by the free brain if one is configured. | Nothing |
| `system_check` | Processor, memory, disk, battery, worrying things first. | Nothing |
| `note` | Says exactly what is in the instruction box. | Nothing |
| `instruct` | Puts the instruction to the thinking brain and keeps the answer. | Ollama, or a Google key |
| `content_scripts` | Queues three Reel scripts on the topic in the instruction box. | The content engine |

Every action returns a sentence rather than raising, wherever the failure is a
fact about the world. A missing API key at half past seven produces "I
couldn't carry out the morning digest: Thinking needs a Google key" — not a
stack trace in a log nobody opens.

**None of them posts anything anywhere.** There is no publishing action, and
the content one only queues.

---

## Schema

Both tables are additive. An older build opening `jarvis.db` sees exactly what
it saw before.

```sql
CREATE TABLE IF NOT EXISTS routines (
    id            TEXT PRIMARY KEY,     -- slug of the name, ASCII, URL-safe
    name          TEXT NOT NULL,
    action        TEXT NOT NULL DEFAULT 'briefing',
    instruction   TEXT NOT NULL DEFAULT '',
    schedule      TEXT NOT NULL DEFAULT '',   -- always stored as 5-field cron
    timezone      TEXT NOT NULL DEFAULT '',   -- IANA name; blank means local
    enabled       INTEGER NOT NULL DEFAULT 1,
    catch_up      INTEGER NOT NULL DEFAULT 1,
    grace_seconds INTEGER NOT NULL DEFAULT 3600,
    speak         INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,        -- UTC ISO-8601, seconds resolution
    updated_at    TEXT NOT NULL,
    next_run_at   TEXT,                 -- UTC ISO-8601; NULL means disarmed
    last_run_at   TEXT,
    last_status   TEXT NOT NULL DEFAULT '',   -- done | failed | skipped
    last_output   TEXT NOT NULL DEFAULT '',
    last_error    TEXT NOT NULL DEFAULT '',
    runs          INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_routines_due ON routines(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS routine_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id  TEXT NOT NULL,
    job_ref     TEXT NOT NULL DEFAULT '',   -- the worker job that ran it
    trigger     TEXT NOT NULL DEFAULT 'schedule',  -- schedule | manual
    due_at      TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    output      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    delivered   INTEGER NOT NULL DEFAULT 0  -- has he been told yet
);
CREATE INDEX IF NOT EXISTS idx_routine_runs_routine
    ON routine_runs(routine_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_routine_runs_undelivered
    ON routine_runs(delivered, id DESC);
```

Timestamps are UTC ISO-8601 strings, so they sort lexicographically and mean
the same thing after the clocks change. Clock times in a schedule are read in
your own timezone, or the one the routine names.

Run history is trimmed to the newest 60 rows per routine.

---

## How a firing works

1. The ticker wakes every 20 seconds on the worker host's event loop and hands
   the database work to a thread, so even the check is off the loop.
2. Any enabled routine whose `next_run_at` is in the past is a candidate.
3. Its next time is computed and written **with the old one in the WHERE
   clause**:

   ```sql
   UPDATE routines SET next_run_at = ?
    WHERE id = ? AND next_run_at = ? AND enabled = 1
   ```

   Exactly one process can win that write. The voice agent and the settings
   API both look at these rows; without this, both would fire the morning
   briefing and you would hear it twice.
4. The winner submits the work to the worker queue, where it serialises behind
   whatever else is running.
5. The result goes to `routine_runs`, and waits there for the next
   conversation.

### Catching up after the machine was off

`next_run_at` is left exactly as it is on start-up, including a time in the
past. That is what makes a missed briefing survive a reboot: the row still
says 07:30, the process comes up at 08:10, and the *catch-up rule* — not the
arming — decides.

- `catch_up = 1` (default): it runs, however late.
- `catch_up = 0`: it runs if it is inside `grace_seconds`, otherwise the run
  is recorded as `skipped` with how late it was, and the schedule moves on.

A briefing missed for a week fires **once**, not seven times.

---

## Where the code is

| File | What it is |
| --- | --- |
| `src/routines/cron.py` | The schedule parser and `next_after`. No dependencies. |
| `src/routines/store.py` | The two tables, and the compare-and-swap claim. |
| `src/routines/actions.py` | What a routine actually does. |
| `src/routines/engine.py` | Arming, claiming, firing, the ticker, the prompt blocks. |
| `src/workers.py` | `spawn()` — a long-lived task beside the job queue. |
| `src/agent.py` | `start_routines()` and the prompt blocks. |
| `src/control_api.py` | `/api/routines`, `…/run`, `…/enabled`, `…/runs`. |
| `src/doctor.py` | `check_routines()`. |

### Why the ticker is spawned rather than submitted

The shipped worker concurrency is **one**. A ticker submitted to the job queue
would occupy that single slot forever and no content job would ever run again.
So `WorkerHost.spawn()` puts it on the loop *beside* the queue: same thread,
same isolation from the voice, no queue slot. A spawned task that raises says
so and is not collected in silence — a scheduler that dies quietly is
indistinguishable from one that was never set up.

---

## Measured

With the ticker running and the worker host under load, on a persistent event
loop:

| | p50 | p95 | max |
| --- | --- | --- | --- |
| All three content tools, routines ticking | 0.50 ms | 3.45 ms | 5.16 ms |
| Voice event loop lag, routines ticking | 0.74 ms | 1.12 ms | 2.03 ms |
| Status tool while a routine actually fires | 1.03 ms | 1.72 ms | 2.14 ms |
| Voice loop lag while a routine actually fires | 0.45 ms | 1.14 ms | 1.81 ms |

The budget is 20 ms. `tests/test_latency.py` fails the build below it.

---

## Tests

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_routines_cron.py` | 79 | Parsing, next-firing arithmetic, the OR rule, impossible dates |
| `tests/test_routines_store.py` | 40 | Persistence, the claim under eight threads, trimming, delivery |
| `tests/test_routines_engine.py` | 55 | Arming, firing, catch-up, restart survival, double fire, the ticker |
| `tests/test_routines_actions.py` | 35 | Every action, and every way it degrades |
| `tests/test_routines_wiring.py` | 26 | That it is actually joined up to agent, API and doctor |

Two of them were run against deliberately broken implementations to confirm
they discriminate:

- Replace the compare-and-swap with a plain UPDATE → the two double-fire tests
  fail.
- Make `arm()` overwrite an existing `next_run_at` → the restart-survival test
  fails.
