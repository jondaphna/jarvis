"""Speech output. Free by default, upgradeable by pasting a key.

Engines, in the order `auto` tries them:

  cartesia    ~40-90ms to first audio   needs CARTESIA_API_KEY
  elevenlabs  ~75ms                     needs ELEVENLABS_API_KEY
  edge        ~200-400ms, free, no key  needs `edge-tts`   <- the default
  pyttsx3     instant, offline, robotic needs `pyttsx3`    <- last resort

Long replies are split into sentences and spoken as they arrive, so JARVIS
starts talking while Claude is still writing. That matters more for perceived
speed than the engine's own latency does.
"""

from __future__ import annotations

import asyncio
import re
import time
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

from . import events
from .audio import resolve_device
from .events import bus, log

#: Split on sentence ends, but not on decimals, abbreviations or ellipses.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
_MIN_CHUNK = 60          # don't bother speaking fragments shorter than this
#: Speakers lag the code; treat this long after the last chunk as still talking.
SPEECH_TAIL_SECONDS = 0.6


class SpeechError(RuntimeError):
    """Text-to-speech failed in a way worth reporting."""


class SpeechEngine:
    """Turns text into audio, and plays it."""

    def __init__(self, config) -> None:
        self.config = config
        self.settings = config.settings
        self._cancel = asyncio.Event()
        self._speaking = False
        #: Audio keeps coming out of the speakers slightly after the last chunk
        #: is handed over, so "are we speaking" has to include a short tail.
        self._speaking_until = 0.0
        #: What was said recently, for telling an echo apart from a real
        #: interruption when there's no hardware echo cancellation.
        self._recent: deque[tuple[float, str]] = deque(maxlen=8)
        self._current: Any = None

    # ------------------------------------------------------------------ #
    # Engine selection
    # ------------------------------------------------------------------ #

    def choose_engine(self, requested: str | None = None) -> str:
        wanted = (requested or self.settings.get("voice.tts_engine", "auto")).lower()
        if wanted not in ("auto", ""):
            return wanted

        if self.config.key("FISH_API_KEY"):
            return "fish"
        if self.config.key("CARTESIA_API_KEY"):
            return "cartesia"
        if self.config.key("ELEVENLABS_API_KEY"):
            return "elevenlabs"
        if _module_available("edge_tts"):
            return "edge"
        if _module_available("pyttsx3"):
            return "pyttsx3"
        return "none"

    def describe(self) -> str:
        engine = self.choose_engine()
        return {
            "fish": "Fish Audio (custom voice, paid)",
            "cartesia": "Cartesia Sonic (fastest, paid)",
            "elevenlabs": "ElevenLabs (premium, paid)",
            "edge": "Edge TTS (free, natural)",
            "pyttsx3": "System voice (offline, basic)",
            "none": "no voice output available - run: pip install edge-tts",
        }.get(engine, engine)

    # ------------------------------------------------------------------ #
    # Speaking
    # ------------------------------------------------------------------ #

    @property
    def speaking(self) -> bool:
        """True while audio is playing, plus a short tail afterwards."""
        return self._speaking or time.monotonic() < self._speaking_until

    def note_spoken(self, text: str) -> None:
        """Remember what was just said, so an echo of it can be recognised."""
        cleaned = _clean_for_speech(text)
        if cleaned:
            self._recent.append((time.monotonic(), cleaned.lower()))

    def recently_spoken(self, window: float = 20.0) -> str:
        cutoff = time.monotonic() - window
        return " ".join(text for at, text in self._recent if at >= cutoff)

    def stop(self) -> None:
        """Cut speech off immediately. This is what makes barge-in possible."""
        self._cancel.set()
        self._speaking = False
        self._speaking_until = 0.0
        self._recent.clear()
        try:
            import sounddevice
            sounddevice.stop()          # halts playback mid-sentence
        except Exception:
            pass

    async def say(self, text: str, voice: str | None = None,
                  engine: str | None = None) -> None:
        """Speak a complete piece of text, sentence by sentence."""
        text = _clean_for_speech(text)
        if not text:
            return

        self._cancel.clear()
        self._speaking = True
        bus.publish(events.SPEAKING, text)
        try:
            for chunk in _sentences(text):
                if self._cancel.is_set():
                    break
                await self.speak_chunk(chunk, voice, engine)
        finally:
            self._speaking = False

    async def stream(self, voice: str | None = None,
                     engine: str | None = None) -> "SpeechStream":
        """A sink you push text deltas into; it speaks each finished sentence."""
        return SpeechStream(self, voice=voice, engine_name=engine)

    async def speak_chunk(self, text: str, voice: str | None = None,
                          engine: str | None = None) -> None:
        """Synthesise and play one chunk of text."""
        name = self.choose_engine(engine)
        if name == "none":
            log.info("SPEAK (no engine): %s", text)
            return

        # Marked here, not just in say(): every streamed reply comes through
        # this path, and without it the echo guard never fires at all.
        self._speaking = True
        self.note_spoken(text)
        try:
            if name == "pyttsx3":
                await asyncio.to_thread(self._pyttsx3_say, text, voice)
                return
            audio = await self._synthesise(text, voice, name)
            if audio and not self._cancel.is_set():
                await self._play(audio)
        except Exception as exc:
            log.warning("tts engine %s failed: %s", name, exc)
            # Fall back once to the always-available offline voice.
            if name != "pyttsx3" and _module_available("pyttsx3"):
                try:
                    await asyncio.to_thread(self._pyttsx3_say, text, voice)
                except Exception:
                    log.warning("offline fallback voice failed too", exc_info=True)
        finally:
            self._speaking = False
            self._speaking_until = time.monotonic() + SPEECH_TAIL_SECONDS

    # ------------------------------------------------------------------ #
    # Synthesis backends
    # ------------------------------------------------------------------ #

    async def _synthesise(self, text: str, voice: str | None, engine: str) -> bytes:
        if engine == "fish":
            return await self._fish(text, voice)
        if engine == "cartesia":
            return await self._cartesia(text, voice)
        if engine == "elevenlabs":
            return await self._elevenlabs(text, voice)
        if engine == "edge":
            return await self._edge(text, voice)
        raise SpeechError(f"Unknown speech engine {engine!r}")

    async def _edge(self, text: str, voice: str | None) -> bytes:
        try:
            import edge_tts
        except ImportError as exc:
            raise SpeechError("edge-tts isn't installed. Run: pip install edge-tts") from exc

        communicate = edge_tts.Communicate(
            text,
            voice or self.settings.get("voice.edge_voice", "en-GB-RyanNeural"),
            rate=self.settings.get("voice.speaking_rate", "+0%"),
        )
        chunks = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.extend(chunk["data"])
        return bytes(chunks)

    async def _fish(self, text: str, voice: str | None) -> bytes:
        """Fish Audio - lets you use a cloned or community JARVIS voice.

        `reference_id` is the voice model id from the fish.audio model page
        (the last part of its URL). Without one you get Fish's default voice.
        """
        import requests

        key = self.config.key("FISH_API_KEY")
        voice_id = voice or self.settings.get("voice.fish_voice_id") or ""
        model = self.settings.get("voice.fish_model", "s1")

        payload: dict[str, Any] = {
            "text": text,
            "format": "mp3",
            "mp3_bitrate": 128,
            "latency": "balanced",
            "normalize": True,
        }
        if voice_id:
            payload["reference_id"] = voice_id

        def _post() -> bytes:
            response = requests.post(
                "https://api.fish.audio/v1/tts",
                headers={"Authorization": f"Bearer {key or ''}",
                         "Content-Type": "application/json",
                         "model": model},
                json=payload,
                timeout=90,
            )
            if response.status_code == 401:
                raise SpeechError(
                    "Fish Audio rejected the API key. Check it in Settings, or "
                    "run: jarvis keys set FISH_API_KEY")
            if response.status_code == 402:
                raise SpeechError("Your Fish Audio account is out of credit.")
            if response.status_code >= 400:
                raise SpeechError(f"Fish Audio returned {response.status_code}: "
                                  f"{response.text[:200]}")
            if not response.content:
                raise SpeechError("Fish Audio returned no audio.")
            return response.content

        return await asyncio.to_thread(_post)

    async def _cartesia(self, text: str, voice: str | None) -> bytes:
        import requests

        key = self.config.key("CARTESIA_API_KEY")
        voice_id = voice or self.settings.get("voice.cartesia_voice_id")

        def _post() -> bytes:
            response = requests.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={"X-API-Key": key or "", "Cartesia-Version": "2024-06-10",
                         "Content-Type": "application/json"},
                json={
                    "model_id": "sonic-2",
                    "transcript": text,
                    "voice": {"mode": "id", "id": voice_id},
                    "output_format": {"container": "mp3", "bit_rate": 128000,
                                      "sample_rate": 44100},
                },
                timeout=60,
            )
            if response.status_code >= 400:
                raise SpeechError(f"Cartesia returned {response.status_code}: "
                                  f"{response.text[:200]}")
            return response.content

        return await asyncio.to_thread(_post)

    async def _elevenlabs(self, text: str, voice: str | None) -> bytes:
        import requests

        key = self.config.key("ELEVENLABS_API_KEY")
        voice_id = voice or self.settings.get("voice.elevenlabs_voice_id")

        def _post() -> bytes:
            response = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": key or "", "Content-Type": "application/json"},
                json={"text": text, "model_id": "eleven_flash_v2_5",
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
                timeout=60,
            )
            if response.status_code >= 400:
                raise SpeechError(f"ElevenLabs returned {response.status_code}: "
                                  f"{response.text[:200]}")
            return response.content

        return await asyncio.to_thread(_post)

    def _pyttsx3_say(self, text: str, voice: str | None) -> None:
        import pyttsx3

        engine = pyttsx3.init()
        if voice:
            for candidate in engine.getProperty("voices"):
                if voice.lower() in candidate.name.lower():
                    engine.setProperty("voice", candidate.id)
                    break
        engine.setProperty("rate", 185)
        engine.say(text)
        engine.runAndWait()

    # ------------------------------------------------------------------ #
    # Playback
    # ------------------------------------------------------------------ #

    async def _play(self, audio: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            handle.write(audio)
            path = Path(handle.name)
        try:
            await asyncio.to_thread(
                _play_file, path, self.settings.get("voice.output_device"))
        finally:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Files (used by the tts_batch plugin)
    # ------------------------------------------------------------------ #

    async def synthesise_to_file(self, text: str, target: Path,
                                 voice: str | None = None,
                                 engine: str | None = None) -> Path:
        name = self.choose_engine(engine)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)

        if name == "none":
            raise SpeechError(
                "No speech engine available. Run: pip install edge-tts")

        if name == "pyttsx3":
            def _save() -> None:
                import pyttsx3
                driver = pyttsx3.init()
                driver.save_to_file(text, str(target))
                driver.runAndWait()
            await asyncio.to_thread(_save)
            return target

        audio = await self._synthesise(_clean_for_speech(text), voice, name)
        target.write_bytes(audio)
        return target


class SpeechStream:
    """Buffers streamed text and speaks each sentence as soon as it's complete."""

    def __init__(self, speech: SpeechEngine, voice: str | None = None,
                 engine_name: str | None = None) -> None:
        # The first argument is named `speech`, not `engine`: an `engine=`
        # keyword used to collide with it and crash every spoken reply.
        self.speech = speech
        self.voice = voice
        self.engine_name = engine_name
        self._buffer = ""
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._worker = asyncio.create_task(self._run())

    async def feed(self, delta: str) -> None:
        """Push a text delta from the model."""
        self._buffer += delta
        while True:
            chunk, rest = _take_sentence(self._buffer)
            if chunk is None:
                break
            self._buffer = rest
            await self._queue.put(chunk)

    async def close(self) -> None:
        """Flush the tail and wait for speech to finish."""
        tail = self._buffer.strip()
        self._buffer = ""
        if tail:
            await self._queue.put(tail)
        await self._queue.put(None)
        try:
            await self._worker
        except asyncio.CancelledError:
            pass

    def cancel(self) -> None:
        self.speech.stop()
        self._worker.cancel()

    async def _run(self) -> None:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            await self.speech.speak_chunk(_clean_for_speech(chunk), self.voice,
                                          self.engine_name)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _sentences(text: str) -> list[str]:
    """Group text into speakable chunks - never a two-word fragment."""
    pieces = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    chunks: list[str] = []
    for piece in pieces:
        if chunks and len(chunks[-1]) < _MIN_CHUNK:
            chunks[-1] = f"{chunks[-1]} {piece}"
        else:
            chunks.append(piece)
    return chunks or ([text] if text.strip() else [])


def _take_sentence(buffer: str) -> tuple[str | None, str]:
    """Pull one complete, long-enough sentence off the front of the buffer."""
    match = _SENTENCE_END.search(buffer)
    if match is None:
        return None, buffer
    head, tail = buffer[:match.start()].strip(), buffer[match.end():]
    if len(head) < _MIN_CHUNK:
        # Too short to speak on its own - wait for the next sentence.
        following = _SENTENCE_END.search(tail)
        if following is None:
            return None, buffer
        head = f"{head} {tail[:following.start()].strip()}".strip()
        tail = tail[following.end():]
    return head, tail


_MARKDOWN = re.compile(r"[*_`#>|]+")
_LINK = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF]")


def _clean_for_speech(text: str) -> str:
    """Strip anything that sounds wrong read aloud."""
    out = _LINK.sub("that link", text or "")
    out = _MARKDOWN.sub("", out)
    out = _EMOJI.sub("", out)
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def _module_available(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _play_file(path: Path, device: Any = None) -> None:
    """Play an audio file, through a specific output device when one is set.

    Device choice is the reason this doesn't just shell out to a system player:
    PowerShell's SoundPlayer and afplay always use the Windows/macOS default
    output, so a machine whose default is the wrong device stays silent no
    matter how loud everything is turned up.
    """
    resolved = resolve_device(device, want_input=False) if device else None

    # Preferred path: sounddevice, which can target a specific device.
    try:
        import sounddevice
        import soundfile

        try:
            data, rate = soundfile.read(str(path), dtype="float32")
        except Exception:
            # Older libsndfile builds can't read MP3 - transcode and retry.
            wav = _transcode_to_wav(path)
            if wav is None:
                raise
            try:
                data, rate = soundfile.read(str(wav), dtype="float32")
            finally:
                wav.unlink(missing_ok=True)

        sounddevice.play(data, rate, device=resolved)
        sounddevice.wait()
        return
    except Exception as exc:
        if resolved is not None:
            # A device was explicitly chosen; falling back would play it
            # somewhere the user can't hear, which looks like "nothing happened".
            log.warning("couldn't play through the selected output device: %s", exc)
        else:
            log.debug("sounddevice playback unavailable: %s", exc)

    if sys.platform == "win32":
        try:
            wav = _transcode_to_wav(path) or path
            subprocess.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                 f'(New-Object Media.SoundPlayer "{wav}").PlaySync();'],
                capture_output=True, timeout=300)
            if wav != path:
                wav.unlink(missing_ok=True)
            return
        except Exception:
            pass
    elif sys.platform == "darwin":
        if shutil.which("afplay"):
            subprocess.run(["afplay", str(path)], capture_output=True, timeout=300)
            return
    else:
        for player in ("ffplay", "mpv", "mpg123", "aplay", "paplay"):
            if shutil.which(player):
                args = ([player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]
                        if player == "ffplay" else [player, str(path)])
                subprocess.run(args, capture_output=True, timeout=300)
                return

    log.warning("no way to play audio - install ffmpeg, or "
                "`pip install sounddevice soundfile`")


def _transcode_to_wav(path: Path) -> Path | None:
    """MP3 -> WAV via ffmpeg, for players that can't handle MP3."""
    if not shutil.which("ffmpeg"):
        return None
    target = path.with_suffix(".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "quiet", "-i", str(path), str(target)],
            capture_output=True, timeout=120)
        return target if target.exists() else None
    except Exception:
        return None
