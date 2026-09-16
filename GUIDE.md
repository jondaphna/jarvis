# JARVIS — the whole thing

Everything you can do, how to start it, and what to do when it misbehaves.

---

## 1. Starting it

Double-click these. That's the whole interface.

| Button | What it does |
|---|---|
| **`butler-setup.bat`** | Run once. Installs everything and checks your keys. |
| **`butler-talk.bat`** | Talk in a terminal. No browser. The fastest way to check it works. |
| **`butler-agent.bat`** | The brain. Start this **first**. Wait for `registered worker`. |
| **`butler-web.bat`** | The web app, at **http://localhost:3000**. Start this **second**. |
| **`butler-orb.bat`** | **The only one you need.** The floating circle — starts everything. |

**The normal way to use it:** run `butler-orb.bat`. That's it.

Starting the orb starts everything — the agent, the web app, and a Chrome window
already connected and listening. By the time the circle appears you can just say
**"hey Jarvis"** and it answers. No clicking, no opening the site.

The orb goes amber while it's starting and cyan when it's ready. The first time
on a new machine, Chrome asks for the microphone — allow it once and every later
launch connects on its own.

**The order matters.** The web page expects the brain to already be waiting. Start
it the other way round and the page connects to nothing.

### The other buttons

| Button | What it does |
|---|---|
| **`butler-settings.bat`** | **Settings, guaranteed.** Opens them directly — nothing can cover it. |
| **`butler-doctor.bat`** | **Something's wrong?** Checks everything and says what. Start here. |
| **`butler-test.bat`** | The thorough one. Runs every test, then the doctor. About a minute. |
| **`butler-check.bat`** | Are my keys still valid? Asks Google and LiveKit directly. |
| **`butler-keys.bat`** | Re-enter your keys. |
| **`butler-devices.bat`** | List microphones and speakers with their numbers. |
| **`butler-api.bat`** | The control service by hand (the agent starts it anyway). |
| **`butler-restore.bat`** | Go back to a version that worked. |

---

## 1b. Nothing to sign into

You don't have to log Jarvis into anything. It opens **your** Chrome — the one
you're already signed into — so "open my Google" is your Google, and "open my
Netflix" is your Netflix, with no password anywhere.

There is a second browser, hidden, that Jarvis uses to read pages so it can
answer questions. You never see it and it's signed into nothing. That separation
is deliberate: the things you look at happen in your browser, and the reading
Jarvis does for you happens in its own.

Jarvis will never ask you for a password. If something ever does, it isn't
Jarvis.

## 2. Talking to it

Just talk. Interrupt whenever you like — it stops mid-sentence and listens.

- **"Hey Jarvis"** on its own gets an instant greeting. Both the phrase and the
  reply are yours to change, under **Voice** in the settings panel.
- **"Hey Jarvis, open YouTube"** skips the greeting and just does it.
- Say **goodbye** and it ends the call.

It can see your camera if you turn it on, and it drives a real Chrome window —
you'll watch it browse.

---

## 3. The settings panel

**Five ways in, because a floating button can always end up underneath
something:**

1. **`butler-settings.bat`** — the one that cannot fail
2. **http://localhost:3000/settings** — its own page
3. The **gear button**, top right
4. **Ctrl+Shift+S**
5. **Right-click the orb** → Settings and instructions

It opens on **Rules**, which is where you tell it what to always do.

Seven tabs.

### Rules — the one you asked for
Two boxes, in your words, read at the start of every conversation:

- **Standing instructions** — anything it should always know or always do.
  *"My business is video production." "Keep answers short." "You can open apps
  without asking." "Always check my calendar before suggesting a time."*
- **Never do these** — absolute limits. It refuses rather than looking for a
  way around them.

Plus which Chrome profile is yours.

### Permissions
A switch for each thing Jarvis can do — opening sites, clicking and typing,
opening apps, volume, memory, power. Switching one off **removes those tools**
rather than asking it to behave, so there is nothing to talk it out of. Power
actions start off.

### Rules (detail)
**The important one.** Written in your words, read at the start of every
conversation.

- **Standing instructions** — anything it should always know or do. *"My business
  is video production." "Keep answers short." "You can open apps without asking."*
- **Never do these** — absolute limits. It refuses rather than looking for a way
  around them. *"Never post publicly without asking." "Never spend money."*

These apply to conversations. Scheduled tasks carry their own separate
permissions, set on the task itself.

### Voice
The wake phrase and exactly what it says back. Changes apply next time the agent
starts — a call in progress keeps the instructions it began with.

### Tasks
Work that runs while you're asleep or out.

Each task has a name, a plain-English description of what to achieve, a schedule,
ordered steps, and — the important part — **its own permissions**.

Everything risky is **off unless you tick it**:

- May spend money
- May post publicly
- May send messages and email
- May upload my files
- May work outside my workspace folder

A task cannot decide on its own that spending money would be helpful. If the box
isn't ticked, the action is refused and logged, not negotiated.

Schedules are presets (*every night at 2am*, *weekdays at 7am*) or cron if you
want the control. Leave it empty and the task only runs when you ask.

You can also pick **which AI** does the thinking for that task — any provider you
have a key for shows up in the dropdown.

### AI
Every model this build can use, and whether a key is stored. Paste a key to switch
one on. Keys are encrypted in your vault, never in a file you might share.

At the top is **Money**, and it starts off. With it off, nothing Jarvis does can
cost you anything — the voice, the browser, memory, learning and the thinking all
run on the free Google key you already have. The paid models are in the dropdown
but locked; picking one while Money is off changes nothing, and a free brain
answers instead. Turn Money on and they unlock, and the panel shows what has been
spent in the last thirty days.

The key box **rejects anything with a space in it**. That's not fussiness — a
command pasted into a key box is what broke it last week, and it fails so far from
the cause that it looks like a microphone problem.

### Learned
Everything you have taught it, in your words and its steps, with how many times
each one has been used. Teaching happens out loud — "when I say put music on,
open Spotify and press play" — and lands here. So does a correction: the old way
is replaced, not kept beside the new one.

Lessons marked **FROM WATCHING** were not taught; it worked out what to do, the
request succeeded, and it wrote down what worked. Those never overwrite something
you said out loud. Anything here can be deleted with one button.

You can also type a lesson in directly, which is the easier way to write a long
one.

### Memory
What it knows about you. It adds things here itself as you talk; you can correct or
delete any of it. Also lists the conversations it's kept.

### Commands
Say these exact words, get exactly those words back. No thinking, no variation.

---

## 4. Everything it can do

### Thinking
Jarvis has two halves, on purpose.

The **voice** is Gemini Live — it hears you and answers in under a second,
which is what makes conversation feel natural. It is fast and shallow by design.

The **thinking** is a second brain. Anything where being right matters more than
being quick — writing a script or an email, analysing, diagnosing what went
wrong — gets handed over, and the voice speaks the result. It says "let me think
about that" first, because it takes a few seconds.

**It is free.** By default Jarvis picks the best free brain it has: a model on
your own machine if you're running Ollama, otherwise Google's free tier on the
same key the voice already uses. Nothing to buy, nothing to set up.

Claude is better at hard problems and costs money per question, so it is off
until you turn **Money** on in the AI tab. That switch wins over the dropdown: a
paid model chosen while Money is off is ignored, not honoured, so a stale setting
can never quietly start spending. You can switch thinking off entirely in
**Permissions**.

It will not think its way through a job you asked it to do. "Open my Spotify" is
a tool call, not a question, and treating it as a question is exactly what made
it worse for a while.

### Learning — teach it once
This is the part that makes it better at *your* work rather than work in general.

Tell it how you want something done and it writes the recipe down:

> "When I say put music on, open Spotify and press play."
> "My email means Gmail, not Outlook."
> "No — my Spotify, not the web player."

Every later call starts with those lessons already in front of it, so the next
time you say those words it follows the recipe instead of working it out again.
That is both faster and right more often. Correcting it replaces the old way
rather than keeping both.

It also learns by watching. When a request works first time, it writes down what
worked by itself — you do not have to say anything. Those are held more loosely
and never overwrite a lesson you gave out loud, and a failed attempt is never
learned.

Everything it has learned is in the **Learned** tab, in plain words, editable and
deletable. Nothing is a black box, nothing is training a model, and nothing is
uploaded anywhere: it is rows in the same local database as the rest of your
memory.

### Conversation
- Real-time voice, both directions, with proper echo cancellation
- Interrupt it mid-sentence; it stops and listens
- Knows the difference between an interruption and "mhm"
- Sees your camera when you share it
- Ends the call when you say goodbye

### Memory — across restarts
- Remembers facts about you forever, in the same database the original JARVIS uses
- Adds them itself as you talk, without being asked
- Searches everything ever said in past conversations
- You can edit or delete any of it

### Opening things
- **Websites, signed in as you** — "open YouTube", "open my Netflix", "open
  Gmail". Your Chrome, your accounts, no setup.
- **Straight to the thing** — "play Daft Punk on Spotify", "find Inception on
  Netflix", "search YouTube for X", "google the weather". It opens that site's
  own search, already signed in as you, landing on the result rather than the
  front page.
- **Apps** — "open Spotify", "open Word", "open task manager". Found through your
  Start Menu, so anything you have installed works by the name you'd actually say.
- If something isn't installed but exists as a website, it opens that instead.

### Your computer
- **Open folders** — "open my downloads"
- **Volume** — up, down, mute
- **Music** — play, pause, next, previous. Works with whatever is playing,
  because it uses the keyboard's media keys rather than one app's API
- **How the machine is doing** — processor, memory, disk, battery
- **Lock the screen**
- **Shut down, restart, sleep** — always asks first, and one yes authorises
  exactly one action

There is deliberately no "run any command" tool. A voice assistant that can be
talked into running arbitrary commands is a security hole with a personality.

### The browser — it drives a real Chrome
- Open any site; search the web
- Read a page and summarise it out loud
- Inspect a page to find its buttons and fields
- Click, type, scroll, press keys, go back
- Take screenshots
- **Asks before anything consequential** — sending, submitting, buying, deleting

### Work while you're away
- Scheduled tasks with their own permissions
- Steps that chain: search → read → think → notify
- Plugins: web search, browse, think/write, notify, images, speech, social posts, video
- Full audit trail of what ran and what it did

### Your rules
- Wake phrase and its instant reply
- Custom commands: exact words in, exact words out
- Per-task permissions, off by default

### On your desktop
- A floating orb, always on top, drag it anywhere
- Never in the taskbar, never steals focus
- Its ring tells you whether Jarvis is running
- Click to open; right-click to start everything

### Also built, reachable from the command line
These live in the original `jarvis/` package. They work, and they're not yet wired
into the web app:

- **Speaker recognition** — only acts on *your* voice (`jarvis voiceprint`)
- **People and authority** — other voices can be heard but not obeyed (`jarvis people`)
- **Local AI fallback** — Ollama, for when the internet or a key is gone
- **Approval queue** — anything blocked waits for you (`jarvis approvals`)
- **Local computer control** — open apps, manage windows, volume
- **Desktop app** — `python -m jarvis ui`

---

## 5. Where everything lives

```
jarvis_new/src/agent.py        the agent: model, voice, turn handling
jarvis_new/src/prompts.py      its personality — edit this to change how it talks
jarvis_new/src/tools.py        the browser tools
jarvis_new/src/brain_memory.py what it remembers
jarvis_new/src/control_api.py  the service behind the settings panel
jarvis_new/frontend/           the web app
desktop_orb.py                 the floating circle
jarvis/                        the original: permissions, missions, people, plugins
agent-starter-flutter/         the phone app (needs the Flutter SDK)
```

Your keys, memory, tasks and settings live **outside the repo**, in
`%APPDATA%\JARVIS`. Updating or restoring the code never touches them.

---

## 5b. It used to go quiet after a while — fixed

A Gemini Live session has a time limit. When it ran out the server said so, the
session closed, and Jarvis lost every tool mid-conversation with nothing on
screen to explain it. It now asks for session resumption, so a dropped session
comes back and carries on, and compresses its context so a long conversation
cannot fill up.

Closing the browser window used to do the same thing by a different route — the
dead connection was kept and every later tool failed. It now notices, drops it,
and opens a new window when you next ask for something.

## 6. When something goes wrong

**Run `butler-doctor.bat` first.** It checks your keys, the AGENT_NAME match,
what memory actually holds, whether the settings service is running, and whether
Chrome is installed — then prints a numbered list of what to fix.

**Read the first error, not the last.** The bottom of a Python traceback is usually
just the crash; the top says why.

| What you see | What it means |
|---|---|
| Page connects, nothing is ever said | `AGENT_NAME` mismatch. Run `butler-keys.bat`. |
| `invalid x-goog-api-key header` | Something extra got pasted into the key. `butler-keys.bat`. |
| `failed to connect to livekit` repeating | Wrong LiveKit credentials. `butler-check.bat`. |
| It can't hear you | Wrong microphone. `butler-devices.bat`, then `butler-talk.bat 1 4`. |
| It opens in Edge and doesn't work | Install Chrome. The orb uses Chrome specifically. |
| It forgot what you talked about | `butler-doctor.bat` — it prints what is actually stored. |
| A site says you're signed out | You're seeing the hidden browser. Ask it to *open* the site instead. |
| It opens in Edge | Install Chrome — Jarvis looks for Chrome specifically. |
| Settings panel says "control service isn't running" | Run `butler-api.bat`. |

**Check the agent window.** You should see `registered worker`, then a job arriving
the moment you press connect. No job arriving means dispatch, not audio.

---

## 7. Going back

**`butler-restore.bat`** returns you to a version that worked.

It never throws anything away — it saves whatever you have onto a backup branch
first, so undoing the undo is always possible. Your keys, memory and tasks are
outside the repo and are never touched.

The restore points are listed in `RESTORE_POINTS.txt`, by commit rather than by
git tag — the environment this was built in refuses to push tags, and a restore
point that isn't there when you need it is worse than none.

To get back to the newest version:

```
git checkout claude/personal-ai-assistant-hihz78
```

---

## 8. What to add next

Against your list — what's already there, and what I'd build in what order.

### Already done
| You asked for | Where it is |
|---|---|
| Local OS control | Done — apps, folders, volume, music, power |
| Real-time system monitoring | Done — "how's the machine doing?" |
| Spotify / audio control | Done via media keys, works with any player |
| Persistent long-term memory | Done, shared database |
| Autonomous browser control | Done, 11 tools |
| Speaker voice biometrics | Built — CLI only, not yet in the web app |
| Local LLM fallback (Ollama) | Wired in — it is now the *first* choice for thinking when it's running |
| Everything free by default | Done — paid AI is one switch in the AI tab, off until you flip it |
| Teach it once and it remembers | Done — the Learned tab, taught out loud or typed in |
| Custom desktop HUD overlay | The orb |
| Mobile app | Flutter app is there, needs the SDK to build |
| Local OS control | Built in `jarvis/core/computer.py` |
| Proactive briefings | The Tasks tab does this now |

### I'd do these next, in this order
1. **Wire speaker recognition into the voice agent** — it's built and it's the one
   that makes the permission system mean something. Right now anyone near your mic
   is you.
2. **Google Calendar and Gmail** — the biggest daily win, and the tasks system is
   already there to hang it on.
3. **Local file search and document summarising** — point it at a folder, ask it
   things about your own documents.
4. **System monitoring and Spotify** — small, satisfying, low risk.
5. **Proactive speech** — it starts the conversation, instead of waiting.

### Worth doing later
Financial tracking, IDE and Git integration, gesture control, multi-agent
orchestration, automated research reports.

### About training your own model
You asked whether it should learn by being trained on your conversations. I built
the learning differently, on purpose, and it's worth knowing why.

The realtime voice model cannot be fine-tuned at all — that option does not exist.
And even where fine-tuning is possible it needs thousands of examples before it
shifts behaviour, takes hours and money per round, and gives you a black box: when
it gets something wrong you can't open it up and see what it thinks you meant, you
can only feed it more examples and hope.

Remembering does the same job better for what you actually asked for. Teaching is
one sentence and takes effect on the next thing you say. It costs nothing. You can
read every lesson back, edit it, and delete it. And "most of the times I give him
the same tasks" is exactly the case remembering is best at — a recipe in front of
it beats a tendency baked into weights.

### I'd leave this one alone
**Self-debugging and auto-patching its own code.** An agent with write access to
its own source and the ability to run it is the one thing on the list that can
break itself in a way you can't easily undo — and it would be editing the very
permission system that's supposed to contain it. If you want it, I'd want a strict
sandbox and a human approving every patch first.

---

See **[WORKPLAN.md](WORKPLAN.md)** for what's next, how long each piece takes,
and what I'd need from you for each one.
