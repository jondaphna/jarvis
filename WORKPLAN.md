# Work plan

Where the eleven items stand, what I'd build in what order, and what each one
actually needs from you. Honest about size: some of these are an evening, some
are a week, and one I'd argue against.

---

## Done

### The brain — two halves instead of one ✅

The complaint was "it can do basic things but can't manage to do stuff". That
was a model problem, not a code problem: a realtime speech model is tuned for
sub-second replies, not for holding several steps in mind.

So the voice keeps the ears and mouth, and hard problems go to Claude Opus 5
with a focused brief per kind of work — planning, research, writing,
engineering. The specialist modes are the useful half of a multi-agent design
without the expensive half: a router that classifies *every* message costs a
model call before anything happens, even on "what's the time". Here the voice
delegates only when it already knows it needs to, so simple turns stay instant.

### 11. Settings & permissions sandbox matrix ✅

All three parts are in the settings panel now.

- **Permissions tab** — a switch per capability: open websites, read pages,
  click and type, open apps, volume and music, machine stats, memory, power.
  Switching one off **removes those tools** before the model is given them. It
  isn't asked to behave; it has no way to do the thing. Power actions start off.
- **Rules tab** — standing instructions and a never-do list, in your words,
  read at the start of every conversation.
- **Commands tab** — exact words in, exact words out.

### The content engine — the business half, on a background worker ✅

The complaint this answers is structural rather than a feature: `jarvis_new/`
had nowhere to do work that takes longer than a sentence. Missions saved from
the settings panel were never executed, because the scheduler only exists on
the older generation's `Assistant`, which the voice agent never builds.

So there is now a background host — its own thread, its own event loop, its own
queue — and the first thing running on it is the Reel scriptwriter. Ask for
three scripts and the answer comes back in about two milliseconds with a
reference; the writing happens behind the conversation and the result is in
SQLite when you come back to it.

Measured rather than asserted, under a busy host and a database being written
to continuously: **p95 of 2.65ms across the three tools, and the voice event
loop running 2.98ms late at p95** — the number that stands in for audio
stutter. See `jarvis_new/CONTENT_ENGINE.md` for the stage map, the schema and
the API configuration each remaining stage needs.

Stages: **script** built and free. **voiceover**, **visuals**, **render**,
**review** and **publish** declared, each naming the existing plugin that
already does the work. Publishing is designed against the official Instagram
Graph API for Business and Creator accounts.

### The browser, which everything else leans on ✅

Jarvis opens an ordinary Chrome and attaches to it. That means one window, the
same tab reused, signed into your accounts, and Jarvis can read it, type in it
and click in it. Items 4, 7 and 10 below all depend on this and now have it.

---

## Next, in this order

### 0. Finish the content pipeline — *two to three days, stage by stage*

The scriptwriter is one of six stages. The rest are wiring existing plugins to
the worker rather than new inventions, and they are worth doing in this order
because each one makes the previous one testable end to end.

**Voiceover — half a day, free.** `jarvis/plugins/tts_batch.py` already turns a
list of strings into audio files, and `ReelScript.voiceover_text()` is exactly
that list. edge-tts costs nothing and is good enough to prove the pipeline;
ElevenLabs is the upgrade and is where the first pound of budget belongs,
because the voice is what holds a viewer.

**Visuals — half a day.** One image per beat from `beat.visual`, through
`jarvis/plugins/image_flux.py` on Replicate. About $0.003 an image, so roughly
two pence for a six-beat Reel.

**Render — a day.** ffmpeg locally: stills to 9:16, the voiceover over the top,
`beat.on_screen` burned in at `beat.at`. Free, and the stage with the most
fiddly detail in it.

**Review — half a day.** The finished file and its caption somewhere you can
watch it before anything is published. Deliberately between render and publish:
the first weeks of a new channel are worth watching by eye.

**Publish — half a day, plus your setup.** The two-call Graph API flow.

**What I need from you:** an Instagram Business or Creator account linked to a
Facebook Page, a Meta app with `instagram_content_publish`, and a long-lived
access token. Also somewhere the render can be uploaded that Instagram can
reach — the Graph API takes a public URL, not a file upload. And, separately
from any of that: a description of the six reference Reels, so the house style
stops being the shipped placeholder.

### 1. Google Calendar & Gmail — *about a day*
The biggest daily win, and the tasks system is already there to hang it on.

**What I need from you:** a Google Cloud project with the Calendar and Gmail
APIs enabled, and an OAuth client ID (Desktop app). You'd approve access once in
a browser window. I'll never see the password — OAuth hands back a token.

Worth doing first because "what's my day look like" and "reply to that email"
are things you'd use daily, and because the scheduled-task system can then brief
you each morning without being asked.

### 2. Autonomous web research → written report — *about a day*
Search several sources, read them, synthesise, save a Markdown summary. The
browser and search tools already exist, so this is mostly orchestration and a
decent prompt. Nothing needed from you.

### 3. Semantic workspace search — *two days*
A local index over folders you nominate, so "what did I write about the Q3
launch" finds it. Uses a local embedding model, so nothing leaves the machine
and there's no API cost.

**What I need from you:** which folders. Not your whole drive — pick two or
three that matter.

### 4. Active window & context indexing — *a day, Windows-specific*
Jarvis knows what you're looking at without being told. Reads window titles via
the Windows accessibility API.

Worth pausing on: this means a running log of everything you have open. I'd keep
it in memory only, never written to disk, and put it behind its own permission
switch that starts off.

### 5. Clipboard history — *half a day*
Encrypted local buffer, recall by voice. Same privacy note: your clipboard
catches passwords sometimes. I'd exclude anything copied from a password
manager, skip entries that look like credentials, and cap history at a few hours.

### 6. Spotify soundscapes & proper playback control — *half a day*
Media keys already work. The Spotify Web API would add "play this specific
playlist", queueing, and picking music to match what you're doing.

**What I need from you:** a Spotify developer app (free) — client ID and secret.
Premium is required for playback control; that's Spotify's rule, not mine.

### 7. Multi-agent orchestrator — *three to four days*
Splitting into research / coding / system agents with a router. Genuinely
useful for long multi-step jobs.

I'd do this **after** the items above, not before. It's an architectural change
that makes everything harder to debug, and right now the value is in giving
Jarvis more things to do rather than reorganising how it decides.

### 8. Speech-to-code "rewrite" mode — *a day*
"Jarvis, rewrite" hands your spoken instructions to Claude Code to edit this
repo. Straightforward, and it needs a firm boundary: changes go to a branch,
never to your working code directly, and nothing is committed without you seeing
a diff.

### 9. Real-time web security guard — *two days, and honestly*
Warning you about phishing pages sounds great and is hard to do well. A
home-made checker produces false alarms, and an alarm you learn to ignore is
worse than none. I'd do it properly — Google Safe Browsing's API, which is free
and is what Chrome itself uses — rather than invent heuristics.

---

## The one I'd argue against

### 10. Automated trading with live execution

Backtesting and chart analysis: happily, any time — it's just data.

Placing **live trades by voice** is a different thing. Speech recognition
mishears numbers, and this is the one capability on the list where a mishearing
costs money that doesn't come back. "Sell fifty" and "sell fifteen" are one
noisy room apart.

If you want it, here's how I'd build it and nothing looser:

- Paper trading by default. Live execution behind its own permission switch that
  starts off.
- Every order read back in full and confirmed out loud before it goes.
- A per-order and per-day cap you set in the settings, enforced in code rather
  than by asking the model nicely.
- Never on a scheduled task. Only when you're there, talking to it.

Your call — say the word and I'll build it that way.

---

## What I need from you, all together

| For | What |
|---|---|
| Publishing Reels | An Instagram Business/Creator account, a Meta app with `instagram_content_publish`, a long-lived token, and public hosting for the rendered file |
| The house style | A description of the six reference Reels, or the files themselves |
| Premium voiceovers | An ElevenLabs key (optional — edge-tts is free) |
| Generated visuals | A Replicate token (~$0.003 an image) |
| Calendar & Gmail | A Google Cloud OAuth client ID (Desktop app) |
| Spotify control | A Spotify developer client ID + secret, and Premium |
| Workspace search | Which folders to index |
| Security guard | A Safe Browsing API key (free) |
| Trading, if you want it | A broker account with paper trading on |

Nothing else needs anything from you. Say which to start and I'll work down the
list.
