"""
main.py — The "Ghost" orchestrator for BouncingMoon.

Boots the HUD, starts the audio listener AND always-listening voice mode,
and routes events through the full loop:
  - Thump → vision/voice → action → confirmation
  - Voice ("Hey Moon, ...") → classify → execute → confirmation
"""

import json
import os
import subprocess
import threading
import time

import pyautogui
import pyperclip

from audio_ear import AudioEar, TriggerEvent
from overlay_hud import OverlayHUD, HUDState
from vision_agent import analyze_screen, is_confident
from voice_interface import VoiceCommand, VoiceInterface


# ── Safety: prevent pyautogui from going haywire ──────────────────
pyautogui.FAILSAFE = True      # Move mouse to corner to abort
pyautogui.PAUSE = 0.3          # Small delay between actions

# ── User profile for autofill ────────────────────────────────────
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "user_profile.json")


def load_profile() -> dict:
    """Load user profile data for form filling."""
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, "r") as f:
            return json.load(f)
    return {}


class BouncingMoon:
    """Main orchestrator — the Ghost service."""

    def __init__(self):
        self.hud = OverlayHUD()
        self.ear = AudioEar(callback=self._on_trigger)
        self.voice = VoiceInterface()
        self.profile = load_profile()
        self._busy = False

    def start(self):
        """Boot everything up."""
        print("🌕 BouncingMoon starting…")

        # Start the HUD (runs in its own thread)
        self.hud.start()
        self.hud.set_state(HUDState.IDLE)

        # Start listening for thumps
        self.ear.start()

        # Start always-listening voice mode
        self.voice.start_listening(on_command=self._on_voice_command)

        # Greet the user
        self.voice.speak_async("BouncingMoon is ready. Thump your desk or say Hey Moon.")
        print("   Listening for desk thumps and voice… (Ctrl+C to quit)")
        print("   1 thump = click  |  2 thumps = autofill  |  3 thumps = voice command")
        print('   Or say "Hey Moon" followed by any command')

        # Keep main thread alive
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        """Clean up everything."""
        print("\n🌑 Shutting down…")
        self.ear.stop()
        self.voice.stop_listening()
        self.hud.stop()

    # ── Thump event routing ───────────────────────────────────────

    def _on_trigger(self, event: TriggerEvent):
        """Called by AudioEar when a thump pattern is detected."""
        if self._busy:
            return
        self._busy = True

        t = threading.Thread(
            target=self._handle_event,
            args=(event,),
            daemon=True,
        )
        t.start()

    def _handle_event(self, event: TriggerEvent):
        """Process a trigger event through the full pipeline."""
        try:
            if event == TriggerEvent.TRIGGER_CLICK:
                self._handle_click()
            elif event == TriggerEvent.TRIGGER_AUTOFILL:
                self._handle_autofill()
            elif event == TriggerEvent.TRIGGER_VOICE_COMMAND:
                self._handle_voice_command_thump()
        except Exception as e:
            print(f"   ✗ Error: {e}")
            self.hud.set_state(HUDState.IDLE)
            self.voice.speak_async("Something went wrong. Please try again.")
        finally:
            self._busy = False

    # ── Voice command routing ─────────────────────────────────────

    def _on_voice_command(self, cmd: VoiceCommand):
        """Called by VoiceInterface when a wake-word command is detected."""
        if self._busy:
            self.voice.speak_async("I'm still working on something. One moment.")
            return
        self._busy = True

        t = threading.Thread(
            target=self._route_voice_command,
            args=(cmd,),
            daemon=True,
        )
        t.start()

    def _route_voice_command(self, cmd: VoiceCommand):
        """Route a parsed voice command to the right handler."""
        try:
            print(f"   🎤 Voice: '{cmd.raw_text}' → {cmd.intent}")

            if cmd.intent.get("action") == "stop":
                self._handle_stop()
            elif cmd.is_direct:
                self._handle_direct_action(cmd)
            elif cmd.intent.get("action") == "read_screen":
                self._handle_read_screen()
            elif cmd.is_errand:
                self._handle_errand(cmd)
            elif cmd.needs_vision:
                self._handle_vision_command(cmd)
            else:
                self._handle_vision_command(cmd)
        except Exception as e:
            print(f"   ✗ Error: {e}")
            self.hud.set_state(HUDState.IDLE)
            self.voice.speak_async("Something went wrong. Please try again.")
        finally:
            self._busy = False

    # ── Direct actions (no AI needed) ─────────────────────────────

    def _handle_direct_action(self, cmd: VoiceCommand):
        """Execute actions that don't require screen analysis."""
        action = cmd.intent.get("action")
        self.hud.set_state(HUDState.ACTIVE, "On it!")

        if action == "scroll":
            direction = cmd.intent.get("direction", "down")
            amount = 5 if direction == "up" else -5
            pyautogui.scroll(amount)
            self.voice.speak_async(f"Scrolled {direction}.")

        elif action == "go_back":
            pyautogui.hotkey("command", "[")
            self.voice.speak_async("Went back.")

        elif action == "go_forward":
            pyautogui.hotkey("command", "]")
            self.voice.speak_async("Went forward.")

        elif action == "undo":
            pyautogui.hotkey("command", "z")
            self.voice.speak_async("Undone.")

        elif action == "key_press":
            key = cmd.intent.get("key", "")
            if "+" in key:
                keys = key.split("+")
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(key)
            self.voice.speak_async("Done.")

        elif action == "open_app":
            app_name = cmd.intent.get("argument", "")
            if app_name:
                subprocess.Popen(["open", "-a", app_name])
                self.voice.speak_async(f"Opening {app_name}.")
            else:
                self.voice.speak_async("What app should I open?")

        self.hud.set_state(HUDState.SUCCESS, "Done!")
        time.sleep(1.0)
        self.hud.set_state(HUDState.IDLE)

    def _handle_stop(self):
        """Stop/cancel whatever is happening."""
        self.hud.set_state(HUDState.IDLE)
        self.voice.speak_async("Stopped.")
        print("   ⏹ Stopped by voice command")

    # ── Read screen (describe what's visible) ─────────────────────

    def _handle_read_screen(self):
        """Take a screenshot and describe the screen contents to the user."""
        self.hud.set_state(HUDState.THINKING, "Reading…")

        result = analyze_screen(
            intent="command",
            command="Describe what you see on this screen in 2-3 short sentences. "
                    "Focus on the main content and any actionable elements. "
                    "Speak as if talking to someone who cannot see the screen clearly."
        )

        description = result.get("description", result.get("element", ""))
        if not description:
            # If the structured response doesn't have a description, use raw
            description = str(result)

        self.hud.set_state(HUDState.SUCCESS, "Read!")
        self.voice.speak(description)
        time.sleep(0.5)
        self.hud.set_state(HUDState.IDLE)

    # ── Vision-assisted voice commands ────────────────────────────

    def _handle_vision_command(self, cmd: VoiceCommand):
        """Voice command that needs a screenshot to execute."""
        self.hud.set_state(HUDState.THINKING, "Looking…")

        result = analyze_screen(intent="command", command=cmd.raw_text)
        print(f"   AI result: {result}")

        if not is_confident(result):
            desc = result.get("element", "this action")
            answer = self.voice.ask(
                f"I want to {result.get('action', 'do something')} on {desc}. Should I?"
            )
            if not self._is_yes(answer):
                self.voice.speak_async("Cancelled.")
                self.hud.set_state(HUDState.IDLE)
                return

        action = result.get("action", "click")
        x, y = result.get("x", 0), result.get("y", 0)

        if action == "click" and x and y:
            pyautogui.click(x, y)
        elif action == "type" and x and y:
            pyautogui.click(x, y)
            time.sleep(0.2)
            value = result.get("value", "")
            if value:
                pyperclip.copy(value)
                pyautogui.hotkey("command", "v")
        elif action == "scroll":
            pyautogui.scroll(result.get("value", -3))
        elif action == "open":
            app_name = result.get("element", "")
            if app_name:
                subprocess.Popen(["open", "-a", app_name])

        self.hud.set_state(HUDState.SUCCESS, "Done!")
        self.voice.speak_async("Task complete.")
        print(f"   ✓ Executed: {action}")

        time.sleep(1.5)
        self.hud.set_state(HUDState.IDLE)

    # ── Errand commands (NanoClaw) ────────────────────────────────

    def _handle_errand(self, cmd: VoiceCommand):
        """Commands that should be routed to NanoClaw for messaging."""
        self.hud.set_state(HUDState.THINKING, "Sending…")
        # TODO: Wire up NanoClaw here
        # For now, acknowledge and log
        print(f"   📨 Errand request: {cmd.raw_text}")
        self.voice.speak_async(
            f"I'd send that for you, but NanoClaw isn't connected yet. "
            f"Your request was: {cmd.raw_text}"
        )
        self.hud.set_state(HUDState.IDLE)

    # ── Thump handlers ────────────────────────────────────────────

    def _handle_click(self):
        """Single thump: identify and click the primary element."""
        print("   → CLICK triggered")
        self.hud.set_state(HUDState.ACTIVE, "Thump!")
        time.sleep(0.3)
        self.hud.set_state(HUDState.THINKING, "Looking…")

        result = analyze_screen(intent="click")
        print(f"   AI result: {result}")

        if result.get("ambiguous"):
            options = result.get("options", [])
            names = [o.get("element", "unknown") for o in options]
            question = f"I see {len(options)} options: {', '.join(names)}. Which one?"
            self.hud.set_state(HUDState.LISTENING, "Which one?")
            answer = self.voice.ask(question)
            chosen = self._match_option(answer, options)
            if chosen:
                result = chosen
            else:
                self.voice.speak_async("I didn't understand. Let's try again.")
                self.hud.set_state(HUDState.IDLE)
                return

        if not is_confident(result):
            self.hud.set_state(HUDState.LISTENING, "Confirm?")
            element_name = result.get("element", "this element")
            answer = self.voice.ask(f"I think you want to click {element_name}. Should I?")
            if not self._is_yes(answer):
                self.voice.speak_async("Okay, cancelled.")
                self.hud.set_state(HUDState.IDLE)
                return

        x, y = result.get("x", 0), result.get("y", 0)
        if x and y:
            pyautogui.click(x, y)
            self.hud.set_state(HUDState.SUCCESS, "Clicked!")
            self.voice.speak_async("Done.")
            print(f"   ✓ Clicked {result.get('element')} at ({x}, {y})")
        else:
            self.voice.speak_async("I couldn't find where to click.")
            self.hud.set_state(HUDState.IDLE)
            return

        time.sleep(1.5)
        self.hud.set_state(HUDState.IDLE)

    def _handle_autofill(self):
        """Double thump: detect form fields and fill them."""
        print("   → AUTOFILL triggered")
        self.hud.set_state(HUDState.ACTIVE, "Autofill!")
        time.sleep(0.3)
        self.hud.set_state(HUDState.THINKING, "Scanning…")

        if not self.profile:
            self.voice.speak_async("No user profile found. Please set up your profile first.")
            self.hud.set_state(HUDState.IDLE)
            return

        result = analyze_screen(intent="autofill")
        print(f"   AI result: {result}")

        fields = result.get("fields", [])
        if not fields:
            self.voice.speak_async("I don't see any form fields on this screen.")
            self.hud.set_state(HUDState.IDLE)
            return

        filled = 0
        for field in fields:
            field_type = field.get("type", "text")
            label = field.get("label", "").lower()
            value = self._match_profile_field(field_type, label)
            if value:
                x, y = field.get("x", 0), field.get("y", 0)
                if x and y:
                    pyautogui.click(x, y)
                    time.sleep(0.2)
                    pyperclip.copy(value)
                    pyautogui.hotkey("command", "v")
                    time.sleep(0.1)
                    pyautogui.press("tab")
                    filled += 1

        if filled > 0:
            self.hud.set_state(HUDState.SUCCESS, f"Filled {filled}")
            self.voice.speak_async(f"Filled {filled} fields.")
            print(f"   ✓ Filled {filled} fields")
        else:
            self.voice.speak_async("I found fields but couldn't match them to your profile.")

        time.sleep(1.5)
        self.hud.set_state(HUDState.IDLE)

    def _handle_voice_command_thump(self):
        """Triple thump: listen for a voice command and route it."""
        print("   → VOICE COMMAND triggered (thump)")
        self.hud.set_state(HUDState.LISTENING, "Speak…")
        self.voice.speak("What would you like me to do?")

        command_text = self.voice.listen()
        if not command_text:
            self.voice.speak_async("I didn't hear anything.")
            self.hud.set_state(HUDState.IDLE)
            return

        print(f"   Heard: '{command_text}'")
        cmd = VoiceCommand(command_text)
        # Re-route through the same voice command handler
        self._route_voice_command(cmd)

    # ── Helpers ───────────────────────────────────────────────────

    def _match_profile_field(self, field_type: str, label: str) -> str | None:
        """Try to match a form field to a value in the user profile."""
        profile = self.profile
        type_map = {
            "name": profile.get("name"),
            "email": profile.get("email"),
            "phone": profile.get("phone"),
            "address": profile.get("address"),
            "date": profile.get("date_of_birth"),
        }
        if field_type in type_map and type_map[field_type]:
            return type_map[field_type]
        for key, value in profile.items():
            if key.lower() in label or label in key.lower():
                return str(value)
        return None

    def _match_option(self, answer: str, options: list[dict]) -> dict | None:
        """Try to match a spoken answer to one of the ambiguous options."""
        answer_lower = answer.lower()
        for opt in options:
            name = opt.get("element", "").lower()
            if name in answer_lower or answer_lower in name:
                return opt
        return None

    def _is_yes(self, text: str) -> bool:
        """Check if a spoken response is affirmative."""
        yes_words = {"yes", "yeah", "yep", "sure", "okay", "ok", "go", "do it", "please", "correct"}
        return any(w in text.lower() for w in yes_words)


# ── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    moon = BouncingMoon()
    moon.start()
