"""OpenAI-backed audio and sampled-frame detectors.

Media is sent to the OpenAI API only when these methods are called. Audio is
transcribed, then the transcript is moderated. Image frames are sent directly
to the moderation endpoint. No raw media or transcript is retained here.
"""

from __future__ import annotations

import base64
import io
import wave
from typing import Any, Mapping, Optional, Sequence

from moderation import Signal, SignalKind


MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_LANGUAGE_CATEGORIES = (
    "hate",
    "hate/threatening",
    "harassment",
    "harassment/threatening",
)


def pcm16_to_wav(pcm_data: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap signed 16-bit little-endian PCM in a WAV container."""
    if sample_rate <= 0 or channels <= 0:
        raise ValueError("sample_rate and channels must be positive")
    if len(pcm_data) % (2 * channels) != 0:
        raise ValueError("PCM byte length must align to 16-bit channel samples")

    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return output.getvalue()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(by_alias=True)
    return vars(value)


def _score(scores: Any, category: str) -> float:
    values = _as_mapping(scores)
    candidates = (category, category.replace("/", "_"))
    for key in candidates:
        value = values.get(key)
        if value is not None:
            return float(value)
        value = getattr(scores, key.replace("/", "_"), None)
        if value is not None:
            return float(value)
    return 0.0


def _image_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("frame must be encoded as JPEG, PNG, or WebP")


class OpenAIDetectors:
    """Convert audio chunks and sampled frames into moderation Signals.

    Pass a supported audio file payload (WAV, WebM, MP4, or MP3), or convert
    PCM16 with ``pcm16_to_wav`` first.
    Pass each sampled video frame as JPEG, PNG, or WebP bytes. Supply a unique
    sample ID and a monotonic timestamp in seconds for every chunk/frame.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        transcription_model: str = "gpt-transcribe",
        moderation_model: str = "omni-moderation-latest",
        language_categories: Sequence[str] = DEFAULT_LANGUAGE_CATEGORIES,
    ) -> None:
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "Install the OpenAI SDK with `pip install -r requirements.txt`."
                ) from exc
            client = OpenAI()

        self.client = client
        self.transcription_model = transcription_model
        self.moderation_model = moderation_model
        self.language_categories = tuple(language_categories)
        if not self.language_categories or any(not category for category in self.language_categories):
            raise ValueError("language_categories must contain nonempty category names")

    def transcribe_audio_chunk(
        self, audio_bytes: bytes, filename: str = "meeting-chunk.wav"
    ) -> Optional[str]:
        """Convert one supported audio chunk to text without retaining it."""
        if not audio_bytes:
            raise ValueError("audio_bytes must not be empty")
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise ValueError("audio chunk exceeds the 25 MB transcription limit")

        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = filename
        transcription = self.client.audio.transcriptions.create(
            model=self.transcription_model,
            file=audio_file,
        )
        text = getattr(transcription, "text", "").strip()
        return text or None

    def transcript_signal(
        self,
        text: str,
        sample_id: str,
        timestamp: float,
        kind: SignalKind = SignalKind.POTENTIALLY_HARMFUL_LANGUAGE,
    ) -> Optional[Signal]:
        """Run the NLP moderation model on text and return its language score."""
        text = text.strip()
        if not text:
            return None

        response = self.client.moderations.create(
            model=self.moderation_model,
            input=text,
        )
        scores = response.results[0].category_scores
        language_score = max(
            _score(scores, category) for category in self.language_categories
        )
        return Signal(
            kind=kind,
            confidence=language_score,
            timestamp=timestamp,
            sample_id=sample_id,
        )

    def audio_chunk_signal(
        self,
        audio_bytes: bytes,
        sample_id: str,
        timestamp: float,
        filename: str = "meeting-chunk.wav",
    ) -> Optional[Signal]:
        """Transcribe an audio chunk, then classify it with the NLP model."""
        text = self.transcribe_audio_chunk(audio_bytes, filename=filename)
        if not text:
            return None
        return self.transcript_signal(text, sample_id, timestamp)

    def frame_signals(
        self, image_bytes: bytes, sample_id: str, timestamp: float
    ) -> Sequence[Signal]:
        """Score explicit and injury-related content in one sampled frame."""
        if not image_bytes:
            raise ValueError("image_bytes must not be empty")
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("image frame exceeds the 20 MB moderation limit")

        mime_type = _image_mime(image_bytes)
        data_url = (
            f"data:{mime_type};base64,"
            f"{base64.b64encode(image_bytes).decode('ascii')}"
        )
        response = self.client.moderations.create(
            model=self.moderation_model,
            input=[
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            ],
        )
        scores = response.results[0].category_scores

        return (
            Signal(
                kind=SignalKind.EXPLICIT_VISUAL,
                confidence=_score(scores, "sexual"),
                timestamp=timestamp,
                sample_id=sample_id,
            ),
            Signal(
                kind=SignalKind.VISIBLE_INJURY,
                confidence=max(
                    _score(scores, "violence"),
                    _score(scores, "violence/graphic"),
                ),
                timestamp=timestamp,
                sample_id=sample_id,
            ),
        )
