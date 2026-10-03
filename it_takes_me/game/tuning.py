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
JUMP_AIR_MS = 400  # airtime after a jump press before the next step
DOUBLE_JUMP_GAP_MS = 250  # delay between the two jump presses
DASH_AFTER_MS = 250
GROUND_POUND_AFTER_MS = 500
GRAPPLE_AFTER_MS = 600
LOCATE_PARTNER_AFTER_MS = 200  # allow the partner indicator to appear before observing
SKIP_CUTSCENE_MS = 2000  # default hold; the model can choose a longer duration

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

# --- Pipelining -------------------------------------------------------------------------------
# Lead time: how long before a chunk's end its result goes back, i.e. how long the model takes
# to send the next `act`. Measured act-to-act within a turn and smoothed; these bound it.
LEAD_DEFAULT_MS = 3500
LEAD_MIN_MS = 1000
LEAD_MAX_MS = 6000
LEAD_SMOOTHING = 0.3  # weight of the newest gap
IDLE_REPORT_MS = 200  # report the moving share when the model stood still at least this long
