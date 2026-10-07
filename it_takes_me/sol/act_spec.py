"""The model-facing description and input schema of the `act` tool."""

from __future__ import annotations

from typing import Any

from it_takes_me.game.tuning import (
    CARRY_MAX_MS,
    LEAD_DEFAULT_MS,
    MAX_REPEAT,
    MIN_RELEASE_FRACTION,
)

_ACT_DESCRIPTION = """\
Play a chunk of input (up to {max_ms} ms total). Steps run back to back with exact timing on \
the pad; {ending} On clear ground you can run as far as you can see in one chunk. End the \
chunk at the first jump or climb whose landing you cannot be sure of: later steps assume it \
landed, and the local checks cannot detect arrival or reliably prevent falls.

`steps` is one line of steps separated by `;`, each a skill then tokens in any order:
  run f 1200; jump f; run f 300; double_jump fl; interact 500
Tokens:
- direction, relative to the camera: f b l r fl fr bl br none, or @<deg> for an exact \
heading (@0 forward, @90 right, @-90 left, @180 back). For look: l r u d.
- <number>: the step's TOTAL ms, including button holds and movement after them.
- <n>deg: look l/r only, instead of ms: turn the camera n degrees (look r 90deg is a quarter \
turn, look l 20deg a small correction). Judge the angle from the frame.
- h<ms>: how long the button is held (hold_ms); g<ms>: released gap between the two presses \
of double_jump/jump_dash (gap_ms). Each press holds h, so ms must cover 2*h + g.
- s<0.1-1>: stick push (speed), 1 = full run; distance is roughly speed x ms. On look it \
is the camera turn rate (look_speed).
- run only: sprint; stuck and cut (end the step early on that check; see below).
- buttons in capitals: A B X Y LB RB LT RT LS RS START BACK DPAD_UP DPAD_DOWN DPAD_LEFT \
DPAD_RIGHT. press and ability take one; raw holds all it lists.
- raw only: L<x>,<y> and R<x>,<y> stick positions in [-1, 1], +y forward/up.
- <times>x(<steps>): repeat steps 2-{max_repeat} times, e.g. 4x(jump l; jump r). \
The expanded chunk must fit {max_ms} ms; repeats cannot nest.

Skills (defaults; buttons are tapped for 100 ms unless noted):
- run <dir> [500]: left stick; needs a direction.
- jump [650, h250]; double_jump or djump [1150, h250 g250]: A is held 250 ms for full \
height; add h100 only for a small hop.
- dash [350] (works in the air too); \
jump_dash or jdash [700, g250]; ground_pound or pound [600] (while airborne).
- interact [100]: Y, held for the whole step unless h is given.
- grapple [700]: RB, to a rope point in range.
- ability <LT|RT> [300]: trigger, held for the whole step unless h is given.
- look <l|r|u|d> [200]: turn the camera; for l/r give degrees (look l 45deg). This changes \
what forward means for every later step.
- locate_partner or locate [300]: click RS to reveal your partner's location. Use it alone \
and inspect the returned frame before moving.
- skip_cutscene or skip [2000]: hold B with neutral sticks. Use it alone and inspect the \
returned frame; give a longer ms if the skip prompt needs more.
- press <button> [100]: tap any button, optionally steering.
- raw <ms> [L<x>,<y>] [R<x>,<y>] [buttons]: exact pad state for anything else; it must press \
or push something.
There is no wait: start each move straight after the last, giving a jump enough ms to land.
A directional jump/double_jump/jump_dash/dash must keep steering until it lands: its ms must \
cover the default airtime unless the next step is another directional move (run, jump, \
dash) or a ground_pound/grapple; otherwise the chunk is rejected. Leave s at 1 on jumps. \
Moving sideways makes the camera swing and curves your path, so on ledges face along the \
ledge with look first, then walk f at s0.3-0.5.
After each jump/double_jump/jump_dash the result's `landings` line says whether the view \
suggests you landed higher, lower, or at the same height (a guess from the camera). If a \
directional jump changed nothing (blocked by a wall or ledge), the chunk stops there and the \
steps after it are skipped, because they assumed the jump landed.

Runs longer than 3000 ms automatically end early when your view stops changing while \
moving (stuck: walked into a wall) or the screen changes abruptly (cut: cutscene, fade, \
respawn, menu); shorter runs can opt in with stuck/cut. The rest of the chunk is then \
skipped and you are told which check fired.

{pipelining}`observe: "keyframes"` also returns {n} frames from during the chunk. \
Only use it to diagnose a failure.{keyframes_cost}"""

_PIPELINING = """\
Pipelining: a chunk at least twice your planning time (about {long_s} s or more) comes back \
about {lead_s} s before it ends, while its last steps are still playing; shorter chunks come \
back when they end. Plan your next chunk from where those steps will leave you: it is \
queued and starts the instant this one ends. If this chunk is then stopped early by a check, \
your next chunk is NOT played (nothing is pressed); you get the reason and a fresh frame.
`keep_moving: true` (last step must be a run with a direction): after the chunk, keep \
running the same way until your next chunk starts, a stuck view, a scene cut, or {carry_s} s. \
Use it on clear ground heading toward your target, never toward an edge or right up to it.
A chunk of only look steps is rejected unless it uses `observe: "keyframes"` (to scan around) \
or carries `say`: put the camera turn in front of the move that follows it instead.
"""


def act_description(max_chunk_ms: int, keyframes: int, *, pipelining: bool = False) -> str:
    if pipelining:
        ending = (
            "the pad returns to neutral when the chunk ends unless another chunk is queued or "
            "`keep_moving` is set. Planning your next call takes a few seconds."
        )
        extra = _PIPELINING.format(
            carry_s=CARRY_MAX_MS // 1000,
            lead_s=round(LEAD_DEFAULT_MS / 1000, 1),
            long_s=round(LEAD_DEFAULT_MS / 1000 / (1 - MIN_RELEASE_FRACTION)),
        )
        cost = " It waits for the end of the chunk, giving up pipelining for it."
    else:
        ending = (
            "the pad returns to neutral when the chunk ends, and you get the frame showing "
            "where you ended up. Nothing moves while you plan, so take the time to plan from it."
        )
        extra, cost = "", ""
    return _ACT_DESCRIPTION.format(
        max_ms=max_chunk_ms,
        n=keyframes,
        max_repeat=MAX_REPEAT,
        ending=ending,
        pipelining=extra,
        keyframes_cost=cost,
    )


def act_schema(max_chunk_ms: int, *, pipelining: bool = False) -> dict[str, Any]:
    del max_chunk_ms  # the limit is checked when the steps compile
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "string",
                "minLength": 1,
                "description": "Steps separated by `;`, e.g. `run f 1200; jump f; run f 300`.",
            },
            "task": {
                "type": "string",
                "maxLength": 200,
                "description": "Only when there is no current task or it changes: the one task "
                'you commit to, e.g. "pull the lever with Cody". Omit it to keep the current one.',
            },
            "previous_task": {
                "type": "string",
                "enum": ["done", "blocked"],
                "description": "Only when `task` changes: whether the previous task is "
                "complete or impossible right now.",
            },
            "intent": {
                "type": "string",
                "maxLength": 100,
                "description": "What this chunk does toward the task and where it should leave "
                "you, in a few words.",
            },
            "observe": {
                "type": "string",
                "enum": ["keyframes"],
                "description": "Only to diagnose a failed sequence; omit otherwise.",
            },
            "keep_moving": {
                "type": "boolean",
                "description": "Keep the final run going until your next chunk starts.",
            },
            "say": {
                "type": "string",
                "maxLength": 300,
                "description": "Optional: a short note on what you are doing and why, "
                "shown on a log. Your partner does not read it.",
            },
        },
        "required": ["intent", "steps"],
        "additionalProperties": False,
    }
    if not pipelining:
        del schema["properties"]["keep_moving"]
    return schema
