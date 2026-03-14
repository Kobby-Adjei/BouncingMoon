# BouncingMoon

An accessibility tool that helps people with Parkinson's disease and other motor-control impairments operate their computer without precise manual input. Instead of forcing users to control a mouse and keyboard, BouncingMoon lets them express intent through desk thumps and voice commands while an AI agent handles the precision work.

## The Problem

Modern computers assume precise physical input: accurate cursor movement, small target clicks, fast typing, clean menu navigation. For people with Parkinson's, essential tremor, cerebral palsy, MS, or other motor impairments, these actions are exhausting, slow, or impossible.

The barrier is not cognition. The barrier is execution.

## How It Works

BouncingMoon replaces fine-motor input with **Rhythmic Intent**. Users thump their desk to trigger actions, and speak commands for complex tasks. An AI agent sees the screen, understands the context, and performs the action on their behalf.

### Input Modalities

Users have two parallel ways to interact — thumps and voice. Both are always active.

#### Desk Thumps

| Input | Trigger | What Happens |
|-------|---------|--------------|
| **1 thump** | `TRIGGER_CLICK` | AI identifies the primary button/link on screen and clicks it |
| **2 thumps** | `TRIGGER_AUTOFILL` | AI detects form fields and fills them from the user profile |
| **3 thumps** | `TRIGGER_VOICE_COMMAND` | System listens for a spoken command, then executes it |

#### Voice Commands (Always Listening)

Say **"Hey Moon"** followed by a command. No thump needed. Voice works alongside thumps at all times.

**Direct commands** (instant, no AI vision needed):

| Command | What Happens |
|---------|--------------|
| "Hey Moon, scroll down" | Scrolls the page down |
| "Hey Moon, scroll up" | Scrolls the page up |
| "Hey Moon, go back" | Browser/app back navigation |
| "Hey Moon, go forward" | Browser/app forward navigation |
| "Hey Moon, undo" | Undo last action (Cmd+Z) |
| "Hey Moon, press enter" | Presses Enter key |
| "Hey Moon, press escape" | Presses Escape key |
| "Hey Moon, close this" | Closes current window/tab |
| "Hey Moon, new tab" | Opens a new browser tab |
| "Hey Moon, copy" / "paste" / "select all" | Clipboard operations |
| "Hey Moon, open Safari" | Opens an application by name |
| "Hey Moon, stop" | Cancels whatever is happening |

**Vision commands** (AI looks at the screen first):

| Command | What Happens |
|---------|--------------|
| "Hey Moon, click the blue button" | AI finds the element and clicks it |
| "Hey Moon, type hello in the search box" | AI finds the field and types |
| "Hey Moon, read the screen" | AI describes what's on screen via voice |
| "Hey Moon, what do you see" | Same as read the screen |

**Errand commands** (routed to NanoClaw):

| Command | What Happens |
|---------|--------------|
| "Hey Moon, send Mom a WhatsApp message" | NanoClaw sends the message |
| "Hey Moon, email my doctor" | NanoClaw drafts and sends email |
| "Hey Moon, text my daughter I'm fine" | NanoClaw sends via messaging |

### The Loop

```
Thump or "Hey Moon" → HUD activates → AI sees screen → Action executed → Voice confirms "Done"
```

1. User thumps their desk OR says "Hey Moon" + a command
2. The floating Moon HUD switches to the appropriate state
3. If the command needs screen context, a screenshot is sent to Claude
4. The AI returns coordinates and intent
5. PyAutoGUI performs the action, or NanoClaw handles the errand
6. ElevenLabs speaks "Task complete"
7. NanoClaw sends messaging errands; AWS SES sends email receipts


## Competitive Landscape

BouncingMoon sits at the intersection of Assistive Hardware and AI Agents.

### The Old Guard (Hardware-Heavy)
*   **Tobii (Eye-Tracking) / Cognixion (EEG/Head-Tracking):** These systems are the gold standard for high-level disability but come with extreme costs ($15k+) and require users to wear or calibrate bulky hardware.
*   **The BouncingMoon Edge:** No specialized hardware. It turns a standard $20 microphone and a webcam into a high-precision control suite using equipment the user already owns.

### The New Guard (Vision-First Agents)
*   **Envision AI / OrCam:** Incredible at describing the world for the visually impaired, but they lack the system-level "hands" to operate a complex desktop OS for motor-impaired users.
*   **The BouncingMoon Edge:** While others focus on *perception* (what is this?), we focus on *execution* (do this for me). We bridge the gap between "I can see the button" and "I can't physically click it."

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

The Moon HUD will appear in the top-left corner. Thump your desk or say "Hey Moon" to interact.

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

Two-way voice interface using ElevenLabs with two modes:

- **On-demand mode:** `listen()` records a single command (used after thumps)
- **Always-listening mode:** Continuously monitors for the wake word "Hey Moon", then captures and classifies the command

```bash
export ELEVENLABS_API_KEY="your-key"
python voice_interface.py
# Starts always-listening mode — say "Hey Moon" followed by a command
```

**Key methods:**

- `voice.listen()` — Record until silence, return transcribed text
- `voice.speak(text)` — Convert text to speech and play (blocking)
- `voice.speak_async(text)` — Same but non-blocking
- `voice.ask(question)` — Speak a question, then listen for answer
- `voice.start_listening(on_command=fn)` — Start always-listening mode with a callback
- `voice.pause_listening()` / `voice.resume_listening()` — Pause/resume (used during TTS to avoid echo)

**VoiceCommand classification:** Every voice command is automatically parsed into one of three categories:

- **Direct** (`cmd.is_direct`) — Can be executed instantly without AI vision (scroll, go back, open app, key presses)
- **Vision** (`cmd.needs_vision`) — Needs a screenshot to understand context (click the blue button, read the screen)
- **Errand** (`cmd.is_errand`) — Should be routed to NanoClaw for messaging (send a text, email someone)

### main.py — The Ghost

Wires everything together. Boots the HUD, starts the audio listener AND always-listening voice mode, and routes all events through the pipeline. Handles both thump triggers and voice commands through unified routing.

```bash
export ANTHROPIC_API_KEY="your-key"
export ELEVENLABS_API_KEY="your-key"
python main.py
```

**Command routing logic:**

- Direct voice commands (scroll, go back, open app) execute instantly without calling the vision API
- Vision commands (click something, read the screen) take a screenshot and go through Claude
- Errand commands (send a message, email someone) are routed to NanoClaw
- Triple-thump voice commands go through the same routing as wake-word commands

**Safety features:**

- `pyautogui.FAILSAFE = True` — move mouse to any screen corner to abort
- Say "Hey Moon, stop" at any time to cancel the current action
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
- Test that always-listening mode correctly detects "Hey Moon" without too many false positives
- Adjust silence detection for slower speech patterns (Parkinson's users may speak slower)
- Prevent mic contention — the always-listening loop pauses during TTS but verify this works smoothly
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
