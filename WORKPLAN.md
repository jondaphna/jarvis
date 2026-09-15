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

### The browser, which everything else leans on ✅

Jarvis opens an ordinary Chrome and attaches to it. That means one window, the
same tab reused, signed into your accounts, and Jarvis can read it, type in it
and click in it. Items 4, 7 and 10 below all depend on this and now have it.

---

## Next, in this order

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
| Calendar & Gmail | A Google Cloud OAuth client ID (Desktop app) |
| Spotify control | A Spotify developer client ID + secret, and Premium |
| Workspace search | Which folders to index |
| Security guard | A Safe Browsing API key (free) |
| Trading, if you want it | A broker account with paper trading on |

Nothing else needs anything from you. Say which to start and I'll work down the
list.
