"""Voiceprint recognition - so JARVIS answers you and ignores everyone else.

How it works: a speaker-embedding model turns a few seconds of speech into a
vector that characterises the voice rather than the words. Enrol once with a
handful of samples, and every later utterance is scored by cosine similarity
against that profile. Below the threshold, JARVIS doesn't respond.

PRIVACY, stated plainly: a voiceprint is biometric data. It is computed on this
machine, stored on this machine, and never sent anywhere - not to Claude, not to
any API. The stored file holds only the numeric embedding, never the audio. You
can delete it any time with `jarvis voiceprint forget`.

WHAT THIS IS NOT: it is a convenience filter, not a security control. A good
recording of your voice can pass it. Nothing that actually matters - spending
money, publishing, sending mail - is gated on it; those still need the
permission system. Treat it as "ignore the television", not "authenticate the
user".
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .events import log

SAMPLE_RATE = 16000

#: Cosine similarity above which a voice is considered a match. Tuned to be
#: forgiving: wrongly ignoring the owner is far more annoying than occasionally
#: answering someone else.
DEFAULT_THRESHOLD = 0.70

#: Enrolment needs enough speech to characterise a voice rather than a phrase.
MIN_ENROL_SECONDS = 2.0
RECOMMENDED_SAMPLES = 3


class SpeakerIDUnavailable(RuntimeError):
    """No embedding backend is installed."""


@dataclass
class VerificationResult:
    accepted: bool
    score: float
    reason: str = ""
    enrolled: bool = True

    def __bool__(self) -> bool:
        return self.accepted


@dataclass
class Voiceprint:
    """The stored profile: one averaged embedding plus the samples behind it."""

    name: str = "owner"
    embeddings: list[list[float]] = field(default_factory=list)
    backend: str = ""
    threshold: float = DEFAULT_THRESHOLD
    created_at: str = ""

    @property
    def sample_count(self) -> int:
        return len(self.embeddings)

    def centroid(self) -> list[float] | None:
        if not self.embeddings:
            return None
        width = len(self.embeddings[0])
        totals = [0.0] * width
        for embedding in self.embeddings:
            for index, value in enumerate(embedding):
                totals[index] += value
        return [total / len(self.embeddings) for total in totals]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "embeddings": self.embeddings,
            "backend": self.backend,
            "threshold": self.threshold,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Voiceprint":
        return cls(
            name=raw.get("name", "owner"),
            embeddings=[list(map(float, e)) for e in raw.get("embeddings", [])],
            backend=raw.get("backend", ""),
            threshold=float(raw.get("threshold", DEFAULT_THRESHOLD)),
            created_at=raw.get("created_at", ""),
        )


# --------------------------------------------------------------------------- #
# Embedding backends
# --------------------------------------------------------------------------- #

Embedder = Callable[[bytes], Sequence[float]]


def _resemblyzer_embedder() -> tuple[Embedder, str]:
    from resemblyzer import VoiceEncoder, preprocess_wav   # type: ignore
    import numpy as np

    encoder = VoiceEncoder(verbose=False)

    def embed(pcm: bytes) -> Sequence[float]:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        processed = preprocess_wav(audio, source_sr=SAMPLE_RATE)
        return encoder.embed_utterance(processed).tolist()

    return embed, "resemblyzer"


def _speechbrain_embedder() -> tuple[Embedder, str]:
    import numpy as np
    import torch                                            # type: ignore
    from speechbrain.inference.speaker import EncoderClassifier  # type: ignore

    from .. import paths

    classifier = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(paths.CACHE_DIR / "speechbrain"),
    )

    def embed(pcm: bytes) -> Sequence[float]:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(audio).unsqueeze(0)
        with torch.no_grad():
            vector = classifier.encode_batch(tensor).squeeze().cpu().numpy()
        return vector.tolist()

    return embed, "speechbrain"


def load_embedder() -> tuple[Embedder, str]:
    """First backend that imports wins. Resemblyzer is the lighter of the two."""
    errors = []
    for factory in (_resemblyzer_embedder, _speechbrain_embedder):
        try:
            return factory()
        except ImportError as exc:
            errors.append(str(exc))
        except Exception as exc:
            log.warning("speaker-id backend failed to load: %s", exc)
            errors.append(str(exc))
    raise SpeakerIDUnavailable(
        "Voice recognition needs a speaker-embedding model. Install one with:\n"
        "  pip install resemblyzer\n"
        f"({'; '.join(errors[:2])})")


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #

def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SpeakerVerifier:
    """Enrol a voice, then decide whether later speech belongs to it."""

    def __init__(self, settings, profile_path: Path | None = None,
                 embedder: Embedder | None = None) -> None:
        from .. import paths

        self.settings = settings
        self.path = profile_path or (paths.ROOT / "voiceprint.json")
        self._embedder = embedder
        self._backend = "injected" if embedder else ""
        self._profile: Voiceprint | None = None
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    @property
    def enrolled(self) -> bool:
        return self._profile is not None and self._profile.sample_count > 0

    @property
    def enabled(self) -> bool:
        """On only when switched on AND a voice has actually been enrolled."""
        return bool(self.settings.get("voice.only_my_voice", False)) and self.enrolled

    @property
    def profile(self) -> Voiceprint | None:
        return self._profile

    def threshold(self) -> float:
        configured = self.settings.get("voice.speaker_threshold")
        if configured is not None:
            try:
                return float(configured)
            except (TypeError, ValueError):
                pass
        if self._profile is not None:
            return self._profile.threshold
        return DEFAULT_THRESHOLD

    def available(self) -> bool:
        try:
            self._get_embedder()
            return True
        except SpeakerIDUnavailable:
            return False

    def why_unavailable(self) -> str:
        try:
            self._get_embedder()
            return ""
        except SpeakerIDUnavailable as exc:
            return str(exc)

    def _get_embedder(self) -> Embedder:
        with self._lock:
            if self._embedder is None:
                self._embedder, self._backend = load_embedder()
            return self._embedder

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._profile = Voiceprint.from_dict(
                json.loads(self.path.read_text("utf-8")))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log.warning("couldn't read the voiceprint: %s", exc)

    def _save(self) -> None:
        if self._profile is None:
            return
        from .. import paths

        paths.ensure_dirs()
        self.path.write_text(json.dumps(self._profile.to_dict()), encoding="utf-8")
        try:
            import sys
            if sys.platform != "win32":
                self.path.chmod(0o600)
        except OSError:
            pass

    def forget(self) -> bool:
        """Delete the stored voiceprint."""
        with self._lock:
            existed = self.path.exists()
            self.path.unlink(missing_ok=True)
            self._profile = None
        self.settings.set("voice.only_my_voice", False)
        try:
            self.settings.save()
        except Exception:
            pass
        return existed

    # ------------------------------------------------------------------ #
    # Enrolment and verification
    # ------------------------------------------------------------------ #

    def embed(self, pcm: bytes) -> list[float]:
        return list(self._get_embedder()(pcm))

    def enrol(self, samples: Sequence[bytes], name: str = "owner") -> Voiceprint:
        """Build a profile from several recordings of the same voice."""
        from datetime import datetime, timezone

        usable = [pcm for pcm in samples
                  if len(pcm) >= int(MIN_ENROL_SECONDS * SAMPLE_RATE * 2)]
        if not usable:
            raise ValueError(
                f"Each sample needs at least {MIN_ENROL_SECONDS:.0f} seconds of "
                f"speech. Try saying a full sentence.")

        embeddings = [self.embed(pcm) for pcm in usable]
        with self._lock:
            self._profile = Voiceprint(
                name=name,
                embeddings=embeddings,
                backend=self._backend,
                threshold=self.threshold(),
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self._save()
        log.info("voiceprint enrolled from %d sample(s)", len(embeddings))
        return self._profile

    def add_sample(self, pcm: bytes) -> int:
        """Reinforce an existing profile - handy if it keeps missing you."""
        if self._profile is None:
            raise ValueError("Nothing enrolled yet.")
        with self._lock:
            self._profile.embeddings.append(self.embed(pcm))
            self._save()
            return self._profile.sample_count

    def score(self, pcm: bytes) -> float:
        """Similarity of this audio to the enrolled voice, 0-1."""
        if self._profile is None:
            return 0.0
        centroid = self._profile.centroid()
        if centroid is None:
            return 0.0
        candidate = self.embed(pcm)
        # Best of (centroid, each sample): people sound different across a day,
        # and the closest single sample is often a better match than the mean.
        best = cosine_similarity(candidate, centroid)
        for embedding in self._profile.embeddings:
            best = max(best, cosine_similarity(candidate, embedding))
        return best

    def verify(self, pcm: bytes) -> VerificationResult:
        """Should JARVIS respond to this audio?"""
        if not self.settings.get("voice.only_my_voice", False):
            return VerificationResult(True, 1.0, "voice matching is off")
        if not self.enrolled:
            return VerificationResult(
                True, 1.0,
                "no voiceprint enrolled yet - run `jarvis voiceprint enrol`",
                enrolled=False)

        try:
            similarity = self.score(pcm)
        except SpeakerIDUnavailable as exc:
            # Never let a missing model silence JARVIS completely.
            log.warning("speaker check skipped: %s", exc)
            return VerificationResult(True, 0.0, "speaker model unavailable")
        except Exception as exc:
            log.warning("speaker check failed: %s", exc, exc_info=True)
            return VerificationResult(True, 0.0, "speaker check failed")

        threshold = self.threshold()
        if similarity >= threshold:
            return VerificationResult(True, similarity, "voice matched")
        return VerificationResult(
            False, similarity,
            f"voice didn't match ({similarity:.2f} < {threshold:.2f})")

    def describe(self) -> str:
        if not self.enrolled:
            return "no voiceprint enrolled"
        assert self._profile is not None
        state = "on" if self.enabled else "off"
        return (f"{self._profile.sample_count} sample(s), "
                f"threshold {self.threshold():.2f}, matching {state}")
