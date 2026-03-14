# BouncingMoon

An accessibility tool that helps people with Parkinson's disease and other motor-control impairments operate their computer without precise manual input. Instead of forcing users to control a mouse and keyboard, BouncingMoon lets them express intent through desk thumps and voice commands while an AI agent handles the precision work.

## The Problem

Modern computers assume precise physical input: accurate cursor movement, small target clicks, fast typing, clean menu navigation. For people with Parkinson's, essential tremor, cerebral palsy, MS, or other motor impairments, these actions are exhausting, slow, or impossible.

The barrier is not cognition. The barrier is execution.

## How It Works

BouncingMoon replaces fine-motor input with **Rhythmic Intent**. Users thump their desk to trigger actions, and speak commands for complex tasks. An AI agent sees the screen, understands the context, and performs the action on their behalf.

### Input Modalities

| Input | Trigger | What Happens |
|-------|---------|--------------|
| **1 thump** | `TRIGGER_CLICK` | AI identifies the primary button/link on screen and clicks it |
| **2 thumps** | `TRIGGER_AUTOFILL` | AI detects form fields and fills them from the user profile |
| **3 thumps** | `TRIGGER_VOICE_COMMAND` | System listens for a spoken command, then executes it |

### The Loop

```
Thump → HUD activates → AI sees screen → Action executed → Voice confirms "Done"
```

1. User thumps their desk
2. The floating Moon HUD switches to "Thinking" state
3. A screenshot is sent to Claude (Anthropic) for visual understanding
4. The AI returns coordinates and intent
5. PyAutoGUI performs the click/type/scroll action
6. ElevenLabs speaks "Task complete"
7. NanoClaw sends errands (WhatsApp, email, Telegram) if needed

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Thump Detection | `sounddevice` + `numpy` | Low-frequency acoustic impact detection |
| Voice I/O | `ElevenLabs` (STT + TTS) | Two-way voice interface |
| Screen Understanding | `Anthropic Claude` (Vision API) | Screenshot analysis, UI element identification |
| Visual Feedback | `CustomTkinter` | Floating transparent HUD overlay |
| System Automation | `PyAutoGUI` + `pyperclip` | Mouse/keyboard control, clipboard-based typing |
| Agent Errands | `NanoClaw` | WhatsApp, Telegram, Gmail messaging on behalf of the user |
| Confirmation | `AWS SES` | Email receipts for completed actions |

## Project Structure

```
BouncingMoon/
├── main.py              # Orchestrator — the "Ghost" service
├── audio_ear.py         # Microphone listener, thump detection, rhythm counting
├── overlay_hud.py       # Floating moon HUD with animated states
├── vision_agent.py      # Screenshot capture + Claude vision analysis
├── voice_interface.py   # ElevenLabs STT (listen) + TTS (speak)
├── user_profile.json    # User data for form autofill
└── requirements.txt     # Python dependencies
```

## Setup

### Prerequisites

- Python 3.10+
- macOS (primary target) — requires granting permissions (see below)
- A working microphone
- API keys for Anthropic and ElevenLabs

### Installation

```bash
git clone https://github.com/Kobby-Adjei/BouncingMoon.git
cd BouncingMoon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file or export directly:

```bash
export ANTHROPIC_API_KEY="your-anthropic-api-key"
export ELEVENLABS_API_KEY="your-elevenlabs-api-key"
```

### macOS Permissions

On first run, macOS will prompt you to grant these permissions in **System Settings > Privacy & Security**:

- **Microphone** — for thump detection and voice commands
- **Screen Recording** — for taking screenshots (vision agent)
- **Accessibility** — for PyAutoGUI to control mouse and keyboard

You must grant all three for the full system to work.

### Running

```bash
source .venv/bin/activate
python main.py
```

The Moon HUD will appear in the top-left corner. Thump your desk to interact.

## Module Guide

Each module can be tested independently. Every file has a `if __name__ == "__main__"` block for standalone testing.

### audio_ear.py — The Ear

Listens to the default microphone for low-frequency desk impacts. Uses band-pass filtering (20–300 Hz) to isolate physical thumps from voice, music, and hand tremor vibrations.

```bash
python audio_ear.py
# Thump your desk and watch the terminal for detected events
```

**Key constants to tune:**

| Constant | Default | What It Does |
|----------|---------|--------------|
| `THRESHOLD` | `0.15` | RMS amplitude to count as a thump. Lower = more sensitive |
| `DEBOUNCE_WINDOW` | `0.6s` | Time to wait after last thump before deciding pulse count |
| `COOLDOWN` | `1.0s` | Silence required between trigger groups |
| `LOW_FREQ_CUTOFF` | `300 Hz` | Ignore energy above this frequency |

### overlay_hud.py — The HUD

A 150x150 floating circular overlay that gives visual feedback. Always on top, draggable, transparent background.

```bash
python overlay_hud.py
# Cycles through all 5 states automatically for preview
```

**States:**

| State | Visual | When |
|-------|--------|------|
| `IDLE` | Dim moon with subtle ring | Waiting for input |
| `ACTIVE` | Pulsing purple glow | Thump detected |
| `THINKING` | Orbiting blue dots | AI is processing |
| `LISTENING` | Pulsing blue ring + mic icon | Waiting for voice command |
| `SUCCESS` | Green ring + checkmark | Action completed |

### vision_agent.py — The Eyes

Takes a screenshot, sends it to Claude with a structured prompt, and returns JSON with identified UI elements and pixel coordinates. No screenshots are saved to disk.

```bash
export ANTHROPIC_API_KEY="your-key"
python vision_agent.py
# Takes a screenshot of your current screen and analyzes it
```

**Three analysis modes:**

- `click` — Find the primary actionable element (button, link)
- `autofill` — Identify all form input fields
- `command` — Interpret a free-form voice command against the current screen

**Important for Retina displays:** If coordinates are consistently offset, the screen capture may be at 2x resolution while PyAutoGUI uses logical pixels. Divide x and y by 2.

### voice_interface.py — The Voice

Two-way voice interface using ElevenLabs. Speaks to the user (TTS) and listens for commands (STT). Automatically detects when the user stops speaking.

```bash
export ELEVENLABS_API_KEY="your-key"
python voice_interface.py
# Speaks a greeting, listens for your response, repeats it back
```

**Key methods:**

- `voice.listen()` — Record until silence, return transcribed text
- `voice.speak(text)` — Convert text to speech and play (blocking)
- `voice.speak_async(text)` — Same but non-blocking
- `voice.ask(question)` — Speak a question, then listen for answer

### main.py — The Ghost

Wires everything together. Boots the HUD, starts the audio listener, routes events through the pipeline.

```bash
export ANTHROPIC_API_KEY="your-key"
export ELEVENLABS_API_KEY="your-key"
python main.py
```

**Safety features:**

- `pyautogui.FAILSAFE = True` — move mouse to any screen corner to abort
- Low-confidence actions trigger a voice confirmation before executing
- Ambiguous screens (multiple buttons) prompt the user to clarify by voice

## User Profile

Edit `user_profile.json` with real data for form autofill:

```json
{
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1-555-123-4567",
    "address": "456 Oak Avenue, Portland, OR 97201",
    "date_of_birth": "1958-03-15"
}
```

The autofill system maps these fields to detected form inputs by type and label matching.

## Team Development

The project is split into three ownership areas for parallel development:

### Person 1 — The Senses (`audio_ear.py` + `voice_interface.py`)

- Tune thump detection thresholds on real hardware
- Fix mic contention between thump listener and voice recording
- Adjust silence detection for slower speech patterns
- Add audio feedback (chime) when system starts listening

### Person 2 — The Eyes (`vision_agent.py` + `overlay_hud.py`)

- Validate Claude's coordinate accuracy on real screens
- Fix Retina/HiDPI scaling for screenshots vs. logical pixels
- Add action preview (flash indicator at target coordinates before clicking)
- Polish HUD visuals and animations

### Person 3 — The Brain (`main.py` + NanoClaw + AWS SES)

- Set up NanoClaw for messaging errands (WhatsApp, Telegram, Gmail)
- Build command routing (screen actions vs. errand actions)
- Add AWS SES email confirmations
- Own the demo flow

## Design Principles

- **No screenshots saved to disk.** All screen captures are processed in memory and discarded. Privacy first.
- **Confirm before risky actions.** If the AI confidence is below 80%, it asks the user via voice before acting.
- **Thump-to-action under 3 seconds.** The interaction must feel responsive to avoid user anxiety.
- **Fail safe.** Move the mouse to any screen corner to kill all automation immediately.

## Who This Is For

**Primary users:** People with Parkinson's disease, especially those affected by tremor, rigidity, bradykinesia, or freezing.

**Secondary users:** People with essential tremor, cerebral palsy, multiple sclerosis, stroke survivors with limited hand control, people with arthritis, and anyone with temporary motor impairments.

BouncingMoon is not just a thump detector. It is an assistive operating layer that translates human intent into safe system actions.

## License

MIT
