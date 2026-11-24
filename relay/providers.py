"""The apps Relay knows, in one place: how each is named, how it is recognised, and its logo.

The connector reads the name an app gives itself when it connects (MCP clientInfo), the Allow page
reads the name it registered with (OAuth), and the web app shows names and logos: all of them use
this table (the web app fetches it from /providers), so an app is never Muse in one place and
ChatGPT in another.

Recognition is by whole words, most specific first: "OpenAI Codex" is Codex, "Claude via MetaMCP" is
Claude, and "museum-bot" is nobody. Display only: a provider picks a logo and a label, nothing else.
The logos are LobeHub Icons (MIT); see logos/NOTICE.md.
"""
from __future__ import annotations

import re
from typing import Optional

# id, label, the handle suggested for a new agent, whole words that identify it (checked in this order)
PROVIDERS = (
    ("codex", "Codex", "codex", ("codex",)),
    ("claude", "Claude", "claude", ("claude", "anthropic")),
    ("openai", "ChatGPT", "chatgpt", ("chatgpt", "openai")),
    ("grok", "Grok", "grok", ("grok", "xai", "x.ai")),
    ("gemini", "Gemini", "gemini", ("gemini",)),
    ("cursor", "Cursor", "cursor", ("cursor",)),
    ("copilot", "Copilot", "copilot", ("copilot", "visual studio code", "vscode")),
    ("muse", "Muse", "muse", ("muse", "meta ai", "meta")),
)
LOGOS = ("claude", "codex", "gemini", "copilot", "muse", "openai", "grok", "cursor")     # files in relay/logos
_RX = [(pid, re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in words) + r")(?![a-z0-9])"))
       for pid, _label, _handle, words in PROVIDERS]


def detect(name: Optional[str]) -> Optional[str]:
    """The provider an app's own name points to, or None."""
    n = (name or "").lower()
    return next((pid for pid, rx in _RX if rx.search(n)), None)


def label(pid: Optional[str]) -> str:
    return next((lab for p, lab, _h, _w in PROVIDERS if p == pid), "Other")


def handle_for(pid: Optional[str]) -> str:
    """A name to suggest for an agent on this app."""
    return next((h for p, _l, h, _w in PROVIDERS if p == pid), "agent")


def table() -> list:
    """What the web app needs: ids, labels, the words, and which have a logo file."""
    return [{"id": p, "label": lab, "handle": h, "words": list(w), "logo": p in LOGOS} for p, lab, h, w in PROVIDERS]
