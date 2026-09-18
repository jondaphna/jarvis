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

Tests: `tests/test_workers.py` and `tests/test_content.py`.

## Why the background thread

A Reel script takes most of a minute to write. A realtime voice session cannot
lose a minute: the audio pipeline has to keep feeding frames, and a tool that
blocks the event loop does not raise anything — it just produces an assistant
that has stopped listening, with silence as the only symptom.

So the content engine runs on its own thread with its own event loop. Blocking
work inside a job goes to a thread from there as well, so one slow job cannot
stall the jobs behind it either. Submitting a job is a queue push measured in
milliseconds, which is why the voice can answer "started" in the same breath as
the request.

Jobs run one at a time by default. These jobs spend money and share one browser
profile; serialising them is worth more than throughput.
