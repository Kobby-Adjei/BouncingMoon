"""
audio_ear.py — Rhythmic thump detection for BouncingMoon.

Listens to the default microphone for low-frequency desk impacts,
counts pulses within a debounce window, and emits intent events:
  1 thump  → TRIGGER_CLICK
  2 thumps → TRIGGER_AUTOFILL
  3 thumps → TRIGGER_VOICE_COMMAND
"""

import threading
import time
from collections import deque
from enum import Enum, auto

import numpy as np
import sounddevice as sd


class TriggerEvent(Enum):
    TRIGGER_CLICK = auto()
    TRIGGER_AUTOFILL = auto()
    TRIGGER_VOICE_COMMAND = auto()


# ── Tuning constants ──────────────────────────────────────────────
SAMPLE_RATE = 44100
BLOCK_SIZE = 1024          # ~23 ms per block at 44.1 kHz
THRESHOLD = 0.15           # RMS amplitude to count as a thump (tune per mic)
DEBOUNCE_WINDOW = 0.6      # seconds to wait after last thump before deciding
COOLDOWN = 1.0             # seconds of silence required between trigger groups
LOW_FREQ_CUTOFF = 300      # Hz — ignore energy above this (filters out voice/shaking)


def _bandpass_energy(block: np.ndarray, sr: int, cutoff: int) -> float:
    """Return RMS energy only in the 20 Hz – cutoff Hz band."""
    fft = np.fft.rfft(block.flatten())
    freqs = np.fft.rfftfreq(len(block.flatten()), d=1.0 / sr)
    mask = (freqs >= 20) & (freqs <= cutoff)
    filtered = np.zeros_like(fft)
    filtered[mask] = fft[mask]
    signal = np.fft.irfft(filtered, n=len(block.flatten()))
    return float(np.sqrt(np.mean(signal ** 2)))


class AudioEar:
    """Continuously listens for desk thumps and fires callbacks."""

    def __init__(self, callback=None, threshold=THRESHOLD):
        self.callback = callback          # fn(TriggerEvent) called on detection
        self.threshold = threshold
        self._pulse_times: deque = deque(maxlen=10)
        self._last_trigger_time = 0.0
        self._running = False
        self._stream = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────

    def start(self):
        """Begin listening on the default mic (non-blocking)."""
        self._running = True
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=1,
            dtype="float32",
            callback=self._audio_callback,
        )
        self._stream.start()

    def stop(self):
        """Stop listening and clean up."""
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._timer:
            self._timer.cancel()

    # ── Internal ──────────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for every audio block."""
        if not self._running:
            return

        energy = _bandpass_energy(indata, SAMPLE_RATE, LOW_FREQ_CUTOFF)

        if energy >= self.threshold:
            now = time.monotonic()

            # Enforce cooldown so we don't re-trigger immediately
            if now - self._last_trigger_time < COOLDOWN:
                return

            with self._lock:
                # Ignore if this spike is too close to the previous one (same thump)
                if self._pulse_times and (now - self._pulse_times[-1]) < 0.15:
                    return
                self._pulse_times.append(now)

            # Reset the decision timer every time a new pulse arrives
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_WINDOW, self._decide)
            self._timer.start()

    def _decide(self):
        """Count pulses collected in the debounce window and fire the event."""
        with self._lock:
            now = time.monotonic()
            # Only count pulses within the last debounce window + a little slack
            recent = [t for t in self._pulse_times if now - t < DEBOUNCE_WINDOW + 0.2]
            count = len(recent)
            self._pulse_times.clear()

        if count == 0:
            return

        self._last_trigger_time = time.monotonic()

        if count == 1:
            event = TriggerEvent.TRIGGER_CLICK
        elif count == 2:
            event = TriggerEvent.TRIGGER_AUTOFILL
        else:
            event = TriggerEvent.TRIGGER_VOICE_COMMAND

        if self.callback:
            self.callback(event)


# ── Quick test ────────────────────────────────────────────────────
if __name__ == "__main__":
    def _on_trigger(event: TriggerEvent):
        labels = {
            TriggerEvent.TRIGGER_CLICK: "CLICK (1 thump)",
            TriggerEvent.TRIGGER_AUTOFILL: "AUTOFILL (2 thumps)",
            TriggerEvent.TRIGGER_VOICE_COMMAND: "VOICE COMMAND (3 thumps)",
        }
        print(f"  >> {labels[event]}")

    ear = AudioEar(callback=_on_trigger)
    print("Listening for desk thumps… (Ctrl+C to quit)")
    print(f"  Threshold: {THRESHOLD}  |  Debounce: {DEBOUNCE_WINDOW}s  |  Low-freq cutoff: {LOW_FREQ_CUTOFF} Hz")
    ear.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        ear.stop()
        print("\nStopped.")
