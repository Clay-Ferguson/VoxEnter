"""Audio -> text via faster-whisper. Deliberately Qt-free.

Everything here is plain Python and numpy, so the resampling, normalization
and whisper post-processing rules can be exercised without a display or a
microphone attached -- feed `transcribe_audio` samples read from a WAV file.

The model is loaded once and kept in memory for the life of the process, so
an utterance costs only inference, not a model load. faster_whisper itself is
imported lazily, inside `get_model`: it pulls in CTranslate2 and onnxruntime,
which take long enough to import that doing it at module scope would delay
the window appearing.

The commented-out `log.debug` calls are left in place on purpose: they are the
diagnostic scaffolding referenced by TROUBLESHOOTING.md when a quiet mic is
not triggering, and are meant to be uncommented rather than rewritten.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

# English-only base model. faster-whisper downloads it from Hugging Face
# (Systran/faster-whisper-base.en) on first use and caches it under
# ~/.cache/huggingface; setup.sh triggers that download ahead of time.
MODEL_NAME = "base.en"

# No GPU path: CTranslate2's GPU support is CUDA-only. int8 runs a quantized
# copy of the weights on the CPU -- faster, with negligible accuracy loss.
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
CPU_THREADS = 4

# Recording at 48kHz (common USB mic rate), resampled to 16kHz for whisper.
RECORD_SAMPLE_RATE = 48000  # Rate to record from microphone
WHISPER_SAMPLE_RATE = 16000  # Rate whisper expects
CHANNELS = 1
DTYPE = np.int16

# =============================================================================
# Model
# =============================================================================

_model: WhisperModel | None = None
# Held for the whole load, so a transcription that arrives while the preload
# is still running waits for it instead of loading a second copy.
_model_lock = threading.Lock()


def get_model() -> WhisperModel:
    """Return the shared model, loading (and if need be downloading) it once.

    One model is shared by every worker thread. With CTranslate2's default of
    one worker, concurrent transcribe() calls queue rather than run in
    parallel, which is exactly what back-to-back utterances want.
    """
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            log.info(f"Loading faster-whisper model '{MODEL_NAME}' ({DEVICE}, {COMPUTE_TYPE})...")
            start = time.time()
            options = dict(device=DEVICE, compute_type=COMPUTE_TYPE, cpu_threads=CPU_THREADS)
            # Cache first: without local_files_only, huggingface_hub asks the
            # Hub about the model on every launch, even when it is already on
            # disk. Only a missing model goes to the network.
            try:
                _model = WhisperModel(MODEL_NAME, local_files_only=True, **options)
            except Exception:
                log.info("Model not cached yet -- downloading from Hugging Face (~145MB)...")
                _model = WhisperModel(MODEL_NAME, **options)
            log.info(f"Model loaded in {time.time() - start:.2f}s")
        return _model


def preload_model() -> None:
    """Load the model on a background thread, so the first utterance is fast.

    Failure is only logged: transcribe_audio will try again, and report it,
    when there is actually something to transcribe.
    """

    def load() -> None:
        try:
            get_model()
        except Exception as e:
            log.error(f"Failed to preload whisper model: {e}", exc_info=True)

    threading.Thread(target=load, daemon=True).start()


# =============================================================================
# Audio Conditioning
# =============================================================================


def normalize_audio(audio_data: np.ndarray) -> np.ndarray:
    """Normalize audio to use more of the int16 dynamic range.

    This helps whisper recognize quieter audio.
    """
    audio_float = audio_data.astype(np.float32)

    peak = max(abs(audio_float.min()), abs(audio_float.max()))

    if peak < 100:
        # Audio is basically silent, don't amplify noise
        # log.debug(f"Audio too quiet to normalize (peak={peak})")
        return audio_data

    # Target peak at 80% of int16 max to avoid clipping
    target_peak = 32767 * 0.8
    gain = target_peak / peak

    # Limit gain to avoid amplifying quiet audio too much
    gain = min(gain, 20.0)  # Max 20x amplification

    # log.debug(f"Normalizing audio: peak={peak:.0f}, gain={gain:.2f}x")

    normalized = audio_float * gain

    normalized = np.clip(normalized, -32768, 32767)
    return normalized.astype(np.int16)


def resample_audio(audio_data: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
    """Resample audio from orig_rate to target_rate using linear interpolation.

    Accepts the (N, channels) shape sounddevice produces as well as 1D, and
    always returns 1D.
    """
    if audio_data.ndim > 1:
        audio_data = audio_data.flatten()

    if orig_rate == target_rate:
        return audio_data

    duration = len(audio_data) / orig_rate
    num_samples = int(duration * target_rate)

    indices = np.linspace(0, len(audio_data) - 1, num_samples)

    resampled = np.interp(indices, np.arange(len(audio_data)), audio_data.astype(np.float32))

    return resampled.astype(np.int16)


# =============================================================================
# Transcription
# =============================================================================


def transcribe_audio(audio_data: np.ndarray) -> str | None:
    """Transcribe int16 samples at RECORD_SAMPLE_RATE, returning text or None.

    Returns None rather than raising for every failure mode (model unavailable,
    inference error, hallucination filtered out), because every caller treats
    "no usable text" identically.
    """
    try:
        model = get_model()
    except Exception as e:
        log.error(f"Whisper model unavailable: {e}")
        return None

    if RECORD_SAMPLE_RATE != WHISPER_SAMPLE_RATE:
        audio_data = resample_audio(audio_data, RECORD_SAMPLE_RATE, WHISPER_SAMPLE_RATE)

    # Normalize audio to use more dynamic range (helps whisper with quiet audio)
    audio_data = normalize_audio(audio_data)

    # faster-whisper takes the waveform directly -- mono float32 in [-1, 1] at
    # 16kHz -- so there is no temp WAV file to write and clean up.
    audio = audio_data.astype(np.float32) / 32768.0

    try:
        segments, _info = model.transcribe(
            audio,
            language="en",
            beam_size=5,
            without_timestamps=True,
            # Silence detection already happened in audio.py; a second VAD
            # pass here would only trim what that state machine chose to keep.
            vad_filter=False,
            # Each utterance is independent, so there is no earlier text in
            # this call worth conditioning on.
            condition_on_previous_text=False,
        )

        # `segments` is a lazy generator: the actual inference happens here,
        # as it is consumed.
        clean_lines = []
        for segment in segments:
            line = segment.text.strip()
            if not line:
                continue
            # Skip blank-audio markers such as "[BLANK_AUDIO]"
            if line.startswith("["):
                continue
            clean_lines.append(line)

        text = " ".join(clean_lines)

        # Filter out whisper hallucinations: if the first character is not
        # alphanumeric it is likely "(music)" or "[BLANK_AUDIO]" - ignore it.
        if text and not text[0].isalnum():
            return None

        # Post-process for continuous dictation: this is a sentence fragment
        # being appended mid-sentence, not a standalone utterance.
        if text:
            while text and text[-1] in ".?!,;:":
                text = text[:-1]
            text = text.strip()
            if text and text[0].isupper():
                text = text[0].lower() + text[1:]
            if text:
                text = text + " "  # Add space for smooth sentence flow

        return text

    except Exception as e:
        log.error(f"Transcription exception: {e}")
        return None
