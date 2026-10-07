"""
Constants that set how action chunks are compiled and played: button bindings, skill timings,
chunk limits, and the thresholds of the `until` checks.
"""

from __future__ import annotations

from .io import Button

# --- It Takes Two bindings (Xbox defaults, EA accessibility page) -----------------------------

JUMP = Button.A
DASH = Button.X
INTERACT = Button.Y
CANCEL = Button.B
GROUND_POUND = Button.B
SPRINT = Button.LS
GRAPPLE = Button.RB
LOCATE_PARTNER = Button.RS
SKIP_CUTSCENE = Button.B

# --- Skill timings (ms) -----------------------------------------------------------------------

TAP_MS = 100  # how long a button is held for a "press"
# Jump height grows with how long A is held: a 100 ms tap is a short hop, 250 ms is full height.
JUMP_HOLD_MS = 250
JUMP_AIR_MS = 400  # airtime after a jump press before the next step
DOUBLE_JUMP_GAP_MS = 250  # delay between the two jump presses
DASH_AFTER_MS = 250
GROUND_POUND_AFTER_MS = 500
GRAPPLE_AFTER_MS = 600
LOCATE_PARTNER_AFTER_MS = 200  # allow the partner indicator to appear before observing
SKIP_CUTSCENE_MS = 2000  # default hold; the model can choose a longer duration
# Right stick held fully left or right for this long turns the camera a full circle, so
# `look r 90deg` holds it for a quarter of this. A first guess: measure it with
# `uv run python calibrate_look.py` and put the result here.
LOOK_MS_PER_TURN = 2400

# --- Chunk limits ----------------------------------------------------------------------------

MAX_CHUNK_MS = 10_000
MAX_REPEAT = 20  # most times a `repeat` block may play
MIN_RELEASE_FRACTION = 0.5  # earliest point, as a fraction of the chunk, to hand back a frame
AUTO_WATCH_RUN_MS = 3000
CARRY_MAX_MS = 6000  # longest a final run keeps going while the model plans the next chunk

# --- `until` checks ---------------------------------------------------------------------------
# Diffs are mean absolute grayscale difference (0-255) between consecutive snapshots of our half
# of the screen. Thresholds are first guesses — tune them from recorded runs.

SNAPSHOT_SIZE = (160, 90)  # full frame; our half is cropped from it
CHECK_EVERY_MS = 100
STUCK_DIFF = 2.0  # below this our view is ~frozen
STUCK_MS = 400  # ...for this long while the stick is pushed = stuck
STUCK_GRACE_MS = 300  # ignore the start of a step (accelerating from standstill)
CUT_DIFF = 40.0  # a jump this big in one tick = cutscene cut, fade, respawn, menu

# --- Jump landing check -----------------------------------------------------------------------
# Compares snapshots from just before and just after each jump step. The camera follows the
# character's height, so landing higher moves the scene down in the view. First guesses too.
JUMP_BLOCKED_DIFF = 3.0  # below this the view barely changed: the jump got you nowhere
JUMP_SHIFT_MAX = 0.15  # largest vertical view shift searched, as a fraction of the height
JUMP_RISE = 0.04  # a shift at least this big (fraction of the height) counts as higher/lower
JUMP_MATCH = 0.6  # a shift counts only if it cuts the difference to this share of unshifted

# --- Pipelining -------------------------------------------------------------------------------
# Lead time: how long before a chunk's end its result goes back, i.e. how long the model takes
# to send the next `act`. Measured act-to-act within a turn and smoothed; these bound it.
LEAD_DEFAULT_MS = 3500
LEAD_MIN_MS = 1000
LEAD_MAX_MS = 6000
LEAD_SMOOTHING = 0.3  # weight of the newest gap
IDLE_REPORT_MS = 200  # report the moving share when the model stood still at least this long

# --- Stall nudge ------------------------------------------------------------------------------
# A model stuck on one task (e.g. climbing a box that is not on the route) rarely notices by
# itself. After this many chunks on one task, the result asks it to step back.
STALL_CHUNKS = 8
STALL_REPEAT = 6  # ...and again every this many chunks after that
