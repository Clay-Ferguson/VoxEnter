"""Continuous recording with silence detection.

The RMS state machine in `_audio_callback` is tuned against real microphones
and is carried over unchanged from Lingo, the app VoxEnter was derived from;
drift here shows up as missed utterances or spurious ones, so resist
"tidying" it.

Results reach the UI through Qt signals: with an auto connection to a
main-thread receiver, `emit` from the PortAudio callback thread posts a queued
call rather than running the slot inline.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .config import DEFAULT_SILENCE_THRESHOLD
from .transcribe import CHANNELS, DTYPE, RECORD_SAMPLE_RATE, transcribe_audio

log = logging.getLogger(__name__)

# =============================================================================
# Silence Detection Tuning
# =============================================================================

SILENCE_DURATION_S = 1.0  # Seconds of silence before transcription
MIN_AUDIO_DURATION_S = 0.5  # Minimum audio length to process
MIN_VOICED_DURATION_S = 0.2  # Minimum time above threshold to process
SPEECH_CONFIRM_CHUNKS = 3  # Consecutive above-threshold chunks to confirm speech
BLOCK_SIZE = 1024  # Frames per callback, so each RMS value covers ~21ms

# =============================================================================
# Threshold Calibration Tuning
# =============================================================================

CALIBRATION_DURATION_S = 3.0  # How long to listen to the room
CALIBRATION_SETTLE_S = 0.3  # Discarded: stream start-up and the button's own click
CALIBRATION_PERCENTILE = 95  # Noise level, ignoring the loudest few blocks
CALIBRATION_MARGIN_DB = 9.0  # How far above the noise level the threshold sits
# In a quiet room the floor, not the margin, decides the threshold: the noise
# there measures ~0.0003, and keyboard and mouse clicks reach ~0.0025, which a
# floor of 0.002 let through as speech. Speech starts well above 0.005.
MIN_CALIBRATED_THRESHOLD = 0.004


def block_rms(indata: np.ndarray) -> float:
    """Loudness of one int16 block, normalized to 0..1."""
    audio_float = indata.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(audio_float ** 2)))


def threshold_from_noise(noise_rms: list[float]) -> float:
    """Pick a silence threshold that the measured background noise stays under.

    The constraint that matters is the end of an utterance: the recorder waits
    for SILENCE_DURATION_S of *unbroken* below-threshold blocks, so noise that
    pokes over the threshold even occasionally keeps resetting that timer and
    the utterance never ends. Hence a high percentile rather than the mean,
    with a margin on top. The percentile rather than the max, so one stray
    click during the measurement does not drag the threshold up to where you
    have to shout.
    """
    noise = float(np.percentile(noise_rms, CALIBRATION_PERCENTILE))
    margin = 10 ** (CALIBRATION_MARGIN_DB / 20)  # dB to an RMS amplitude ratio
    # Rounded to the precision the settings dialog shows, so what the user
    # sees is exactly what is saved.
    return round(max(noise * margin, MIN_CALIBRATED_THRESHOLD), 4)


def find_input_device(name: str) -> int | None:
    """Index of the named input device, or None if it is not present."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["name"] == name and d["max_input_channels"] > 0:
            log.info(f"Using audio device: [{i}] {d['name']}")
            return i
    log.warning(f"Device '{name}' not found in available devices")
    available = [d["name"] for d in devices if d["max_input_channels"] > 0]
    log.info(f"Available input devices: {available}")
    return None


class AudioRecorder(QObject):
    """Records continuously, emitting transcribed text once speech ends."""

    # 'idle' | 'speech-detected' | 'transcribing' | 'typing'
    phase_changed = pyqtSignal(str)
    speech_transcribed = pyqtSignal(str)

    def __init__(self, audio_device: str | None = None,
                 silence_threshold: float = DEFAULT_SILENCE_THRESHOLD) -> None:
        super().__init__()
        self.audio_device = audio_device  # Device name or None for default
        self.is_running = False
        self.audio_buffer: list[np.ndarray] = []
        self.silence_start_time: float | None = None
        self.recording_start_time: float | None = None
        self.speech_detected = False  # Track if we've heard speech above threshold
        self.speech_confirm_count = 0  # Consecutive above-threshold chunks
        self.voiced_frames = 0  # Count of frames above threshold
        self.peak_rms = 0.0
        self.stream: Any = None
        # Reentrant: _audio_callback holds this and calls _transition_to_phase,
        # which takes it again.
        self.lock = threading.RLock()
        self.current_phase = "idle"
        self.silence_threshold = float(silence_threshold)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> tuple[bool, str | None]:
        """Start recording. Returns (success, error_message).

        Failures are returned rather than raised because every one of them is
        something to show the user in a dialog, not a bug.
        """
        self.is_running = True
        self.audio_buffer = []
        self.silence_start_time = None
        self.recording_start_time = time.time()
        self.speech_detected = False
        self.current_phase = "idle"
        self.voiced_frames = 0
        self.peak_rms = 0.0

        # Find the audio device by name
        device = None
        if self.audio_device:
            device = find_input_device(self.audio_device)
            if device is None:
                self.is_running = False
                return (False, f"Microphone '{self.audio_device}' not found.\n\nIt may be in use by another application (like Chrome).\n\nTry closing other apps that use the microphone and try again.")

        log.info(f"Starting audio stream: {RECORD_SAMPLE_RATE}Hz, {CHANNELS} channel(s), device={device}")

        try:
            self.stream = sd.InputStream(
                samplerate=RECORD_SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=self._audio_callback,
                blocksize=BLOCK_SIZE,
                device=device,
            )
            self.stream.start()
            log.info("Audio stream started successfully")
            return (True, None)
        except sd.PortAudioError as e:
            error_msg = str(e)
            log.error(f"Failed to open audio stream: {error_msg}")
            self.is_running = False
            return (False, f"Failed to open microphone.\n\nError: {error_msg}\n\nThe device may be in use by another application.")
        except Exception as e:
            error_msg = str(e)
            log.error(f"Unexpected error opening audio stream: {error_msg}")
            self.is_running = False
            return (False, f"Unexpected error opening microphone:\n\n{error_msg}")

    def stop(self) -> None:
        """Stop recording."""
        self.is_running = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self._transition_to_phase("idle")

    # -- phase / settings --------------------------------------------------

    def _transition_to_phase(self, phase: str) -> None:
        notify = False
        with self.lock:
            if self.current_phase != phase:
                self.current_phase = phase
                notify = True
        if notify:
            self.phase_changed.emit(phase)

    def notify_typing_done(self) -> None:
        """Called by the window once injected keystrokes have all been sent."""
        self._transition_to_phase("idle")

    def set_silence_threshold(self, value: float) -> None:
        with self.lock:
            self.silence_threshold = float(value)
        log.info(f"Silence threshold updated to {self.silence_threshold:.6f}")

    # -- the hot path ------------------------------------------------------

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Called for each audio chunk from the microphone (PortAudio thread)."""
        if not self.is_running:
            return

        # RMS (root mean square) for volume level. Calibration measures with
        # the same function, so the two can never disagree about loudness.
        rms = block_rms(indata)

        # Track peak RMS for diagnostics
        self.peak_rms = max(self.peak_rms, rms)

        with self.lock:
            # Always accumulate audio
            self.audio_buffer.append(indata.copy())

            now = time.time()
            threshold = self.silence_threshold

            if rms < threshold:
                if self.current_phase == "speech-detected" and not self.speech_detected:
                    self._transition_to_phase("idle")
                # Below threshold - might be silence
                if self.silence_start_time is None:
                    self.silence_start_time = now
                elif now - self.silence_start_time >= SILENCE_DURATION_S:
                    # Enough silence - process if we have audio AND speech was detected
                    audio_duration = now - self.recording_start_time
                    voiced_duration_s = self.voiced_frames / RECORD_SAMPLE_RATE

                    if (
                        len(self.audio_buffer) > 0
                        and audio_duration >= MIN_AUDIO_DURATION_S
                        and voiced_duration_s >= MIN_VOICED_DURATION_S
                        and self.speech_detected
                    ):
                        audio_data = np.concatenate(self.audio_buffer)
                        log.info(
                            ">>> TRIGGERING WHISPER: "
                            f"{len(audio_data)} samples ({len(audio_data)/RECORD_SAMPLE_RATE:.2f}s), "
                            f"voiced={voiced_duration_s:.2f}s, peak_rms={self.peak_rms:.6f}"
                        )
                        self._reset_utterance(now)

                        # Transcription is slow (faster-whisper inference); never run
                        # it on the PortAudio callback thread.
                        threading.Thread(
                            target=self._process_audio,
                            args=(audio_data,),
                            daemon=True,
                        ).start()
                    else:
                        # Not enough audio or no speech detected, reset silently
                        self._reset_utterance(now)
                        if self.current_phase == "speech-detected":
                            self._transition_to_phase("idle")
            else:
                # Above threshold - might be speech, but require sustained signal
                self.silence_start_time = None
                if self.current_phase in ("idle", "speech-detected"):
                    self._transition_to_phase("speech-detected")
                self.speech_confirm_count += 1
                self.voiced_frames += frames

                if not self.speech_detected and self.speech_confirm_count >= SPEECH_CONFIRM_CHUNKS:
                    # Confirmed speech - multiple consecutive chunks above threshold
                    log.info(f">>> VOICE CONFIRMED (RMS={rms:.6f} > threshold={threshold})")
                    self.speech_detected = True

    def _reset_utterance(self, now: float) -> None:
        """Clear per-utterance state. Caller must hold self.lock."""
        self.audio_buffer = []
        self.recording_start_time = now
        self.silence_start_time = None
        self.speech_detected = False
        self.speech_confirm_count = 0
        self.voiced_frames = 0
        self.peak_rms = 0.0

    def _process_audio(self, audio_data: np.ndarray) -> None:
        """Run whisper on a finished utterance (worker thread)."""
        duration_s = len(audio_data) / RECORD_SAMPLE_RATE
        log.info(f">>> SUBMITTING TO WHISPER: {len(audio_data)} samples ({duration_s:.2f}s of audio)")

        self._transition_to_phase("transcribing")

        try:
            text = transcribe_audio(audio_data)
            # Only the length is logged, never the words: what people say is private.
            log.info(f">>> WHISPER RETURNED: {len(text)} characters" if text else ">>> WHISPER RETURNED: (empty/None)")
            if text and text.strip():
                # The window drives the phase back to idle via
                # notify_typing_done() once the keystrokes have been sent.
                self._transition_to_phase("typing")
                self.speech_transcribed.emit(text)
            else:
                self._transition_to_phase("idle")
        except Exception as e:
            log.error(f"Transcription error: {e}", exc_info=True)
            self._transition_to_phase("idle")


# =============================================================================
# Threshold Calibration
# =============================================================================


class NoiseCalibrator(QObject):
    """Listens to the room for a few seconds and suggests a silence threshold.

    Opens its own short-lived stream with the recorder's exact format and
    block size, because the RMS of a block depends on its length: a threshold
    measured any other way would not mean the same thing to the recorder.
    """

    finished = pyqtSignal(float)  # the suggested threshold
    failed = pyqtSignal(str)  # a message to show the user

    def __init__(self, audio_device: str | None = None) -> None:
        super().__init__()
        self.audio_device = audio_device
        self.stream: Any = None
        # Appended to on the PortAudio thread and read only after the stream
        # has been stopped, which waits for the last callback; list.append is
        # atomic, so no lock.
        self.noise_rms: list[float] = []
        self._blocks_to_skip = int(CALIBRATION_SETTLE_S * RECORD_SAMPLE_RATE / BLOCK_SIZE)

    def start(self) -> tuple[bool, str | None]:
        """Start listening. Returns (success, error_message), like AudioRecorder."""
        device = None
        if self.audio_device:
            device = find_input_device(self.audio_device)
            if device is None:
                return (False, f"Microphone '{self.audio_device}' not found.")

        try:
            self.stream = sd.InputStream(
                samplerate=RECORD_SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=self._audio_callback,
                blocksize=BLOCK_SIZE,
                device=device,
            )
            self.stream.start()
        except Exception as e:
            log.error(f"Failed to open audio stream for calibration: {e}")
            self.stream = None
            return (False, f"Failed to open microphone.\n\nError: {e}")

        log.info(f"Calibrating silence threshold for {CALIBRATION_DURATION_S:.0f}s")
        QTimer.singleShot(int(CALIBRATION_DURATION_S * 1000), self._finish)
        return (True, None)

    def cancel(self) -> None:
        self._close_stream()

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if self._blocks_to_skip > 0:
            self._blocks_to_skip -= 1
            return
        self.noise_rms.append(block_rms(indata))

    def _close_stream(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def _finish(self) -> None:
        if self.stream is None:
            return  # cancelled
        self._close_stream()

        if not self.noise_rms:
            self.failed.emit("The microphone delivered no audio during calibration.")
            return

        threshold = threshold_from_noise(self.noise_rms)
        log.info(
            f"Calibration: {len(self.noise_rms)} blocks, "
            f"p{CALIBRATION_PERCENTILE}={np.percentile(self.noise_rms, CALIBRATION_PERCENTILE):.6f}, "
            f"max={max(self.noise_rms):.6f} -> threshold {threshold:.4f}"
        )
        self.finished.emit(threshold)
