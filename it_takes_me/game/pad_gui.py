"""On-screen Xbox controller: shows what the pad is doing and lets you press it with the mouse.

`PadMixer` sits between the agent and the real `GameIO`: the agent's `set_pad` and your clicks
are merged (buttons OR'd, your stick wins when you are pushing it) and written to the one
virtual pad, so the game sees a single controller. `PadWindow` draws the pad from the mixer.

Mouse: left-click and hold a button to press it; drag a stick; right-click a stick to click it
(LS/RS).
"""

from __future__ import annotations

import math
import sys
import threading
import tkinter as tk
from typing import TYPE_CHECKING

from .io import NEUTRAL, Button, GameIO, PadState, grab

if TYPE_CHECKING:
    from PIL.Image import Image


class PadMixer:
    """A `GameIO` that merges the agent's pad state with manual input from the window."""

    def __init__(self, inner: GameIO) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._agent = NEUTRAL
        self._manual = NEUTRAL

    def capture(self) -> bytes:
        return self._inner.capture()

    def capture_image(self) -> Image:
        return grab(self._inner)

    def snapshot(self, size: tuple[int, int]) -> Image:
        return self._inner.snapshot(size)

    def set_pad(self, state: PadState) -> None:
        with self._lock:
            self._agent = state
            self._write()

    def set_manual(self, state: PadState) -> None:
        with self._lock:
            self._manual = state
            self._write()

    def states(self) -> tuple[PadState, PadState]:
        """(agent, manual) as last set."""
        with self._lock:
            return self._agent, self._manual

    def _write(self) -> None:
        a, m = self._agent, self._manual
        self._inner.set_pad(
            PadState(
                left=m.left if m.left != (0.0, 0.0) else a.left,
                right=m.right if m.right != (0.0, 0.0) else a.right,
                buttons=a.buttons | m.buttons,
            )
        )

    def close(self) -> None:
        self._inner.close()


# --- drawing ------------------------------------------------------------------------------

W, H = 520, 330
STICK_R = 34  # stick well radius; the knob travels this far at full deflection
KNOB_R = 15
IDLE = "#3a3f47"
AGENT = "#e0529c"  # outline of anything the agent is pressing
BG = "#16181c"
BODY = "#2a2e35"

# name -> (shape, x0, y0, x1, y1, lit colour)
_BUTTONS: dict[Button, tuple[str, int, int, int, int, str]] = {
    Button.LT: ("rect", 110, 12, 190, 34, "#d0d0d0"),
    Button.RT: ("rect", 330, 12, 410, 34, "#d0d0d0"),
    Button.LB: ("rect", 100, 44, 200, 64, "#d0d0d0"),
    Button.RB: ("rect", 320, 44, 420, 64, "#d0d0d0"),
    Button.BACK: ("oval", 222, 130, 246, 150, "#d0d0d0"),
    Button.START: ("oval", 274, 130, 298, 150, "#d0d0d0"),
    Button.Y: ("oval", 356, 94, 386, 124, "#f2c230"),
    Button.X: ("oval", 324, 126, 354, 156, "#3d8ef0"),
    Button.B: ("oval", 388, 126, 418, 156, "#e5484d"),
    Button.A: ("oval", 356, 158, 386, 188, "#3fbf5f"),
    Button.DPAD_UP: ("rect", 188, 188, 210, 212, "#d0d0d0"),
    Button.DPAD_DOWN: ("rect", 188, 234, 210, 258, "#d0d0d0"),
    Button.DPAD_LEFT: ("rect", 164, 212, 188, 234, "#d0d0d0"),
    Button.DPAD_RIGHT: ("rect", 210, 212, 234, 234, "#d0d0d0"),
}
_STICKS = {"left": (150, 140, Button.LS), "right": (320, 222, Button.RS)}


class PadWindow:
    def __init__(self, mixer: PadMixer, title: str = "pad") -> None:
        self.mixer = mixer
        self.root = tk.Tk()
        self.root.title(title)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        _never_take_focus(self.root)
        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=BG, highlightthickness=0)
        self.canvas.pack()

        self._held: set[Button] = set()
        self._sticks: dict[str, tuple[float, float]] = {"left": (0.0, 0.0), "right": (0.0, 0.0)}
        self._dragging: str | None = None
        self._pressing: Button | None = None

        self._draw_static()
        self._status = self.canvas.create_text(
            W // 2, H - 14, fill="#9aa0a6", font=("Consolas", 10), text=""
        )
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # Right-click a stick to click it (Button-2 on macOS Tk, Button-3 elsewhere).
        for seq in ("<ButtonPress-3>", "<ButtonPress-2>"):
            self.canvas.bind(seq, self._on_stick_click)
        for seq in ("<ButtonRelease-3>", "<ButtonRelease-2>"):
            self.canvas.bind(seq, self._on_stick_unclick)
        self._tick()

    # -- layout ------------------------------------------------------------------------------

    def _draw_static(self) -> None:
        c = self.canvas
        c.create_oval(60, 70, 260, 300, fill=BODY, outline="")
        c.create_oval(260, 70, 460, 300, fill=BODY, outline="")
        c.create_rectangle(140, 70, 380, 250, fill=BODY, outline="")
        for button, (shape, x0, y0, x1, y1, _) in _BUTTONS.items():
            make = c.create_oval if shape == "oval" else c.create_rectangle
            make(x0, y0, x1, y1, fill=IDLE, outline="", width=3, tags=("button", button.value))
            c.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text=_label(button),
                fill="#f5f5f5",
                font=("Segoe UI", 8, "bold"),
                tags=("label", button.value),
            )
        for name, (x, y, click) in _STICKS.items():
            c.create_oval(
                x - STICK_R, y - STICK_R, x + STICK_R, y + STICK_R,
                fill="#1f2227", outline="", width=3, tags=("well", name, click.value),
            )  # fmt: skip
            c.create_oval(
                x - KNOB_R, y - KNOB_R, x + KNOB_R, y + KNOB_R,
                fill=IDLE, outline="", width=3, tags=("knob", name),
            )  # fmt: skip

    # -- mouse -------------------------------------------------------------------------------

    def _hit(self, event: tk.Event) -> tuple[str, str] | None:
        """(kind, name) of the button or stick under the mouse."""
        for item in reversed(self.canvas.find_overlapping(event.x, event.y, event.x, event.y)):
            tags = self.canvas.gettags(item)
            if "button" in tags or "label" in tags:
                return "button", tags[1]
            if "well" in tags or "knob" in tags:
                return "stick", tags[1]
        return None

    def _on_press(self, event: tk.Event) -> None:
        hit = self._hit(event)
        if hit is None:
            return
        kind, name = hit
        if kind == "button":
            self._pressing = Button(name)
            self._held.add(self._pressing)
        else:
            self._dragging = name
            self._drag_to(event)
        self._push()

    def _on_drag(self, event: tk.Event) -> None:
        if self._dragging is not None:
            self._drag_to(event)
            self._push()

    def _on_release(self, _: tk.Event) -> None:
        if self._pressing is not None:
            self._held.discard(self._pressing)
            self._pressing = None
        if self._dragging is not None:
            self._sticks[self._dragging] = (0.0, 0.0)
            self._dragging = None
        self._push()

    def _drag_to(self, event: tk.Event) -> None:
        assert self._dragging is not None
        cx, cy, _ = _STICKS[self._dragging]
        x, y = (event.x - cx) / STICK_R, (cy - event.y) / STICK_R  # +y is up
        norm = math.hypot(x, y)
        if norm > 1.0:
            x, y = x / norm, y / norm
        self._sticks[self._dragging] = (x, y)

    def _on_stick_click(self, event: tk.Event) -> None:
        hit = self._hit(event)
        if hit and hit[0] == "stick":
            self._held.add(_STICKS[hit[1]][2])
            self._push()

    def _on_stick_unclick(self, _: tk.Event) -> None:
        self._held -= {Button.LS, Button.RS}
        self._push()

    def _push(self) -> None:
        self.mixer.set_manual(
            PadState(
                left=self._sticks["left"],
                right=self._sticks["right"],
                buttons=frozenset(self._held),
            )
        )

    # -- display -----------------------------------------------------------------------------

    def _tick(self) -> None:
        agent, manual = self.mixer.states()
        c = self.canvas
        for button, (*_, lit) in _BUTTONS.items():
            on = button in agent.buttons or button in manual.buttons
            c.itemconfigure(
                f"button&&{button.value}",
                fill=lit if on else IDLE,
                outline=AGENT if button in agent.buttons else "",
            )
        for name, (x, y, click) in _STICKS.items():
            sx, sy = manual.left if name == "left" else manual.right
            if (sx, sy) == (0.0, 0.0):
                sx, sy = agent.left if name == "left" else agent.right
            c.coords(
                f"knob&&{name}",
                x + sx * STICK_R - KNOB_R, y - sy * STICK_R - KNOB_R,
                x + sx * STICK_R + KNOB_R, y - sy * STICK_R + KNOB_R,
            )  # fmt: skip
            agent_moving = (agent.left if name == "left" else agent.right) != (0.0, 0.0)
            c.itemconfigure(
                f"knob&&{name}",
                fill="#d0d0d0" if (sx, sy) != (0.0, 0.0) else IDLE,
                outline=AGENT if agent_moving else "",
            )
            clicked = click in agent.buttons or click in manual.buttons
            c.itemconfigure(
                f"well&&{name}",
                outline="#d0d0d0" if clicked else "",
            )
        c.itemconfigure(self._status, text=f"agent: {_describe(agent)}    you: {_describe(manual)}")
        self.root.after(33, self._tick)

    def run(self) -> None:
        """Run the window on this thread until it is closed. Releases manual input on close."""
        try:
            self.root.mainloop()
        finally:
            self.mixer.set_manual(NEUTRAL)


def start_pad_window(mixer: PadMixer, title: str = "pad") -> threading.Thread:
    """Open the window on a daemon thread, so the caller's thread stays free for the agent.
    Tk objects are created and used only on that thread."""

    def _run() -> None:
        PadWindow(mixer, title).run()

    thread = threading.Thread(target=_run, daemon=True, name="pad-window")
    thread.start()
    return thread


def _never_take_focus(root: tk.Tk) -> None:
    """Keep the game focused while the pad is clicked: games (Unreal included) drop controller
    input while their window is in the background, so a focus-stealing pad would do nothing."""
    if sys.platform != "win32":
        return
    import ctypes

    gwl_exstyle, ws_ex_noactivate, ws_ex_appwindow = -20, 0x08000000, 0x00040000
    root.update_idletasks()
    hwnd = int(root.wm_frame(), 16)
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongPtrW(hwnd, gwl_exstyle)
    # APPWINDOW keeps it in the taskbar, which NOACTIVATE would otherwise hide it from.
    user32.SetWindowLongPtrW(hwnd, gwl_exstyle, style | ws_ex_noactivate | ws_ex_appwindow)


def _label(button: Button) -> str:
    return {
        Button.DPAD_UP: "",
        Button.DPAD_DOWN: "",
        Button.DPAD_LEFT: "",
        Button.DPAD_RIGHT: "",
        Button.BACK: "",
        Button.START: "",
    }.get(button, button.value)


def _describe(state: PadState) -> str:
    parts = sorted(b.value for b in state.buttons)
    for name, (x, y) in (("L", state.left), ("R", state.right)):
        if (x, y) != (0.0, 0.0):
            parts.append(f"{name}({x:+.1f},{y:+.1f})")
    return " ".join(parts) or "-"
