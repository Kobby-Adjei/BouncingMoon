"""
voice_interface.py — Two-way voice interface for BouncingMoon.

Uses ElevenLabs for both:
  - STT (Speech-to-Text): Captures user voice commands via mic
  - TTS (Text-to-Speech): Speaks back confirmations and questions

Supports two modes:
  - On-demand: listen() for a single command (triggered by thumps)
  - Always-listening: continuous background loop that detects a wake
    word ("moon" / "hey moon") and routes commands to a callback

Requires ELEVENLABS_API_KEY environment variable.
"""

import io
import os
import threading
import time
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

# Wake words that activate voice control
WAKE_WORDS = {"moon", "hey moon", "bouncing moon", "hey bouncing moon"}

# Duration of each chunk in always-listening mode (seconds)
LISTEN_CHUNK_DURATION = 3.0


class VoiceCommand:
    """Parsed voice command with intent classification."""

    # Direct actions that don't need vision
    DIRECT_ACTIONS = {
        "scroll up":    {"action": "scroll", "direction": "up"},
        "scroll down":  {"action": "scroll", "direction": "down"},
        "go back":      {"action": "go_back"},
        "go forward":   {"action": "go_forward"},
        "stop":         {"action": "stop"},
        "cancel":       {"action": "stop"},
        "undo":         {"action": "undo"},
        "read the screen": {"action": "read_screen"},
        "read screen":  {"action": "read_screen"},
        "what do you see": {"action": "read_screen"},
        "describe the screen": {"action": "read_screen"},
        "take a screenshot": {"action": "read_screen"},
        "open":         {"action": "open_app"},
        "switch to":    {"action": "open_app"},
        "press enter":  {"action": "key_press", "key": "enter"},
        "press escape": {"action": "key_press", "key": "escape"},
        "press tab":    {"action": "key_press", "key": "tab"},
        "close this":   {"action": "key_press", "key": "command+w"},
        "close window": {"action": "key_press", "key": "command+w"},
        "new tab":      {"action": "key_press", "key": "command+t"},
        "copy":         {"action": "key_press", "key": "command+c"},
        "paste":        {"action": "key_press", "key": "command+v"},
        "select all":   {"action": "key_press", "key": "command+a"},
    }

    def __init__(self, raw_text: str):
        self.raw_text = raw_text
        self.text = raw_text.lower().strip()
        self.intent = self._classify()

    def _classify(self) -> dict:
        """Classify the command into an intent."""
        # Check direct actions first (exact or prefix match)
        for trigger, intent in self.DIRECT_ACTIONS.items():
            if self.text == trigger or self.text.startswith(trigger + " "):
                result = dict(intent)
                # Extract the argument (e.g., "open Safari" → app_name = "Safari")
                remainder = self.text[len(trigger):].strip()
                if remainder:
                    result["argument"] = remainder
                return result

        # If it mentions clicking something specific, it's a vision-assisted action
        if any(word in self.text for word in ["click", "tap", "press", "select", "hit"]):
            return {"action": "vision_click", "description": self.raw_text}

        # If it mentions typing/writing
        if any(word in self.text for word in ["type", "write", "enter text"]):
            return {"action": "vision_type", "description": self.raw_text}

        # If it mentions sending/messaging — errand action for NanoClaw
        if any(word in self.text for word in ["send", "message", "text", "email", "tell"]):
            return {"action": "errand", "description": self.raw_text}

        # Default: treat as a general command that needs vision
        return {"action": "general_command", "description": self.raw_text}

    @property
    def is_direct(self) -> bool:
        """True if this command can be executed without AI vision."""
        return self.intent.get("action") in {
            "scroll", "go_back", "go_forward", "stop", "undo",
            "key_press", "open_app",
        }

    @property
    def is_errand(self) -> bool:
        """True if this should be routed to NanoClaw."""
        return self.intent.get("action") == "errand"

    @property
    def needs_vision(self) -> bool:
        """True if this command needs a screenshot to execute."""
        return self.intent.get("action") in {
            "vision_click", "vision_type", "general_command", "read_screen",
        }


class VoiceInterface:
    """Two-way voice I/O using ElevenLabs with always-listening support."""

    def __init__(self, voice_id: str = TTS_VOICE_ID):
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise RuntimeError("Set ELEVENLABS_API_KEY environment variable")
        self.client = ElevenLabs(api_key=api_key)
        self.voice_id = voice_id
        self._recording = False
        self._always_listening = False
        self._listen_thread = None
        self._paused = False        # Pause listening (e.g., while speaking)
        self.on_command = None       # Callback: fn(VoiceCommand)

    # ── Speech-to-Text (on-demand) ────────────────────────────────

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
                if got_speech and silence_frames >= frames_for_silence:
                    break

        if not frames:
            return None
        return np.concatenate(frames)

    # ── Always-Listening Mode ─────────────────────────────────────

    def start_listening(self, on_command=None):
        """
        Start always-listening mode in background.
        Continuously records short chunks, transcribes them, and checks
        for wake words. When a wake word is detected, records the full
        command and fires the callback.
        """
        self.on_command = on_command
        self._always_listening = True
        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True
        )
        self._listen_thread.start()

    def stop_listening(self):
        """Stop always-listening mode."""
        self._always_listening = False

    def pause_listening(self):
        """Temporarily pause (e.g., while TTS is playing to avoid echo)."""
        self._paused = True

    def resume_listening(self):
        """Resume after pause."""
        self._paused = False

    def _listen_loop(self):
        """Background loop: record chunks, check for wake words."""
        while self._always_listening:
            if self._paused:
                time.sleep(0.2)
                continue

            # Record a short chunk
            audio = self._record_chunk(LISTEN_CHUNK_DURATION)
            if audio is None:
                continue

            # Check if there was meaningful audio (not just silence)
            rms = float(np.sqrt(np.mean(audio ** 2)))
            if rms < SILENCE_THRESHOLD:
                continue

            # Transcribe the chunk
            wav_bytes = self._to_wav_bytes(audio)
            try:
                result = self.client.speech_to_text.convert(
                    file=wav_bytes,
                    model_id=STT_MODEL,
                    language_code="en",
                )
                text = result.text.strip() if hasattr(result, "text") else str(result).strip()
            except Exception:
                continue

            if not text:
                continue

            text_lower = text.lower()

            # Check for wake word
            has_wake_word = any(wake in text_lower for wake in WAKE_WORDS)

            if not has_wake_word:
                continue

            # Strip the wake word to get the command
            command_text = text_lower
            for wake in sorted(WAKE_WORDS, key=len, reverse=True):
                if wake in command_text:
                    command_text = command_text.replace(wake, "", 1).strip()
                    break

            if command_text:
                # Got a command in the same breath: "hey moon scroll down"
                cmd = VoiceCommand(command_text)
                if self.on_command:
                    self.on_command(cmd)
            else:
                # Just the wake word — listen for the actual command
                self.pause_listening()
                self.speak("Yes?")
                full_command = self.listen()
                self.resume_listening()
                if full_command:
                    cmd = VoiceCommand(full_command)
                    if self.on_command:
                        self.on_command(cmd)

    def _record_chunk(self, duration: float) -> np.ndarray | None:
        """Record a fixed-length audio chunk."""
        frames = []

        def callback(indata, frame_count, time_info, status):
            frames.append(indata.copy())

        total_frames = int(duration * SAMPLE_RATE / 1024)

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=1024,
            channels=RECORD_CHANNELS,
            dtype="float32",
            callback=callback,
        )

        with stream:
            while len(frames) < total_frames:
                sd.sleep(50)
                if not self._always_listening:
                    return None

        if not frames:
            return None
        return np.concatenate(frames)

    # ── Helpers ───────────────────────────────────────────────────

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
        buf.name = "recording.wav"
        return buf

    # ── Text-to-Speech ────────────────────────────────────────────

    def speak(self, text: str):
        """Convert text to speech and play it immediately."""
        was_listening = not self._paused
        if was_listening:
            self.pause_listening()
        audio = self.client.text_to_speech.convert(
            text=text,
            voice_id=self.voice_id,
            model_id=TTS_MODEL,
            output_format="mp3_22050_32",
        )
        el_play(audio)
        if was_listening:
            self.resume_listening()

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
    def _on_command(cmd: VoiceCommand):
        print(f"\n  Command: '{cmd.raw_text}'")
        print(f"  Intent:  {cmd.intent}")
        print(f"  Direct:  {cmd.is_direct}  |  Vision: {cmd.needs_vision}  |  Errand: {cmd.is_errand}")

    voice = VoiceInterface()

    print("=== BouncingMoon Voice Interface ===")
    print("Say 'Hey Moon' followed by a command.")
    print("Examples:")
    print("  'Hey Moon, scroll down'")
    print("  'Hey Moon, click the blue button'")
    print("  'Hey Moon, read the screen'")
    print("  'Hey Moon, send Mom a message'")
    print()

    voice.start_listening(on_command=_on_command)

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        voice.stop_listening()
        print("\nStopped.")
