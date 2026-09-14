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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import events
from .events import bus, log

#: Split on sentence ends, but not on decimals, abbreviations or ellipses.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
_MIN_CHUNK = 60          # don't bother speaking fragments shorter than this


class SpeechError(RuntimeError):
    """Text-to-speech failed in a way worth reporting."""


class SpeechEngine:
    """Turns text into audio, and plays it."""

    def __init__(self, config) -> None:
        self.config = config
        self.settings = config.settings
        self._cancel = asyncio.Event()
        self._speaking = False
        self._current: Any = None

    # ------------------------------------------------------------------ #
    # Engine selection
    # ------------------------------------------------------------------ #

    def choose_engine(self, requested: str | None = None) -> str:
        wanted = (requested or self.settings.get("voice.tts_engine", "auto")).lower()
        if wanted not in ("auto", ""):
            return wanted

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
        return self._speaking

    def stop(self) -> None:
        """Interrupt whatever is being said. Used for barge-in."""
        self._cancel.set()

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
                await self._speak_chunk(chunk, voice, engine)
        finally:
            self._speaking = False

    async def stream(self, voice: str | None = None,
                     engine: str | None = None) -> "SpeechStream":
        """A sink you push text deltas into; it speaks each finished sentence."""
        return SpeechStream(self, voice=voice, engine=engine)

    async def _speak_chunk(self, text: str, voice: str | None,
                           engine: str | None) -> None:
        name = self.choose_engine(engine)
        if name == "none":
            log.info("SPEAK (no engine): %s", text)
            return
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

    # ------------------------------------------------------------------ #
    # Synthesis backends
    # ------------------------------------------------------------------ #

    async def _synthesise(self, text: str, voice: str | None, engine: str) -> bytes:
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
            await asyncio.to_thread(_play_file, path)
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

    def __init__(self, engine: SpeechEngine, voice: str | None = None,
                 engine_name: str | None = None, **kwargs: Any) -> None:
        self.engine = engine
        self.voice = voice
        self.engine_name = engine_name or kwargs.get("engine")
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
        self.engine.stop()
        self._worker.cancel()

    async def _run(self) -> None:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            await self.engine._speak_chunk(_clean_for_speech(chunk), self.voice,
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


def _play_file(path: Path) -> None:
    """Play an audio file with whatever this machine has."""
    try:
        import sounddevice
        import soundfile
        data, rate = soundfile.read(str(path), dtype="float32")
        sounddevice.play(data, rate)
        sounddevice.wait()
        return
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            # Built into Windows - no dependency, no console window.
            subprocess.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                 f'(New-Object Media.SoundPlayer "{path}").PlaySync();'],
                capture_output=True, timeout=300)
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

    log.warning("no audio player available - install ffmpeg or `pip install sounddevice soundfile`")
