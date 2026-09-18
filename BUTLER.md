# Jarvis, the voice butler

> **Looking for how to use it?** [GUIDE.md](GUIDE.md) is the full manual — every
> button, everything it can do, and what to do when it misbehaves. This file is
> just the setup notes.


This is [ruxakK/jarvis-voice-butler](https://github.com/ruxakK/jarvis-voice-butler)
— the project from the video — copied in and wired to your keys. The code is his
and LiveKit's, unchanged. What I added is the setup: four `.bat` files and a
script that fills in the two config files it needs.

You talk to it, and it drives a real Chromium browser — opening sites,
searching, clicking, typing, reading pages back to you.

---

## Setup — once

Double-click **`butler-setup.bat`**.

It installs `uv` if you don't have it, installs the Python packages, downloads
the browser Jarvis drives, and fills in your keys. If your keys are already in
the JARVIS vault it finds them; otherwise it asks, once.

Keys are checked before anything is written: the shape first, then the key
itself against Google and LiveKit. If a key is wrong you get one line saying
so, not a page of websocket errors half an hour later.

You need Node.js. If it stops and says so, get the LTS build from
[nodejs.org](https://nodejs.org) and run it again.

### The four other buttons

| | |
|---|---|
| **`butler-check.bat`** | Are my keys still good? Asks Google and LiveKit. |
| **`butler-keys.bat`** | Re-enter the keys from scratch. |
| **`butler-devices.bat`** | List microphones and speakers, with their numbers. |
| **`butler-talk.bat 1 4`** | Talk using microphone 1 and speakers 4. |

## Running it

**The fastest check — does it talk?**

Double-click **`butler-talk.bat`**. That's console mode: no web page, no second
window, just talk to it in the terminal. If this works, everything works.

**The full thing**, two windows, in this order:

1. **`butler-agent.bat`** — wait for `registered worker`
2. **`butler-web.bat`**

Then open **http://localhost:3000**.

Order matters. The web page assumes the agent is already waiting; start it
second and the page connects to nothing.

## Your phone

The Flutter app is in `agent-starter-flutter/`. It needs the Flutter SDK, and
it's a bigger job than the web page — worth doing once the rest is working.

---

## If it dies with a wall of red text

Read the **first** error, not the last. The bottom of a Python traceback is
usually just the crash; the top says why.

**`invalid x-goog-api-key header`** means the Google key has something in it
that doesn't belong — a space, a newline, a command, or the key pasted twice.
An HTTP header can't contain a space, so it fails deep inside the websocket
handshake and the message never names the file it came from. Fix it with
`butler-keys.bat` and paste **only** the key.

This is also why the microphone looks innocent but suspicious: the session dies
before audio is ever used, so the level meter at the bottom is real and the
silence is not its fault.

## Wrong microphone or speakers

Console mode takes whatever Windows has set as default, which on a machine with
a headset, a webcam and a VR headset is rarely the one you want.

Run **`butler-devices.bat`**, find the microphone you actually talk into and the
speakers you actually hear, then:

```
butler-talk.bat 1 4
```

— microphone 1, speakers 4. Either number can be left out.

The web page (`butler-web.bat`) doesn't have this problem: the browser asks you
which microphone to use and handles echo cancellation itself.

## If it connects but never speaks

That's the failure worth knowing, because every part of it looks healthy — the
page connects, the microphone lights up, and nothing is ever said.

**Almost always `AGENT_NAME`.** `agent.py` registers as `my-agent`, which means
*explicit dispatch*: LiveKit only sends it to a room that asks for it by name.
If `jarvis_new/frontend/.env.local` doesn't say `AGENT_NAME=my-agent`, nobody
asks, and the agent sits there idle. `butler-setup.bat` writes both sides so
they agree — but if you ever edit those files by hand, keep them matching.

To check: in the agent window you should see `registered worker`, and then a job
arriving the moment you press connect on the page. No job arriving means
dispatch, not audio.

**Other things worth checking**

- `LIVEKIT_URL` must be the `wss://` address from your project page. The setup
  script converts `https://` for you.
- The key and secret must be from the *same* project as the URL.
- Google key comes from [aistudio.google.com](https://aistudio.google.com).

## What it costs

LiveKit Cloud and Google AI Studio both have free tiers that cover personal use.
The agent runs on your machine; you're paying for neither the CPU nor the
browser.

---

## Where things are

```
jarvis_new/
  src/agent.py      the agent - model, voice, turn handling
  src/tools.py      the 11 browser tools
  src/browser.py    Playwright wrapper
  src/prompts.py    the butler personality
  src/workers.py    background jobs, off the voice thread
  src/content/      the content engine - see CONTENT_ENGINE.md
  frontend/         the web page (Next.js)
agent-starter-flutter/   the phone app
```

To change how it behaves, `src/prompts.py` is the file. To change the voice,
`src/agent.py` — it's `voice="Enceladus"`, and any
[Gemini voice name](https://ai.google.dev/gemini-api/docs/speech-generation)
works.

## Credit and licence

The agent, frontend and Flutter app come from
[ruxakK/jarvis-voice-butler](https://github.com/ruxakK/jarvis-voice-butler),
built on LiveKit's starter templates. The frontend and Flutter app carry
LiveKit's MIT licence, kept in place at `jarvis_new/frontend/LICENSE` and
`agent-starter-flutter/LICENSE`.

## This is separate from the JARVIS in `jarvis/`

Both are still here. `jarvis_new/` is his; `jarvis/` is the one with your
permission system, missions, custom commands and people registry. They share
your keys and nothing else. Get this one working first — then we can move the
parts you want across.
