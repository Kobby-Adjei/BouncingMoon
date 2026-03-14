"""
vision_agent.py — Screen understanding via Claude vision for BouncingMoon.

Takes a screenshot, sends it to Claude with a structured prompt,
and returns the identified UI element with pixel coordinates.
All processing is in-memory — no screenshots saved to disk.
"""

import base64
import io
import json
import os

import anthropic
from PIL import ImageGrab


CLAUDE_MODEL = "claude-sonnet-4-6-20250514"

CLICK_PROMPT = """\
You are an accessibility agent helping a user with Parkinson's disease.
Look at this screenshot. The user wants to CLICK the primary actionable element
(the most prominent button, link, or interactive control).

Return ONLY a JSON object with no other text:
{"element": "name/label of the element", "x": pixel_x, "y": pixel_y, "type": "button|link|field|menu", "confidence": 0.0-1.0}

If you see multiple actionable elements and are unsure which one, return:
{"ambiguous": true, "options": [{"element": "name", "x": px, "y": py, "type": "..."}], "confidence": 0.0}
"""

AUTOFILL_PROMPT = """\
You are an accessibility agent helping a user with Parkinson's disease.
Look at this screenshot. The user wants to FILL OUT a form on this screen.

Identify ALL visible input fields. Return ONLY a JSON object with no other text:
{"fields": [{"label": "field name", "x": pixel_x, "y": pixel_y, "type": "text|email|phone|address|name|date|password|other"}], "confidence": 0.0-1.0}
"""

GENERAL_PROMPT = """\
You are an accessibility agent helping a user with Parkinson's disease.
Look at this screenshot. The user gave this voice command: "{command}"

Determine what action to take on this screen to fulfill the command.
Return ONLY a JSON object with no other text:
{"action": "click|type|scroll|open", "element": "name/label", "x": pixel_x, "y": pixel_y, "type": "button|link|field|menu|app", "value": "text to type if applicable or null", "confidence": 0.0-1.0}
"""


def capture_screenshot() -> bytes:
    """Capture the full screen and return as PNG bytes (in memory only)."""
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def analyze_screen(intent: str = "click", command: str = "") -> dict:
    """
    Screenshot the screen and ask Claude what to do.

    Args:
        intent: "click", "autofill", or "command"
        command: The voice command text (only used when intent="command")

    Returns:
        Parsed JSON dict from Claude's response.
    """
    client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var

    screenshot_bytes = capture_screenshot()
    b64_image = base64.b64encode(screenshot_bytes).decode("utf-8")

    if intent == "click":
        prompt = CLICK_PROMPT
    elif intent == "autofill":
        prompt = AUTOFILL_PROMPT
    elif intent == "command":
        prompt = GENERAL_PROMPT.format(command=command)
    else:
        prompt = CLICK_PROMPT

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1])

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"error": "Failed to parse AI response", "raw": raw, "confidence": 0.0}

    return result


def is_confident(result: dict, min_confidence: float = 0.8) -> bool:
    """Check if the AI is confident enough to act without asking."""
    return result.get("confidence", 0.0) >= min_confidence


# ── Quick test ────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Taking screenshot and analyzing…")
    result = analyze_screen(intent="click")
    print(json.dumps(result, indent=2))

    if not is_confident(result):
        print("\n⚠ Low confidence — would trigger voice clarification.")
    else:
        print(f"\n✓ Confident. Would click: {result.get('element')} at ({result.get('x')}, {result.get('y')})")
