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
import threading
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import events
from .audio import list_devices, resolve_device
from .events import bus, log

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
#: Shorter than this isn't a word - it's a cough, a click or a door.
MIN_SPEECH_MS = 250


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
        return list_devices("input")

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
        device = resolve_device(self.settings.get("voice.input_device"),
                                want_input=True)

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


# --------------------------------------------------------------------------- #
# Always-on listening
# --------------------------------------------------------------------------- #

class ContinuousListener:
    """Listens without stopping, so the wake word is never missed.

    The old design recorded a burst, closed the microphone, transcribed, then
    re-opened it - and anything said during transcription was simply lost. Here
    capture and transcription are separate: a background thread holds the
    microphone open the whole time and cuts speech into utterances, while the
    async side transcribes them a beat later. You can talk over JARVIS thinking.

    The audio source is injectable, which is what lets the segmentation logic be
    tested without a microphone.
    """

    def __init__(self, config, source: Any = None) -> None:
        self.config = config
        self.settings = config.settings
        self.transcriber = Transcriber(config)
        self._source = source                 # None = the real microphone
        self._pcm: queue.Queue = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._level = 0.0
        self._error: str = ""

    # ------------------------------------------------------------------ #

    @property
    def level(self) -> float:
        """Live input level 0-1, for the orb."""
        return self._level

    @property
    def error(self) -> str:
        return self._error

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    async def start(self) -> None:
        """Open the microphone and begin segmenting speech."""
        if self.running():
            return
        self._stop.clear()
        self._error = ""
        # Loading Whisper reads ~150MB from disk (and downloads it the first
        # time). Doing that on the event loop froze the whole app.
        await asyncio.to_thread(self.transcriber.warm_up)
        self._thread = threading.Thread(target=self._capture, name="jarvis-mic",
                                        daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)
        self._level = 0.0

    async def utterances(self) -> AsyncIterator[Utterance]:
        """Yield each transcribed utterance as it becomes available."""
        while not self._stop.is_set():
            try:
                pcm = await asyncio.to_thread(self._pcm.get, True, 0.4)
            except queue.Empty:
                if not self.running() and self._error:
                    raise MicrophoneError(self._error)
                continue
            if pcm is None:
                return
            try:
                utterance = await self.transcriber.transcribe(pcm)
            except TranscriptionError as exc:
                log.warning("transcription failed: %s", exc)
                continue
            if utterance:
                yield utterance

    # ------------------------------------------------------------------ #
    # Capture thread
    # ------------------------------------------------------------------ #

    def _frames(self):
        """Yield raw PCM frames, from the microphone or an injected source."""
        if self._source is not None:
            yield from self._source
            return

        import sounddevice as sd

        device = resolve_device(self.settings.get("voice.input_device"),
                               want_input=True)
        inbox: queue.Queue = queue.Queue()

        def callback(indata, _frames, _time, status) -> None:
            if status:
                log.debug("audio input status: %s", status)
            inbox.put(bytes(indata))

        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES,
                               dtype="int16", channels=1, callback=callback,
                               device=device):
            while not self._stop.is_set():
                try:
                    yield inbox.get(timeout=0.4)
                except queue.Empty:
                    continue

    def _capture(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self._error = "Voice input needs numpy. Run: pip install numpy"
            log.warning(self._error)
            return

        silence_ms = int(self.settings.get("voice.vad_silence_ms", 700))
        silence_frames = max(2, silence_ms // FRAME_MS)
        max_frames = int(30_000 / FRAME_MS)
        # A short pre-roll keeps the first syllable of "Jarvis" from being cut.
        preroll_frames = max(3, 300 // FRAME_MS)

        preroll: deque[bytes] = deque(maxlen=preroll_frames)
        collected: list[bytes] = []
        speaking = False
        quiet_run = 0
        voiced_frames = 0
        noise_floor = 0.0
        calibrated = 0

        try:
            for frame in self._frames():
                if self._stop.is_set():
                    break

                samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                energy = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
                self._level = min(1.0, energy * 8)

                # Track the room's noise level, but only while nobody is talking.
                if not speaking:
                    if calibrated < 25:
                        noise_floor = energy if calibrated == 0 else \
                            noise_floor * 0.85 + energy * 0.15
                        calibrated += 1
                    else:
                        noise_floor = noise_floor * 0.995 + energy * 0.005

                speech_on = max(noise_floor * 3.0, 0.015)
                speech_off = max(noise_floor * 1.8, 0.008)

                if not speaking:
                    preroll.append(frame)
                    if energy > speech_on and calibrated >= 5:
                        speaking = True
                        quiet_run = 0
                        voiced_frames = 1
                        collected = list(preroll)
                        preroll.clear()
                    continue

                collected.append(frame)
                if energy < speech_off:
                    quiet_run += 1
                else:
                    quiet_run = 0
                    voiced_frames += 1

                if quiet_run >= silence_frames or len(collected) >= max_frames:
                    # Measure the *voiced* part only. Counting the pre-roll and
                    # the trailing silence would let a door slam through as a word.
                    if voiced_frames * FRAME_MS >= MIN_SPEECH_MS:
                        self._emit(b"".join(collected))
                    speaking = False
                    quiet_run = 0
                    voiced_frames = 0
                    collected = []

            if speaking and voiced_frames * FRAME_MS >= MIN_SPEECH_MS:
                self._emit(b"".join(collected))

        except Exception as exc:
            self._error = str(exc)
            log.warning("microphone capture stopped: %s", exc, exc_info=True)
        finally:
            self._level = 0.0

    def _emit(self, pcm: bytes) -> None:
        try:
            self._pcm.put_nowait(pcm)
        except queue.Full:
            # Transcription is behind; drop the oldest rather than block capture.
            try:
                self._pcm.get_nowait()
                self._pcm.put_nowait(pcm)
            except queue.Empty:
                pass


def contains_wake_word(text: str, wake_word: str) -> tuple[bool, str]:
    """Did they say the wake word, and what did they say after it?

    Speech-to-text mangles names, so "Jarvis" also arrives as "Jarvis,",
    "Service", "Travis" or "Charvis". Accepting near-misses matters more than
    being strict: the cost of a false positive is one unnecessary "yes?", and
    the cost of a false negative is the whole thing feeling broken.
    """
    import re as _re

    lowered = (text or "").lower()
    word = (wake_word or "jarvis").lower().strip()
    if not lowered.strip():
        return False, ""

    variants = [word]
    if word == "jarvis":
        variants += ["jarvis", "jarvus", "jervis", "jarves", "charvis", "travis",
                     "service", "servis", "javis", "jarv", "yarvis", "harvis"]

    for variant in variants:
        match = _re.search(r"\b" + _re.escape(variant) + r"\b", lowered)
        if match is None:
            continue
        remainder = text[match.end():].lstrip(" ,.!?-–—:;").strip()
        return True, remainder

    return False, ""
