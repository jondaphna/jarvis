# Jarvis, the voice butler

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

You need Node.js. If it stops and says so, get the LTS build from
[nodejs.org](https://nodejs.org) and run it again.

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
