# The content engine

The business half of Jarvis. It writes Instagram Reel scripts in the
background, on a worker thread that the voice never waits for, and stores them
in the same database as everything else.

Today it writes. It does not render and it does not publish — this document
says exactly what each of those still needs, and the `butler-doctor.bat` check
says it again against your actual machine.

---

## Turning it on

The three content tools are **off by default**. They are behind the `content`
switch in the Permissions tab, alongside `browse`, `apps` and the rest.

Off is deliberate rather than cautious. Every tool the model is given competes
for its attention on every single turn, and a fresh install that will never run
a channel should not pay for three of them in the prompt behind "open my
Spotify". Turn it on once and it stays on.

- **Permissions tab** → *Run the content engine* → on.
- Or in `%APPDATA%\JARVIS\settings.json`: `"permissions": { "content": true }`.

Nothing else needs configuring. Script writing uses the same free brain as the
`think` tool: a model on your own machine if Ollama is running, otherwise
Google's free tier on the key the voice already uses. It cannot reach a paid
model unless you have switched paid thinking on, and a hundred scripts a week
still costs nothing.

## Using it

Three things the voice understands:

| You say | What happens |
| --- | --- |
| "Write me three reels about why home espresso tastes sour" | Queues the job, answers in about ten milliseconds with a reference, writes in the background. |
| "How's the content side going?" | Recent jobs, which pipeline stage is next, and what that stage is waiting on. |
| "Read me the hooks" | Reads back the most recent finished job. |

A job survives the call that started it. Ask for scripts, hang up, come back an
hour later and ask for them again.

## Your house style

Everything the model is told about *how* to write lives in one file:

```
%APPDATA%\JARVIS\content_styles.json
```

It is written on first use and never overwritten afterwards. Edit the
`reference` profile — length, hook timing, how the narration sounds, what the
on-screen text does, what to avoid — and the next job follows it. No code
change, no restart.

**The shipped profile is a placeholder.** It was written without access to the
reference Reels, so it follows generic short-form best practice rather than
your look. Replace its fields with what those Reels actually do and set
`"placeholder": false`; until you do, the status tool and the doctor both say
so out loud.

## The pipeline

```
idea → script → voiceover → visuals → render → review → publish
       ^ built
```

| Stage | State | Needs | Cost |
| --- | --- | --- | --- |
| **script** | Built | Nothing | Free |
| **voiceover** | Not built | — | Free on edge-tts; ElevenLabs is the paid upgrade |
| **visuals** | Not built | `REPLICATE_API_TOKEN` | ~$0.003 an image, so ~2p for a six-beat Reel |
| **render** | Not built | `ffmpeg` on PATH | Free |
| **review** | Not built | — | Free |
| **publish** | Not built | `INSTAGRAM_ACCESS_TOKEN` | Free |

Each unbuilt stage names the existing plugin in `jarvis/plugins/` that already
does the work — `text_to_speech`, `generate_image`, `video_merge`,
`post_to_social` — so wiring a stage is joining two things that exist, not
writing a third.

### Publishing

Publishing is designed against the **official Instagram Graph API**, on a
Business or Creator account. Reels publishing there is a two-call flow: create
a media container pointing at a publicly reachable video URL, then publish the
container. So the render has to be uploaded somewhere reachable before the
publish call — that hosting step is part of the render stage, not the publish
stage.

Analytics read back through the same API, which is what closes the loop between
what was published and what is worth publishing next.

**Not in scope, at all:** automated account warm-up, automated likes, follows or
comments on other accounts, multi-account session rotation, and proxy rotation.
Those exist to make automation look like a person to Instagram's enforcement,
and the outcome is a banned account rather than a cheaper one. The engine
publishes what you approve, through the documented API, as you.

## Money

The free core stays free. Script writing, rendering and publishing all cost
nothing. Money belongs in the two places it buys retention — the voice and the
visuals — and every paid stage goes through the existing permission broker,
which enforces the daily spend cap in `autonomy.daily_spend_cap_usd` and
records every charge in the audit log.

## The script schema

This is the contract between the scriptwriter and every stage after it. The
model is asked for exactly this shape, and `content/models.py` enforces it on
the way in — anything that arrives wrong is repaired where repair is safe and
rejected where it is not.

```jsonc
{
  "title":   "a short internal name",        // string, optional
  "hook":    "the spoken first line",        // string, REQUIRED
  "beats": [                                 // array, REQUIRED, >= 2 entries
    {
      "at":        2.5,                      // number, seconds from the start
      "voiceover": "the line spoken here",   // string
      "on_screen": "3-5 words",              // string
      "visual":    "a prompt for an image model"
    }
  ],
  "caption":        "the Instagram caption", // string, REQUIRED
  "call_to_action": "one short line",        // string, optional
  "hashtags":       ["#example"],            // array or string
  "seconds":        30                       // number
}
```

### What each stage reads

| Stage | Reads |
| --- | --- |
| voiceover | `ReelScript.voiceover_text()` — the hook and every `beat.voiceover`, joined in order |
| visuals | one `beat.visual` per beat, one image or clip each |
| render | `beat.at` for timing, `beat.on_screen` for the burned-in caption |
| publish | `ReelScript.full_caption()` — caption, call to action and hashtags, capped at Instagram's 2,200 characters |

### Validation and repair

Repaired silently, because these are normal model behaviour rather than
errors:

- **Fenced or prose-wrapped JSON** — the widest bracketed span is extracted.
- **A single object where an array was asked for**, or an array wrapped under
  `scripts`, `reels`, `items` or `results`.
- **Beat timings** absent, zero or out of order — re-derived by spreading the
  beats evenly across `seconds`. A wrong timestamp desynchronises the captions
  from the audio, so re-deriving them is safer than trusting what arrived.
- **Timestamps as strings** (`"4.5"`) are parsed; unparseable ones become 0 and
  are then re-derived. Negative values are clamped.
- **Hashtags** as a sentence, with punctuation, duplicated, or in the hundreds
  — split, stripped to `[0-9A-Za-z_]`, lowercased, de-duplicated, capped at 12.
- **`beats` given as a string** is treated as no beats at all. A string is
  iterable, so the naive reading produces one empty shot per character and a
  script that passes every other check.
- **Captions over 2,200 characters** are truncated, because Instagram rejects
  them.

Rejected, and retried once with the problem named:

- No hook, fewer than two beats, no caption, or no visual direction anywhere.

A second failure raises with the reason. A script that cannot be produced is
reported; it is never published half-made.

## Schema of the stored tables

Both live in the same `jarvis.db` as everything else and are additive — no
table the older generation owns is touched.

```sql
CREATE TABLE content_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref         TEXT NOT NULL UNIQUE,   -- 12 hex chars, invented by the caller
    kind        TEXT NOT NULL DEFAULT 'script',
    topic       TEXT NOT NULL DEFAULT '',
    style       TEXT NOT NULL DEFAULT '',
    account     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
    stage       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,          -- ISO 8601, UTC, seconds
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT NOT NULL DEFAULT '',
    result      TEXT                    -- JSON
);

CREATE TABLE content_assets (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_ref  TEXT NOT NULL,
    kind     TEXT NOT NULL,             -- script|voiceover|image|video|caption
    path     TEXT NOT NULL DEFAULT '',  -- for stages that produce files
    body     TEXT,                      -- JSON, for stages that produce data
    meta     TEXT,                      -- JSON
    at       TEXT NOT NULL
);
```

Connections are per-thread and WAL is on, so a worker writing never blocks the
voice thread reading. Background jobs run on pool threads, so the number of
open connections is bounded by that pool rather than by the number of jobs.

## What each remaining stage needs, exactly

### voiceover

Free on edge-tts, which needs no key. `jarvis/plugins/tts_batch.py` already
accepts a list of strings and returns saved paths.

For the paid upgrade: `ELEVENLABS_API_KEY`, and a voice id in
`voice.elevenlabs_voice_id`. This is the line item worth spending on first.

### visuals

`REPLICATE_API_TOKEN`. `jarvis/plugins/image_flux.py` defaults to
`black-forest-labs/flux-schnell` at roughly $0.003 an image. Pass
`aspect_ratio: "9:16"`. The plugin declares `CAPABILITY = CAP_PAYMENT_SPEND`,
so every call is judged by the permission broker against
`autonomy.daily_spend_cap_usd` before a penny moves.

### render

`ffmpeg` on PATH. Nothing else, and no cost. `jarvis/plugins/video_merge.py`
already merges video and audio.

Output target: 1080×1920, H.264, AAC, under 90 seconds.

### publish

The official **Instagram Graph API**, on a Business or Creator account. What
this needs, in order:

1. An Instagram **Business or Creator** account — not a personal one.
2. It must be linked to a Facebook Page.
3. A Meta app with the `instagram_content_publish` permission (and
   `instagram_basic`; add `instagram_manage_insights` for analytics).
4. A long-lived access token, stored as `INSTAGRAM_ACCESS_TOKEN`. Store it in
   the vault via the settings panel — never in a file in this repository.
5. The IG user id for the account.

Publishing a Reel is two calls:

```
POST /{ig-user-id}/media
     media_type=REELS
     video_url=<a URL Instagram can fetch>
     caption=<the full caption>
  -> returns a creation id

POST /{ig-user-id}/media_publish
     creation_id=<that id>
```

The consequence worth planning for: **`video_url` is a URL, not a file
upload.** The rendered file has to be somewhere Instagram can reach before the
publish call, which makes hosting part of the render stage rather than an
afterthought. The container also takes time to process; poll `status_code` on
the creation id until it reads `FINISHED` before publishing.

Rate limits are per account and generous for a publishing schedule of a few
Reels a day. Analytics read back from `/{media-id}/insights`.

### What is deliberately absent

No account warm-up, no automated likes, follows or comments on other accounts,
no multi-account session rotation, no proxy rotation. All of those exist to
make automation look like a person to Instagram's enforcement, and the outcome
is a banned account rather than a cheaper one — and it takes the channel and
its history with it. The engine publishes what you approve, through the
documented API, as you.

## Measured behaviour

The design claim is that the voice never waits for the business. It is
measured rather than asserted, in `tests/test_latency.py`, on a persistent
event loop with the background host working and the database being written to
continuously.

| Measurement | p50 | p95 | max |
| --- | --- | --- | --- |
| All three tools | 0.40 ms | 2.65 ms | 7.50 ms |
| `write_reel_scripts` alone | 0.33 ms | 0.82 ms | 1.12 ms |
| Voice event loop lag | 0.83 ms | 2.98 ms | 4.50 ms |

Loop lag is the number that matters most: it is how late a callback runs on
the loop the audio pipeline shares, and therefore the closest available proxy
for whether a listener would hear a stutter. Audio frames arrive every 10–20
ms; the tests fail above 20 ms.

**The pathological case, recorded rather than hidden.** With four pure-Python
CPU-bound jobs running flat out, tool latency rises to a p50 of about 51 ms and
loop lag to about 78 ms. That is CPython's global interpreter lock, not this
design — no amount of care in a tool makes it responsive while four threads
never yield the interpreter. The engine does not create that load: its jobs
wait on model APIs and on ffmpeg in a subprocess of its own, both of which
release the GIL, and the shipped concurrency is one job at a time. Anyone
raising concurrency and adding CPU-bound work should reach for a subprocess.

### Why the reference is invented before the row is written

`queue_scripts` does no database work at all. The job reference is a random
string rather than a row id, so it can be created in memory and the row
written on the worker.

The obvious alternative — write the row, return its reference — profiles at
well under a millisecond and looks fine. The cost is not the write, it is the
**lock**. One busy writer and the voice thread joins the queue behind it:
measured under a saturated host, writing the row on the voice path put the
tool at 1.4 seconds at p95 and occasionally failed outright with "database is
locked", raised at the person talking.

## Where the code is

| File | What it is |
| --- | --- |
| `src/workers.py` | The background host: a daemon thread with its own event loop and a job queue. Nothing here touches the voice loop. |
| `src/content/styles.py` | The editable house style, and the prompt block built from it. |
| `src/content/models.py` | A Reel script as a structured object — hook, timed beats, caption, hashtags. |
| `src/content/scriptwriter.py` | The agent that writes it, on a free brain, with one honest retry. |
| `src/content/store.py` | Jobs and assets, in `jarvis.db`, one connection per thread. |
| `src/content/pipeline.py` | The stages, what each one needs, and the job that runs today. |
| `src/content/tools.py` | The three things the voice can ask for. |

| Tests | Covers |
| --- | --- |
| `tests/test_workers.py` | The happy path: queueing, running, failing. |
| `tests/test_workers_lifecycle.py` | Cancellation, eviction, shutdown, concurrent reads. |
| `tests/test_content.py` | Script shape, job records, what the voice sees. |
| `tests/test_content_edges.py` | Malformed model output, schema repair, database contention, the permission switch. |
| `tests/test_latency.py` | The promise: tool latency and voice-loop lag under load. |

## Why the background thread

A Reel script takes most of a minute to write. A realtime voice session cannot
lose a minute: the audio pipeline has to keep feeding frames, and a tool that
blocks the event loop does not raise anything — it just produces an assistant
that has stopped listening, with silence as the only symptom.

So the content engine runs on its own thread with its own event loop. Blocking
work inside a job goes to a thread from there as well, so one slow job cannot
stall the jobs behind it either. Submitting a job is a queue push measured in
microseconds, which is why the voice can answer "started" in the same breath as
the request.

Jobs run one at a time by default. These jobs spend money and share one browser
profile; serialising them is worth more than throughput.

### Cancelling

`WorkerHost.cancel(ref)` stops a queued or running job. A queued job is simply
never started. A running coroutine has its task cancelled and stops at its next
await point.

A running job that is a plain blocking function is an honest half-promise:
Python cannot stop a thread from outside, so the caller is freed immediately
and the work runs to its own end with nobody waiting for the result. Jobs that
may genuinely need stopping mid-flight should be written as coroutines with
await points — or, for anything CPU-heavy, run in a subprocess that can be
killed.
