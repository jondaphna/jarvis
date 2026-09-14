"""Speech input: microphone capture, endpointing, and transcription.

Default is faster-whisper running locally - free, private, no account, and
accurate. Deepgram is available if you'd rather trade a few cents for a bit
less latency on a slow machine.

The capture model is record-then-transcribe with voice-activity endpointing:
JARVIS listens, notices when you stop talking, and transcribes the utterance.
On a local `base.en` model that lands around 300-600ms after you stop, which is
under the threshold where a conversation starts feeling laggy.
"""

from __future__ import annotations

import asyncio
import queue
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import events
from .events import bus, log

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


class MicrophoneError(RuntimeError):
    """The microphone is unavailable or misconfigured."""


class TranscriptionError(RuntimeError):
    """Speech-to-text failed."""


@dataclass
class Utterance:
    text: str
    seconds: float = 0.0
    engine: str = ""
    confidence: float | None = None

    def __bool__(self) -> bool:
        return bool(self.text.strip())


# --------------------------------------------------------------------------- #
# Microphone
# --------------------------------------------------------------------------- #

class Microphone:
    """Records one utterance at a time, ending on silence."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._level = 0.0

    @staticmethod
    def available() -> bool:
        try:
            import sounddevice  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def devices() -> list[dict[str, Any]]:
        try:
            import sounddevice
            return [
                {"index": index, "name": device["name"],
                 "inputs": device["max_input_channels"]}
                for index, device in enumerate(sounddevice.query_devices())
                if device["max_input_channels"] > 0
            ]
        except Exception:
            return []

    @property
    def level(self) -> float:
        """Most recent input level, 0-1. Drives the UI orb."""
        return self._level

    async def record_utterance(
        self,
        max_seconds: float = 30.0,
        silence_ms: int | None = None,
        start_timeout: float = 15.0,
        on_level: Callable[[float], Any] | None = None,
    ) -> bytes:
        """Record until the speaker stops. Returns 16-bit mono PCM at 16kHz."""
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise MicrophoneError(
                "Voice input needs sounddevice and numpy. Run: "
                "pip install sounddevice numpy"
            ) from exc

        silence_ms = silence_ms or int(self.settings.get("voice.vad_silence_ms", 700))
        silence_frames = max(1, silence_ms // FRAME_MS)
        device = self.settings.get("voice.input_device")

        frames: list[bytes] = []
        inbox: queue.Queue = queue.Queue()

        def callback(indata, _frames, _time, status) -> None:
            if status:
                log.debug("audio input status: %s", status)
            inbox.put(bytes(indata))

        try:
            stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, dtype="int16",
                channels=1, callback=callback, device=device)
        except Exception as exc:
            raise MicrophoneError(
                f"Couldn't open the microphone: {exc}. Check the input device in "
                f"Settings, and that Windows lets apps use the mic."
            ) from exc

        # Calibrate to the room rather than assuming a fixed threshold.
        noise_floor = 0.0
        speech_started = False
        quiet_run = 0
        loop = asyncio.get_running_loop()

        with stream:
            deadline = loop.time() + start_timeout
            end_by = loop.time() + max_seconds + start_timeout

            while True:
                if loop.time() > end_by:
                    break
                try:
                    chunk = await asyncio.to_thread(inbox.get, True, 0.5)
                except queue.Empty:
                    if not speech_started and loop.time() > deadline:
                        break
                    continue

                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                energy = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
                self._level = min(1.0, energy * 8)
                if on_level is not None:
                    on_level(self._level)

                if not speech_started:
                    # First ~15 frames establish the noise floor.
                    noise_floor = (energy if noise_floor == 0.0
                                   else noise_floor * 0.92 + energy * 0.08)
                    threshold = max(noise_floor * 3.0, 0.012)
                    if energy > threshold:
                        speech_started = True
                        frames.append(chunk)
                        bus.publish(events.LISTENING, "listening")
                    elif loop.time() > deadline:
                        break
                    continue

                frames.append(chunk)
                if energy < max(noise_floor * 2.0, 0.008):
                    quiet_run += 1
                    if quiet_run >= silence_frames:
                        break
                else:
                    quiet_run = 0

                if len(frames) * FRAME_MS / 1000 >= max_seconds:
                    break

        self._level = 0.0
        return b"".join(frames)


def pcm_to_wav(pcm: bytes, path: Path | None = None) -> Path:
    """Wrap raw PCM in a WAV container - what every STT backend expects."""
    target = path or Path(tempfile.mkstemp(suffix=".wav")[1])
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return target


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #

class Transcriber:
    """Speech to text, local by default."""

    def __init__(self, config) -> None:
        self.config = config
        self.settings = config.settings
        self._whisper: Any = None

    def choose_engine(self, requested: str | None = None) -> str:
        wanted = (requested or self.settings.get("voice.stt_engine", "auto")).lower()
        if wanted not in ("auto", ""):
            return wanted
        if _module_available("faster_whisper"):
            return "faster-whisper"
        if self.config.key("DEEPGRAM_API_KEY"):
            return "deepgram"
        return "none"

    def describe(self) -> str:
        engine = self.choose_engine()
        if engine == "faster-whisper":
            model = self.settings.get("voice.whisper_model", "base.en")
            return f"Whisper {model} (local, free)"
        return {"deepgram": "Deepgram Nova (cloud, paid)",
                "none": "no speech input available - run: pip install faster-whisper"
                }.get(engine, engine)

    def warm_up(self) -> None:
        """Load the local model now, so the first sentence isn't slow."""
        if self.choose_engine() == "faster-whisper":
            try:
                self._load_whisper()
            except Exception as exc:
                log.warning("couldn't preload the speech model: %s", exc)

    def _load_whisper(self):
        if self._whisper is not None:
            return self._whisper
        from faster_whisper import WhisperModel
        from .. import paths

        model = self.settings.get("voice.whisper_model", "base.en")
        log.info("loading local speech model %s (first run downloads it)", model)
        self._whisper = WhisperModel(
            model, device="auto", compute_type="int8",
            download_root=str(paths.CACHE_DIR / "whisper"))
        return self._whisper

    async def transcribe(self, pcm: bytes, engine: str | None = None) -> Utterance:
        if not pcm:
            return Utterance("")
        seconds = len(pcm) / (SAMPLE_RATE * 2)
        if seconds < 0.25:
            return Utterance("", seconds=seconds)   # a cough, not a sentence

        name = self.choose_engine(engine)
        if name == "none":
            raise TranscriptionError(
                "No speech-to-text engine available. Run: pip install faster-whisper")

        try:
            if name == "faster-whisper":
                text, confidence = await asyncio.to_thread(self._whisper_transcribe, pcm)
            elif name == "deepgram":
                text, confidence = await self._deepgram_transcribe(pcm)
            else:
                raise TranscriptionError(f"Unknown speech engine {name!r}")
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(f"Transcription failed: {exc}") from exc

        utterance = Utterance(text.strip(), seconds=seconds, engine=name,
                              confidence=confidence)
        if utterance:
            bus.publish(events.TRANSCRIPT, utterance.text, engine=name)
        return utterance

    # ------------------------------------------------------------------ #

    def _whisper_transcribe(self, pcm: bytes) -> tuple[str, float | None]:
        import numpy as np

        model = self._load_whisper()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = model.transcribe(
            audio, language="en", beam_size=1, vad_filter=True,
            condition_on_previous_text=False)
        pieces = list(segments)
        text = " ".join(segment.text.strip() for segment in pieces).strip()
        confidence = None
        if pieces:
            average = sum(getattr(s, "avg_logprob", 0.0) for s in pieces) / len(pieces)
            confidence = round(min(1.0, max(0.0, 1.0 + average / 2)), 3)
        return text, confidence

    async def _deepgram_transcribe(self, pcm: bytes) -> tuple[str, float | None]:
        import requests

        key = self.config.key("DEEPGRAM_API_KEY")
        if not key:
            raise TranscriptionError("No Deepgram key configured.")

        def _post() -> dict[str, Any]:
            response = requests.post(
                "https://api.deepgram.com/v1/listen"
                "?model=nova-3&smart_format=true&language=en",
                headers={"Authorization": f"Token {key}",
                         "Content-Type": "audio/wav"},
                data=pcm_to_wav(pcm).read_bytes(),
                timeout=60,
            )
            if response.status_code >= 400:
                raise TranscriptionError(f"Deepgram returned {response.status_code}: "
                                         f"{response.text[:200]}")
            return response.json()

        data = await asyncio.to_thread(_post)
        try:
            alternative = data["results"]["channels"][0]["alternatives"][0]
            return alternative.get("transcript", ""), alternative.get("confidence")
        except (KeyError, IndexError) as exc:
            raise TranscriptionError("Deepgram returned an unexpected response.") from exc


# --------------------------------------------------------------------------- #
# Listener - mic + transcriber together
# --------------------------------------------------------------------------- #

class VoiceListener:
    """Convenience wrapper: one call gets you a transcript."""

    def __init__(self, config) -> None:
        self.config = config
        self.settings = config.settings
        self.microphone = Microphone(config.settings)
        self.transcriber = Transcriber(config)

    def available(self) -> bool:
        return Microphone.available() and self.transcriber.choose_engine() != "none"

    def why_unavailable(self) -> str:
        if not Microphone.available():
            return ("No microphone support. Run: pip install sounddevice numpy")
        if self.transcriber.choose_engine() == "none":
            return ("No speech-to-text engine. Run: pip install faster-whisper")
        return ""

    async def listen(self, max_seconds: float = 30.0,
                     start_timeout: float = 15.0,
                     on_level: Callable[[float], Any] | None = None) -> Utterance:
        pcm = await self.microphone.record_utterance(
            max_seconds=max_seconds, start_timeout=start_timeout, on_level=on_level)
        if not pcm:
            return Utterance("")
        return await self.transcriber.transcribe(pcm)

    async def listen_for_wake_word(self, wake_word: str | None = None,
                                   poll_seconds: float = 8.0) -> Utterance | None:
        """Listen in short bursts until the wake word shows up.

        Cheap because the local model is free to run; on a paid STT engine use
        push-to-talk instead so you're not billing every passing noise.
        """
        word = (wake_word or self.settings.get("voice.wake_word", "jarvis")).lower()
        utterance = await self.listen(max_seconds=poll_seconds, start_timeout=poll_seconds)
        if not utterance:
            return None
        lowered = utterance.text.lower()
        if word not in lowered:
            return None
        # Strip the wake word so "jarvis, open chrome" becomes "open chrome".
        index = lowered.find(word)
        remainder = utterance.text[index + len(word):].lstrip(" ,.!?-").strip()
        return Utterance(remainder or utterance.text, utterance.seconds,
                         utterance.engine, utterance.confidence)


def _module_available(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
