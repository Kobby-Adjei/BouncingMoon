"""
overlay_hud.py — Floating moon HUD overlay for BouncingMoon.

A 150×150 always-on-top circular overlay that gives visual feedback:
  IDLE      → dim moon
  ACTIVE    → pulsing glow (thump detected)
  THINKING  → orbiting dots while AI processes
  SUCCESS   → green glow + checkmark
  LISTENING → blue glow (voice command mode)
"""

import math
import threading
from enum import Enum, auto

import customtkinter as ctk


class HUDState(Enum):
    IDLE = auto()
    ACTIVE = auto()
    THINKING = auto()
    SUCCESS = auto()
    LISTENING = auto()


# ── Colors ────────────────────────────────────────────────────────
COLORS = {
    HUDState.IDLE:      {"bg": "#1a1a2e", "ring": "#3a3a5c", "text": "#666688"},
    HUDState.ACTIVE:    {"bg": "#2e1a4a", "ring": "#8844cc", "text": "#bb77ff"},
    HUDState.THINKING:  {"bg": "#1a2e3e", "ring": "#4488cc", "text": "#66bbff"},
    HUDState.SUCCESS:   {"bg": "#1a2e1a", "ring": "#44cc44", "text": "#66ff66"},
    HUDState.LISTENING: {"bg": "#1a1a3e", "ring": "#4466ee", "text": "#6699ff"},
}

SIZE = 150
ANIM_FPS = 30


class OverlayHUD:
    """Floating circular HUD that runs in its own thread."""

    def __init__(self):
        self._state = HUDState.IDLE
        self._root = None
        self._canvas = None
        self._thread = None
        self._anim_angle = 0.0
        self._pulse_phase = 0.0
        self._status_text = ""
        self._ready = threading.Event()

    # ── Public API (thread-safe) ──────────────────────────────────

    def start(self):
        """Launch the HUD window in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def set_state(self, state: HUDState, text: str = ""):
        """Change the HUD state from any thread."""
        self._state = state
        self._status_text = text
        # Animation resets
        if state == HUDState.THINKING:
            self._anim_angle = 0.0
        if state == HUDState.ACTIVE:
            self._pulse_phase = 0.0

    def stop(self):
        if self._root:
            self._root.after(0, self._root.destroy)

    # ── Window setup ──────────────────────────────────────────────

    def _run(self):
        self._root = ctk.CTk()
        self._root.title("")
        self._root.geometry(f"{SIZE}x{SIZE}+50+50")
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)

        # Transparency — platform-dependent
        try:
            self._root.attributes("-transparentcolor", "#000000")
        except Exception:
            try:
                self._root.attributes("-alpha", 0.92)
            except Exception:
                pass

        # Make window draggable
        self._root.bind("<Button-1>", self._start_drag)
        self._root.bind("<B1-Motion>", self._do_drag)

        self._canvas = ctk.CTkCanvas(
            self._root, width=SIZE, height=SIZE,
            bg="#000000", highlightthickness=0,
        )
        self._canvas.pack()

        self._ready.set()
        self._animate()
        self._root.mainloop()

    # ── Drag handling ─────────────────────────────────────────────

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event):
        x = self._root.winfo_x() + event.x - self._drag_x
        y = self._root.winfo_y() + event.y - self._drag_y
        self._root.geometry(f"+{x}+{y}")

    # ── Rendering ─────────────────────────────────────────────────

    def _animate(self):
        if not self._root or not self._root.winfo_exists():
            return

        self._canvas.delete("all")
        state = self._state
        colors = COLORS[state]
        cx, cy = SIZE // 2, SIZE // 2
        r = SIZE // 2 - 8

        # Background circle
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=colors["bg"], outline="", width=0,
        )

        if state == HUDState.IDLE:
            self._draw_idle(cx, cy, r, colors)

        elif state == HUDState.ACTIVE:
            self._draw_active(cx, cy, r, colors)
            self._pulse_phase += 0.15

        elif state == HUDState.THINKING:
            self._draw_thinking(cx, cy, r, colors)
            self._anim_angle += 4.0

        elif state == HUDState.SUCCESS:
            self._draw_success(cx, cy, r, colors)

        elif state == HUDState.LISTENING:
            self._draw_listening(cx, cy, r, colors)
            self._pulse_phase += 0.1

        # Status text
        if self._status_text:
            self._canvas.create_text(
                cx, cy + r - 10,
                text=self._status_text, fill=colors["text"],
                font=("Helvetica", 10, "bold"),
            )

        self._root.after(1000 // ANIM_FPS, self._animate)

    def _draw_idle(self, cx, cy, r, colors):
        """Dim moon with a subtle ring."""
        ring_w = 3
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=colors["ring"], width=ring_w,
        )
        # Moon crescent
        self._canvas.create_text(
            cx, cy - 5, text="☽", fill=colors["text"],
            font=("Helvetica", 36),
        )

    def _draw_active(self, cx, cy, r, colors):
        """Pulsing glow ring."""
        pulse = abs(math.sin(self._pulse_phase))
        ring_w = 3 + int(pulse * 6)
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=colors["ring"], width=ring_w,
        )
        self._canvas.create_text(
            cx, cy - 5, text="☽", fill=colors["text"],
            font=("Helvetica", 36),
        )

    def _draw_thinking(self, cx, cy, r, colors):
        """Orbiting dots around the moon."""
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=colors["ring"], width=2,
        )
        self._canvas.create_text(
            cx, cy - 5, text="☽", fill=colors["text"],
            font=("Helvetica", 36),
        )
        # 3 orbiting dots
        orbit_r = r - 12
        for i in range(3):
            angle = math.radians(self._anim_angle + i * 120)
            dx = cx + orbit_r * math.cos(angle)
            dy = cy + orbit_r * math.sin(angle)
            dot_r = 5
            self._canvas.create_oval(
                dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r,
                fill=colors["ring"], outline="",
            )

    def _draw_success(self, cx, cy, r, colors):
        """Green ring with checkmark."""
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=colors["ring"], width=4,
        )
        self._canvas.create_text(
            cx, cy - 5, text="✓", fill=colors["text"],
            font=("Helvetica", 42, "bold"),
        )

    def _draw_listening(self, cx, cy, r, colors):
        """Blue pulsing ring with mic icon."""
        pulse = abs(math.sin(self._pulse_phase))
        ring_w = 3 + int(pulse * 4)
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=colors["ring"], width=ring_w,
        )
        self._canvas.create_text(
            cx, cy - 5, text="🎤", fill=colors["text"],
            font=("Helvetica", 30),
        )


# ── Quick test ────────────────────────────────────────────────────
if __name__ == "__main__":
    import time as _time

    hud = OverlayHUD()
    hud.start()

    def _cycle():
        _time.sleep(2)
        print("→ ACTIVE")
        hud.set_state(HUDState.ACTIVE, "Thump!")
        _time.sleep(2)
        print("→ THINKING")
        hud.set_state(HUDState.THINKING, "Thinking…")
        _time.sleep(3)
        print("→ LISTENING")
        hud.set_state(HUDState.LISTENING, "Speak…")
        _time.sleep(3)
        print("→ SUCCESS")
        hud.set_state(HUDState.SUCCESS, "Done")
        _time.sleep(2)
        print("→ IDLE")
        hud.set_state(HUDState.IDLE)

    t = threading.Thread(target=_cycle, daemon=True)
    t.start()

    # Keep main thread alive for tkinter
    try:
        while True:
            _time.sleep(0.1)
    except KeyboardInterrupt:
        hud.stop()
