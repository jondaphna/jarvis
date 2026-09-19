# What happens when something goes wrong

Three failures this build used to handle badly, and what it does now. None of
them is exotic: a machine that gets shut down mid-job, an API that fails for
ten seconds, and two parts of the same process each opening their own handle
to the same file. All three are the ordinary weather of a program that runs in
the background on somebody's laptop.

There is no new configuration here. Every change below is on by default except
retries, which are asked for per job.

---

## One connection per file, per thread

`src/db.py`

Both stores — content jobs and standing routines — live in the same
`jarvis.db`. Until this existed, each opened its own connection on each
thread: the worker thread held two handles to one file, the voice thread held
two more, and every one of them was a separate WAL reader with its own
snapshot and its own share of the lock traffic.

Nothing about that is catastrophic. It is simply twice as much of the one
thing this codebase has actually been bitten by, which was SQLite lock
contention on the voice path.

```python
conn = db.connect(path, SCHEMA)   # this thread's handle, opened once
```

- **One connection per (file, thread).** Asking twice on one thread returns the
  same handle. Two threads never share one, because sharing a `sqlite3`
  connection across threads is the bug that surfaces once a fortnight under
  load and never in a test.
- **Each caller's schema applied once per connection**, tracked beside the
  connection rather than in a module flag, and cleared when the connection is.
  A flag says "somebody already did this" about a connection that may not
  exist any more, and the next query then fails on a table nothing created.
- **The same file spelled two ways is one file.** Paths are resolved, so
  `…/sub/../jarvis.db` and `…/jarvis.db` share a handle.
- **A way to let go.** `close_thread()` on the way down, `close_path()` for one
  file, `close_all()` for everything. A connection another thread owns cannot
  be closed from here — sqlite3 guards that as it guards *using* one — so it is
  released instead and closes when the last reference to it goes.

The pragmas are the ones the latency work settled on: WAL, so a long write on
the worker never blocks a read on the voice thread, and a 15-second busy
timeout, so contention is a wait rather than an exception raised at whoever is
talking.

---

## A job whose process died

`ContentStore.recover_interrupted()`, `RoutineStore.recover_interrupted()`,
`content.pipeline.recover_interrupted()`

A job row is written before the work begins and updated when it ends. Between
those two writes the process can simply stop existing: a reboot, a power cut, a
laptop lid closed on a render. The row is then stuck on `running` for good.
Nothing crashes and nothing is logged. The symptom is a status tool reporting
work in progress that nothing anywhere is doing, and a job reference somebody
was given which never resolves into anything.

The hard part is telling that apart from a job **another running copy is
working on right now**, which must not be touched. So every job and every
routine run is stamped with the process that owns it:

```
owner = "12345:1758140000"     # process id : that process's start time
```

A bare process id is not enough — ids are reused, so after a reboot a row can
read as belonging to whatever happens to be running under that number. Pairing
it with the start time makes the tag unforgeable by coincidence.

At start-up, `src/agent.py` submits recovery to a worker. Anything owned by a
process that is provably gone is closed as `failed` with the reason
"interrupted — the process running this job stopped before it finished". A
routine's summary row is cleared too, but only if it still says `running`: a
later successful run must not be overwritten by an older abandoned one.

Unknown or unreadable ownership counts as dead. That is the safe direction:
the cost of being wrong is a job marked interrupted that was fine, against the
cost of the other error, which is a job stuck on `running` until somebody
notices.

---

## A failure that clears

`WorkerHost.submit(..., retries=..., backoff=...)`

Everything the business engine does in the background talks to something that
fails for a second and then doesn't: a model endpoint that rate-limits, a
socket that resets, a file being written by something else. A scriptwriting
job that hit one of those was simply gone.

```python
workers.host().submit(write, topic, name="scripts", retries=2)
```

`retries` is how many **extra** attempts a failure may have, with a delay that
starts at two seconds and doubles, capped at thirty. It is off by default,
because a retry is only ever right for work that is safe to run twice and the
caller is the only one who knows whether it is. Script generation asks for two;
it writes its job row defensively so a second attempt on the same reference is
harmless.

Never retried: a cancelled job, and a job whose host is shutting down. The
attempt count appears in `status()` only for jobs that asked for retries.

The wait happens in the worker, so at concurrency 1 it does hold the queue
while it waits — at most six seconds for the two retries anything asks for
today. That is the accepted cost; what would not be acceptable is the queue
never recovering, which is a test.

---

## Work that was being done more than once

None of these was a wrong answer. Each was the same correct answer computed
three or four times behind a tool the voice calls while somebody is waiting.

| Where | Was | Now |
| --- | --- | --- |
| `styles._read()` | Four reads and four JSON parses of `house_style.json` per scriptwriting prompt, one of them inside a voice tool | Read once, re-read only when the file's modification time or size changes |
| `Stage.as_dict()` | `missing()` — which decrypts the key vault, imports packages and searches PATH — called three times per stage, six stages, per status call | Once per stage |
| `Scriptwriter` system prompt | Rebuilt for the prompt and again for the retry | Built once |

Measured on this machine, on a persistent event loop:

| | Before | After |
| --- | --- | --- |
| `readiness()` | 0.75 ms | 0.33 ms |
| `content_engine_status` tool | 0.90 ms | 0.57 ms |

The style cache is keyed on the file's own modification time and size rather
than a timer, so editing the file still takes effect on the next job with
nothing to restart — the property the whole "style is a file" design rests on.

`permissions.allowed()` was measured at 0.068 ms and deliberately left alone.

---

## Asking for JSON, and saying how long it may be

`Backend.ask_json()` in `src/thinker.py`

Background agents ask for a structured answer and then parse whatever comes
back. Saying so in the prompt works most of the time; saying so in the
*request* works more of the time, and the difference is a wasted model call,
thirty seconds, and a slice of a free daily quota.

The length is the other half, and the more expensive one. The conversation's
answer budget was four thousand tokens for everything, because a spoken answer
is short. A batch of five Reel scripts with beats, captions and hashtags is
not short — it runs to about six thousand — so the fifth script was cut off two
thirds of the way through and the array failed to parse. That is the failure
the salvage below exists to survive, and this is the one that stops it
happening.

```python
backend.ask_json(system, prompt, "medium", max_tokens=room)
```

- **Gemini** gets `response_mime_type="application/json"` and the room asked
  for. A model that refuses the mime type is asked plainly instead rather than
  failing the job: a model one release behind must not break every script job.
- **Ollama** gets `"format": "json"` and `num_predict`.
- **Claude** has no JSON mode in its API, so it inherits the base
  implementation, which is the plain question. A background agent can ask any
  backend the same way regardless.
- **A backend that has never heard of it** — every fake the tests inject — gets
  the plain `ask()` it always got. The caller checks the signature rather than
  requiring the method.

The scriptwriter sizes the request to the batch, and a retry that needs one
more script asks for one script's worth of room, not a batch's.

`SPOKEN_MAX_TOKENS` is unchanged at 4096. Nothing here touches the voice path.

---

## A truncated batch is no longer thrown away

`scriptwriter._salvage_truncated()`

The most expensive failure the scriptwriter has: five scripts is a long answer,
a long answer is the one that hits the output token limit, and the result was
four perfectly good scripts discarded because the fifth stopped mid-sentence
and the whole thing failed to parse.

The array is now walked with string and escape handling, and everything up to
the last **complete** element is kept. Nothing is invented — a half-written
script is dropped, not guessed at. A batch that parses normally never reaches
this path.

---

## Tests

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_db_pool.py` | 37 | Sharing, pragmas, closing, migrations, ownership, and that both stores really do share one handle |
| `tests/test_recovery.py` | 20 | What a crash leaves behind, what must not be touched, and that a broken database cannot stop the agent starting |
| `tests/test_workers_retry.py` | 13 | Retrying, the growing wait, and everything that is never retried |
| `tests/test_efficiency.py` | 17 | Counting the work rather than timing it: one file read, one check per stage, the salvage |
| `tests/test_structured_answers.py` | 27 | The JSON request, the length budget, and every way a backend can not support them |

Counting rather than timing is deliberate. A count is stable on a loaded
machine; a timing assertion that flakes gets deleted, and the thing it was
protecting stops being protected. The timing assertions that do exist live in
`tests/test_latency.py`, which has a 20 ms budget and fails the build above it.
