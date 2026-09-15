# JARVIS

A personal AI that runs on your machine, talks to you, operates your computer, and
keeps working while you sleep.

Two modes, one system:

- **With you** — hold a conversation. It opens apps, reads and writes files,
  searches the web, and does what you ask.
- **For you** — missions run on a schedule overnight. Research, content, file
  wrangling, anything you can describe as steps.

The autonomy is the point. The interface is just how you reach it.

---

## The 10-minute setup

### 1. Install Python

[python.org/downloads](https://www.python.org/downloads/) — **tick "Add Python to
PATH"** on the first screen of the Windows installer. That checkbox is the single
most common reason step 2 fails.

### 2. Install JARVIS

```bash
git clone <your-repo-url> jarvis
cd jarvis
pip install -r requirements.txt
```

In a hurry, or on a low-spec machine? `pip install -r requirements-core.txt` gets
you text chat and missions without the voice stack or the GUI.

### 3. Add one API key

```bash
python -m jarvis setup
```

It asks for your name, a workspace folder, and your API keys. **Only the first key
is required** — everything else is optional and can be added later.

| Key | Where | Cost | What breaks without it |
|---|---|---|---|
| **`ANTHROPIC_API_KEY`** | [console.anthropic.com](https://console.anthropic.com) | pay-as-you-go, $5 lasts a long time | **Everything. This is the brain.** |
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) | $200 free credit | Nothing — local Whisper is the default |
| `CARTESIA_API_KEY` | [play.cartesia.ai](https://play.cartesia.ai) | free tier | Nothing — free Edge TTS is the default |
| `ELEVENLABS_API_KEY` | [elevenlabs.io](https://elevenlabs.io) | 10k chars/month free | Nothing — premium voice option |
| `EXA_API_KEY` | [exa.ai](https://exa.ai) | $10 credit | Nothing — keyless search fallback works |
| `TAVILY_API_KEY` | [tavily.com](https://tavily.com) | 1,000/month free | Nothing — same |
| `REPLICATE_API_TOKEN` | [replicate.com](https://replicate.com) | ~$0.003/image | Image generation |
| `KLING_ACCESS_KEY` + `KLING_SECRET_KEY` | [klingai.com](https://klingai.com) | 66 credits/day free | Video generation |
| `HEYGEN_API_KEY` | [heygen.com](https://heygen.com) | free trial | Talking-avatar video |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | respective consoles | free credit | Nothing — backup brains |

Keys are encrypted at rest in your user config folder. Add or change them any time
with `jarvis keys set NAME`, or in the Settings tab.

### 4. Check it works

```bash
python -m jarvis doctor
```

This prints exactly what's working and what isn't, and tells you the command to fix
each gap. Nothing is silently broken.

### 5. Point it at the right speakers and microphone

Windows machines usually have several of each, and the default is often the
wrong one — which looks exactly like "JARVIS is broken".

```bash
jarvis devices
```

That lists everything with its real name. Pick yours:

```bash
jarvis devices --set-output "NVIDIA"     # what you hear it through
jarvis devices --set-input "Lenovo"      # the microphone it hears you through
```

Then prove both directions work:

```bash
jarvis say "systems online"    # you should hear this
jarvis listen                  # say something; it prints what it heard
```

### 6. Talk to it

```bash
python -m jarvis chat      # terminal
python -m jarvis voice     # out loud, say "jarvis" to wake it
python -m jarvis           # the desktop app
```

First things worth trying:

```
what can you do?
tidy up my workspace folder
search for what's trending in AI automation this week and summarise it
make a mission that emails me a news summary every weekday at 7am
```

---

## What it can actually do

Ask in plain language; JARVIS picks the tools.

| | |
|---|---|
| **Computer** | open any app or site, read/write/move/delete files, search inside files, run commands, take screenshots, drive the keyboard and mouse |
| **Web** | search (Exa, Tavily, or a keyless fallback), read pages, extract and summarise |
| **Create** | images (FLUX), video (Kling), talking-avatar video (HeyGen), voiceovers, ffmpeg editing |
| **Publish** | TikTok, Instagram, YouTube — via their APIs, or staged for a two-click manual post |
| **Remember** | facts about you and your business, every conversation, every action taken |
| **Automate** | missions on a cron schedule, running unattended |

---

## Permissions — the part that matters

JARVIS can operate your computer. So the rules about when it may are strict, and
they aren't decided by the AI.

**1. Free rein in your workspace.** One folder (default `~/JARVIS Workspace`) plus
the apps and sites you approve. Reading, writing, organising, searching, thinking —
silent and immediate.

**2. One-tap approval outside it.** Files elsewhere, an app you never approved, a
shell command. The action stops and lands in the approval queue:

```bash
jarvis approvals          # see what's waiting
jarvis approve 3          # allow it
```

**3. Blocked by default, and only *you* can unblock it.** Four things are never
allowed by policy alone:

- spending money
- posting or uploading publicly
- sending email or messages
- changing system configuration

The only thing that unblocks one is you saying so **in the request that starts the
work**:

> "Research video tools tonight. **You have permission to buy the Kling
> subscription this time, up to $20.**"

> "Make ten videos and **you're allowed to post them to TikTok, today only.**"

Those authorisations are parsed into specific, bounded grants — this capability,
this service, this many times, this much money, until this time. `up to $20` means
JARVIS cannot spend $21, and a grant for Kling does not authorise HeyGen. They
expire, and a mission's grants die when the mission ends.

**4. A hard floor nothing overrides.** Credential stores (`.ssh`, `.aws`, browser
password databases), system directories, JARVIS's own key vault, and a list of
catastrophic shell commands are refused regardless of any grant or approval.

**Widening permissions takes five seconds:**

```
"JARVIS, you can use Photoshop"        → adds it to the app allowlist, permanently
"you may use CapCut and Premiere Pro"  → both
```

That's parsed from *your words*, deterministically. JARVIS cannot grant itself
anything: the `allow_app` tool is itself high-risk, a mission JARVIS writes has its
authorisations stripped, and text it merely *reads* — a web page, an email, a tool
result — can never be an authorisation. That's the security boundary.

Every decision, allowed or refused, is written to an audit log:

```bash
jarvis doctor             # current permissions and pending approvals
```

---

## Paying as little as possible

Claude costs money per message — roughly **$0.006** once the prompt cache is
warm, more on the first message of a session. JARVIS gives you three ways to cut
that down.

**1. A free local brain.** Install [Ollama](https://ollama.com), then:

```bash
ollama pull llama3.1
```

That's it — JARVIS notices it's running and routes everyday chat through it for
**$0.00**. Local models can use tools too, so "open Chrome", "tidy my files" and
"what's in this folder" all work offline and free.

**2. A code word for the hard stuff.** Local models are weaker at long,
multi-step work. When something matters, say the words:

> "**heavy guns**, research my three biggest competitors and write me a plan"

That one request goes to Claude; everything after it goes back to free. Other
phrases that work: *big guns, full power, max power, use claude, serious mode*.
Change them in settings:

```bash
jarvis config brain.escalate_phrases "heavy guns,totach,full power"
```

**3. A cheaper Claude for chat.** If you'd rather not run a local model:

```bash
jarvis config models.general claude-haiku-4-5
```

~5x cheaper for conversation, while overnight missions stay on Opus where the
intelligence earns its cost.

Missions always use Claude regardless — an unattended job that fails at 3am
costs more than it saves. There's also a **$5/day hard cap** by default:

```bash
jarvis config autonomy.daily_spend_cap_usd 5
```

| Setting | Everyday chat | Overnight missions |
|---|---|---|
| Default | Claude Opus 5 | Claude Opus 5 |
| With Ollama installed | **free, local** | Claude Opus 5 |
| `models.general` = haiku | Claude Haiku (~5x cheaper) | Claude Opus 5 |

---

## Talking to it

**You don't press anything.** JARVIS starts listening the moment it opens.

Just say the name:

> **"Jarvis"** → *"Yeah? What's up?"* → then say what you want

or say it in one go:

> **"Jarvis, open Chrome and search for flights to Rome"**

After it answers you have **12 seconds to keep talking without saying the name
again**, so a back-and-forth feels like a conversation rather than a series of
commands. Say *"stop"*, *"never mind"* or *"goodbye"* and it goes quiet.

The microphone stays open the whole time — including while JARVIS is thinking or
talking — so the wake word can never land in a gap. It ignores its own voice
coming back through the speakers, and a door slamming isn't a word.

Not hearing you? It's almost always the wrong microphone:

```bash
jarvis devices --set-input "Lenovo"
jarvis listen
```

Prefer to type? The text box always works and costs the same. Prefer a button?
"Pause listening" stops it; `jarvis config voice.always_listening false` makes
it opt-in.

---

## Your own commands

Tell JARVIS a rule once and it keeps it forever. Just say it:

> "From now on, **when I say hey jarvis, say: Yes sir, how can I help you**"

> "**Always** call me sir"

> "When I say **movie night**, dim the lights and open Netflix"

Three kinds, and the first one matters for speed:

| Kind | What it does | Cost |
|---|---|---|
| **reply** | Says your exact words back | **instant, free — no model call at all** |
| **always** | A standing instruction on every conversation | normal |
| **do** | A shorthand that expands into a fuller request | normal |

A `reply` rule is answered locally, so "hey jarvis" comes back with zero delay
and zero cost — no API call happens.

From the terminal:

```bash
jarvis commands                                     # list them
jarvis commands add "hey jarvis" "Yes sir"          # exact reply
jarvis commands always "keep your answers short"    # standing instruction
jarvis commands do "movie night" "open Netflix"     # shorthand
jarvis commands remove <id>
```

---

## More than one person

JARVIS can tell people apart by voice (and by face, with a camera) and give each
of them a different level of authority — which is how "he can talk to it, but he
can't touch my computer" actually works.

```bash
jarvis people add Dani --authority trusted
jarvis people voice Dani          # learn their voice
jarvis people level Dani --authority guest
```

| Level | Can do |
|---|---|
| **owner** | Everything — still subject to the normal permission system |
| **trusted** | Ask questions, search, read. **Cannot change anything** |
| **guest** | Conversation only. No tools |
| **blocked** | Ignored entirely |

Rules can be scoped too, so `jarvis commands add "hey jarvis" "Yes boss" --who Dani`
gives him a different greeting from yours.

**Identity narrows, it never grants.** A recording of your voice can fool it, so
it can only ever *reduce* what someone is allowed to do — never raise it. Anything
that spends money, publishes, or sends messages still needs your explicit
authorisation in the request, exactly as before.

---

## Interrupting it

Talk over JARVIS and it stops mid-sentence and listens to what you said instead.
You don't have to wait for it to finish.

There's a real problem underneath that, worth knowing about: your microphone
hears your speakers, so JARVIS can hear its own voice and answer itself. Without
hardware echo cancellation the only way to tell the difference is the words - so
JARVIS compares what it just heard against what it just said. Close match, it's
the speakers and it's ignored. Different, you're interrupting, and it stops.

**Headphones make this perfect**, because the microphone never hears the
speakers at all. Without them it works well, but a very noisy room can still
confuse it.

---

## Only listening to you

JARVIS can learn your voice and ignore everybody else — useful if the TV is on,
or people are talking nearby.

```bash
pip install resemblyzer      # ~2GB (pulls in PyTorch) - only if you want this
jarvis voiceprint enrol      # say three sentences
jarvis voiceprint test       # check it recognises you
```

That switches it on. Other commands: `jarvis voiceprint on|off|forget|status`.

**What it does with your voice:** the recording is turned into a list of numbers
on your machine, and only those numbers are saved. The audio is never stored and
nothing is ever uploaded — not to Claude, not anywhere. Delete it whenever you
like with `jarvis voiceprint forget`.

**What it is not:** a security control. A decent recording of your voice would
pass it. It's a filter for background chatter, nothing more — so nothing that
actually matters (spending, posting, sending mail) depends on it. Those still go
through the permission system.

If it keeps missing you, add more samples (`jarvis voiceprint enrol`) or loosen
it: `jarvis config voice.speaker_threshold 0.62`.

---

## The floating orb

Close the main window and JARVIS doesn't go away — a small circle stays on top of
everything else.

- **Say the wake word** — it swells and shows what it heard
- **Click it** — talk without the wake word
- **Double-click** — bring the full window back
- **Drag it** — anywhere you like; it remembers where
- **Right-click** — talk, open, hide, or quit

Turn it off with `jarvis config ui.floating_orb false`.

---

## Missions — the overnight engine

A mission is a JSON file of steps. Each step names a plugin, gets parameters, and
stores its result for later steps to use.

```bash
jarvis missions              # what's saved and when it runs
jarvis run morning_brief     # run one now
jarvis daemon                # just the scheduler, for leaving running
```

Three ship with JARVIS (all disabled until you enable them):

- **`workspace_tidy`** — sorts your workspace into folders nightly. Needs no keys
  and no permissions. The best first test.
- **`morning_brief`** — researches your niche at 7am and puts a five-point brief on
  your desktop.
- **`tiktok_factory`** — finds trends, writes scripts, generates voiceovers and
  clips, cuts them together, stages them for posting.

### Writing your own

Easiest way — just ask:

```
"make a mission that checks my competitors every Monday and emails me what changed"
```

JARVIS writes the JSON, validates it against the plugins you actually have, and
saves it. Or copy `jarvis/builtin_missions/TEMPLATE_MISSION.json`, which documents every field.

```json
{
  "id": "daily_research",
  "name": "Daily Research",
  "schedule": "0 7 * * *",
  "steps": [
    { "name": "Search", "plugin": "web_search",
      "params": { "query": "AI automation news", "num_results": 10 },
      "output_key": "research" },

    { "name": "Summarise", "plugin": "llm",
      "params": { "prompt": "Five bullet points from this: {research}" },
      "output_key": "summary" },

    { "name": "Save", "plugin": "write_file",
      "params": { "path": "briefs/today.md", "content": "{summary}" } }
  ]
}
```

- `{output_key}` reuses an earlier result. `{scripts[*].title}` pulls one field from
  every item in a list. `{results[0].url}` takes the first.
- `"schedule"` is standard cron. `0 2 * * *` is 2am daily.
- `"on_error": "continue"` keeps going when a step fails; `"retry"` with
  `"retries": 3` tries again.
- `"condition": "some_key"` skips a step when an earlier one produced nothing.
- `"authorizations": ["You have permission to post to TikTok, today only."]` is how
  a mission gets to do a high-risk thing unattended. You write it, in the file. It
  applies only while that mission runs, and it's shown in amber in the UI so you
  can never forget it's there.

---

## Adding a new capability

Every capability is a plugin: one file, three things to fill in. Adding one touches
no existing code.

1. Copy `jarvis/plugins/TEMPLATE.py` to `jarvis/plugins/your_tool.py` (or into your
   personal plugin folder — `jarvis where` prints it).
2. Fill in `NAME`, `DESCRIPTION`, `SCHEMA` and `run()`.
3. Restart. It's now a voice command *and* a mission step.

```python
class EtsyPlugin(Plugin):
    NAME = "etsy_post"
    DESCRIPTION = "Lists a product on Etsy."
    REQUIRES_KEYS = ["ETSY_API_KEY"]
    CAPABILITY = CAP_WEB_PUBLISH     # so it needs your explicit go-ahead
    SERVICE = "etsy"                 # so "you may post to Etsy" scopes to it

    async def run(self, params, context):
        ...
```

Set `CAPABILITY` honestly — it's what decides whether the plugin can act on its own
at 3am. `jarvis plugins` lists everything installed and what each one still needs.

---

## Command reference

```
jarvis setup            first-run wizard
jarvis                  desktop app (or chat, if PyQt6 isn't installed)
jarvis chat             terminal conversation        --speak to hear replies
jarvis voice            hands-free                   --always-on to skip the wake word
jarvis ask "..."        one command, then exit
jarvis missions         list saved missions
jarvis run <id>         run a mission now            --unattended to queue approvals
jarvis daemon           scheduler only
jarvis approvals        what's waiting               --approve ID / --deny ID
jarvis doctor           what's working and what isn't
jarvis plugins          installed plugins and tools
jarvis keys list|set|remove
jarvis devices          list microphones and speakers; --set-input / --set-output
jarvis say "hello"      speak a line out loud (tests audio output)
jarvis listen           record one sentence (tests the microphone)
jarvis config KEY VALUE read or change any setting
jarvis voiceprint       enrol/test/on/off/forget your voiceprint
jarvis commands         your own custom commands
jarvis people           who it recognises and what they may ask for
jarvis where            where JARVIS keeps its files
```

---

## Building JARVIS.exe

```bash
build.bat
```

Installs dependencies, runs the tests, and produces `dist\JARVIS\JARVIS.exe` — no
Python needed on the target machine. Zip the `dist\JARVIS` folder to move it.

It's a folder build rather than a single file on purpose: one-file builds unpack to
a temp directory on every launch, which makes startup slow and antivirus
whitelisting harder.

---

## Choices worth knowing about

**Claude as the brain.** Long chains of tool calls that don't fall apart is exactly
what overnight autonomy needs, and it's what Claude is best at. Haiku 4.5 handles
voice replies (fast and cheap), Opus 5 handles missions. Change either in Settings;
OpenAI, Gemini and local Ollama are wired up as fallbacks.

**Fish Audio for a custom voice.** If you want a specific JARVIS voice rather
than a stock one, add a Fish Audio key and paste the voice model id from its
page on fish.audio:

```bash
jarvis keys set FISH_API_KEY
jarvis config voice.fish_voice_id <the id from the fish.audio model page>
jarvis say "at your service"
```

**Free voice by default.** `faster-whisper` transcribes locally — free, private,
works offline. `edge-tts` speaks — free, no key, genuinely good. Paid engines
(Deepgram, Cartesia, ElevenLabs) are a key away if you want the last ~300ms of
latency, but you can run JARVIS tonight with no voice signups at all.

**Search without a key.** Exa and Tavily if you have them, with a keyless fallback
so search never simply stops.

**Posting stages by default.** TikTok and Instagram API posting needs an approved
developer app most people don't have on day one. So `mode: "prepare"` copies the
video and caption into one folder and opens the upload page — two clicks, works
immediately. Add a token later and it posts directly.

**A written mission editor, not a node graph.** A drag-and-drop canvas demos
better. Validation against the real plugin registry — every plugin name, every
`{placeholder}`, every cron expression checked before you can save — is what
actually stops a mission failing silently at 2am.

---

## When something's wrong

Start with `jarvis doctor`. It names the problem and the fix.

| Symptom | Fix |
|---|---|
| `'jarvis' is not recognized` | Use `python -m jarvis`, or reinstall Python with "Add to PATH" ticked |
| "No AI provider configured" | `jarvis keys set ANTHROPIC_API_KEY` |
| Voice input unavailable | `pip install faster-whisper sounddevice numpy` |
| No voice output | `pip install edge-tts` |
| JARVIS talks but you hear nothing | Wrong speakers. `jarvis devices --set-output "NVIDIA"` then `jarvis say "test"` |
| JARVIS never hears you | Wrong microphone. `jarvis devices --set-input "Lenovo"` then `jarvis listen` |
| First voice reply is slow | Normal — the local speech model downloads once (~150MB) |
| Video merging fails | Install ffmpeg and put it on PATH ([ffmpeg.org](https://ffmpeg.org/download.html)) |
| Missions don't fire | They ship disabled. Enable in the Missions tab, and leave `jarvis daemon` or the app running |
| JARVIS keeps asking permission | Move the files into your workspace, or add the app with "you can use X" |

Logs are in the folder `jarvis where` prints.

---

## Testing

```bash
pytest tests -q        # 264 tests, no API key or network needed
```

The permission tests are the ones that matter — they're the safety net for
everything JARVIS is allowed to do to your machine.
