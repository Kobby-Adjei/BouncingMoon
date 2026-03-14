"""
voice_interface.py — Two-way voice interface for BouncingMoon.

Uses ElevenLabs for both:
  - STT (Speech-to-Text): Captures user voice commands via mic
  - TTS (Text-to-Speech): Speaks back confirmations and questions

Requires ELEVENLABS_API_KEY environment variable.
"""

import io
import os
import tempfile
import threading
import wave

import numpy as np
import sounddevice as sd
from elevenlabs import ElevenLabs
from elevenlabs import play as el_play


# ── Config ────────────────────────────────────────────────────────
STT_MODEL = "scribe_v1"
TTS_MODEL = "eleven_turbo_v2"
TTS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"   # "George" — calm, clear voice. Change as needed.
SAMPLE_RATE = 16000
RECORD_CHANNELS = 1
SILENCE_THRESHOLD = 0.02    # RMS below this = silence
SILENCE_DURATION = 1.5      # seconds of silence before we stop recording
MAX_RECORD_SECONDS = 15     # hard cap on recording length


class VoiceInterface:
    """Two-way voice I/O using ElevenLabs."""

    def __init__(self, voice_id: str = TTS_VOICE_ID):
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise RuntimeError("Set ELEVENLABS_API_KEY environment variable")
        self.client = ElevenLabs(api_key=api_key)
        self.voice_id = voice_id
        self._recording = False

    # ── Speech-to-Text ────────────────────────────────────────────

    def listen(self, max_seconds: float = MAX_RECORD_SECONDS) -> str:
        """
        Record from mic until silence is detected, then transcribe.
        Returns the transcribed text.
        """
        audio_data = self._record_until_silence(max_seconds)
        if audio_data is None or len(audio_data) == 0:
            return ""

        wav_bytes = self._to_wav_bytes(audio_data)

        result = self.client.speech_to_text.convert(
            file=wav_bytes,
            model_id=STT_MODEL,
            language_code="en",
        )

        return result.text.strip() if hasattr(result, "text") else str(result).strip()

    def _record_until_silence(self, max_seconds: float) -> np.ndarray | None:
        """Record audio, stopping after sustained silence."""
        frames = []
        silence_frames = 0
        frames_for_silence = int(SILENCE_DURATION * SAMPLE_RATE / 1024)
        max_frames = int(max_seconds * SAMPLE_RATE / 1024)
        got_speech = False

        def callback(indata, frame_count, time_info, status):
            nonlocal silence_frames, got_speech
            frames.append(indata.copy())
            rms = float(np.sqrt(np.mean(indata ** 2)))
            if rms > SILENCE_THRESHOLD:
                silence_frames = 0
                got_speech = True
            else:
                silence_frames += 1

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=1024,
            channels=RECORD_CHANNELS,
            dtype="float32",
            callback=callback,
        )

        with stream:
            while True:
                sd.sleep(50)
                if len(frames) >= max_frames:
                    break
                # Only stop on silence if we've already heard speech
                if got_speech and silence_frames >= frames_for_silence:
                    break

        if not frames:
            return None
        return np.concatenate(frames)

    def _to_wav_bytes(self, audio: np.ndarray) -> io.BytesIO:
        """Convert float32 numpy audio to WAV bytes in memory."""
        pcm = (audio * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(RECORD_CHANNELS)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())
        buf.seek(0)
        buf.name = "recording.wav"  # ElevenLabs SDK needs a .name attribute
        return buf

    # ── Text-to-Speech ────────────────────────────────────────────

    def speak(self, text: str):
        """Convert text to speech and play it immediately."""
        audio = self.client.text_to_speech.convert(
            text=text,
            voice_id=self.voice_id,
            model_id=TTS_MODEL,
            output_format="mp3_22050_32",
        )
        el_play(audio)

    def speak_async(self, text: str):
        """Speak in a background thread so it doesn't block."""
        t = threading.Thread(target=self.speak, args=(text,), daemon=True)
        t.start()

    # ── Convenience ───────────────────────────────────────────────

    def ask(self, question: str) -> str:
        """Speak a question, then listen for the answer."""
        self.speak(question)
        return self.listen()

    def confirm(self, message: str):
        """Speak a confirmation message (non-blocking)."""
        self.speak_async(message)


# ── Quick test ────────────────────────────────────────────────────
if __name__ == "__main__":
    voice = VoiceInterface()

    print("Speaking a greeting…")
    voice.speak("Hello! I'm BouncingMoon. Thump your desk and tell me what you need.")

    print("Listening for your command… (speak, then pause)")
    text = voice.listen()
    print(f"You said: {text}")

    voice.speak(f"I heard you say: {text}")
