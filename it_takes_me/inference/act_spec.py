"""The model-facing description and input schema of the `act` tool."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from it_takes_me.game.chunks import CONDITIONS, DIRECTIONS, LOOK_DIRECTIONS, REPEAT, SKILLS
from it_takes_me.game.io import Button
from it_takes_me.game.tuning import (
    CARRY_MAX_MS,
    LEAD_DEFAULT_MS,
    MAX_REPEAT,
    MIN_RELEASE_FRACTION,
)

_BUTTON = {"type": "string", "enum": [b.value for b in Button]}
_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "enum": list(SKILLS)},
        "dir": {"type": "string", "enum": list(DIRECTIONS)},
        "heading": {
            "type": "number",
            "minimum": -180,
            "maximum": 180,
            "description": "Exact camera-relative angle instead of dir: 0 forward, 90 right, "
            "-90 left, 180 back.",
        },
        "speed": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 1,
            "description": "Left-stick push, 1 = full run (default). Lower is slower and "
            "covers less ground per ms.",
        },
        "look_speed": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 1,
            "description": "look only: camera turn rate, 1 = full (default).",
        },
        "ms": {
            "type": "integer",
            "minimum": 1,
            "description": "Total step duration, including button holds, gaps, and movement.",
        },
        "hold_ms": {
            "type": "integer",
            "minimum": 1,
            "description": "Button hold within ms; each hold for double_jump/jump_dash.",
        },
        "gap_ms": {
            "type": "integer",
            "minimum": 1,
            "description": "Released time between the two presses in double_jump/jump_dash.",
        },
        "sprint": {"type": "boolean"},
        "button": {**_BUTTON, "description": "Any button for press; only LT or RT for ability."},
        "look": {"type": "string", "enum": list(LOOK_DIRECTIONS)},
        "left": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "right": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "buttons": {"type": "array", "items": _BUTTON},
        "until": {"type": "array", "items": {"type": "string", "enum": list(CONDITIONS)}},
    },
    "required": ["skill"],
    "additionalProperties": False,
}

_ACT_DESCRIPTION = """\
Play a chunk of input (up to {max_ms} ms total). Steps run back to back with exact timing on \
the pad; the pad returns to neutral when the chunk ends unless another chunk is queued or \
`keep_moving` is set. Planning your next call takes a few seconds, so put everything you \
can already see into one chunk: run to the object, jump onto it, run to the circle, interact. \
Aim for 4-8 s per chunk when travelling. Use short chunks (under 2 s) only for precise jumps \
near edges or lining up with an object; the local checks cannot detect arrival or reliably \
prevent falls.

Pipelining: a chunk at least twice your planning time (about {long_s} s or more, such as a long \
run) comes back about {lead_s} s before it ends, while its last steps are still playing; \
shorter chunks come back when they end. The early frame shows that moment and the result \
says which steps are still to play. Plan your next chunk from where those steps will leave \
you: it is queued and starts the instant this one ends, so you never stand still while \
thinking. If this chunk is then stopped early by a check, your next chunk is NOT played \
(nothing is pressed); you get the reason and a fresh frame, and plan again from it.
`say`: talk to your co-op partner as the chunk starts; there is no separate tool for it, so \
put what you want to say on the chunk you are about to play.
A chunk of only `look` steps is rejected unless it uses `observe: "keyframes"` (to scan \
around and see several views) or carries `say`: put the camera turn in front of the move \
that follows it instead.
`keep_moving: true` (last step must be a run with a direction): after the chunk, keep \
running the same way until your next chunk starts, a stuck view, a scene cut, or {carry_s} s; \
the next result says how long you kept moving. Use it on clear ground heading toward your \
target, never toward an edge or right up to the target.

Every skill accepts `ms`, its TOTAL duration. Button skills also accept `hold_ms`: the button \
is held at the start, then released while movement continues for the remaining ms. Defaults \
are 100 ms holds, except interact/ability/skip_cutscene which hold for the entire step. \
A hold must fit within ms. double_jump/jump_dash also accept `gap_ms` (default 250): time between \
two holds, each of hold_ms. Their total ms must cover 2*hold_ms + gap_ms. All timings are \
positive integers. Omitted fields retain the defaults below.

`dir` is relative to the camera: forward, back, left, right, forward-left, forward-right, \
back-left, back-right, none. Every skill except raw/skip_cutscene accepts `dir` \
(steer while doing it). Instead of `dir` you can give `heading` in degrees (0 forward, \
90 right, -90 left, 180 back) to aim between those directions, e.g. along a narrow ledge. \
`speed` (0.1-1, default 1) sets how hard the stick is pushed: distance covered is roughly \
speed x ms, so on ledges, beams, and near edges walk forward at 0.3-0.5 in short steps. Leave \
jumps at speed 1. Moving sideways makes the camera swing to follow you and curves your path, \
so on ledges face along the ledge with `look` first, then walk forward. `look` turns the \
camera, which changes what forward means for every later step; `look_speed` (0.1-1) makes \
small camera adjustments. A directional jump/double_jump/jump_dash/dash must keep steering \
until it lands: its ms must cover the default airtime unless the next step is another \
directional move (run, jump, dash) or a ground_pound/grapple; otherwise it is rejected.

Skills:
- run {{dir|heading (required, not none), speed=1, ms=500, sprint?, until?}}: left stick.
- jump {{dir, ms=500, hold_ms=100}}: one jump, including movement after release.
- double_jump {{dir, ms=850, hold_ms=100, gap_ms=250}}: jump, then jump again in the air.
- dash {{dir, ms=350, hold_ms=100}}: quick dash (works in the air too).
- jump_dash {{dir, ms=700, hold_ms=100, gap_ms=250}}: jump then dash in the air.
- ground_pound {{dir, ms=600, hold_ms=100}}: slam down; use while airborne.
- interact {{dir, ms=100, hold_ms?}}: Y. Defaults to holding for ms; set hold_ms for a tap.
- grapple {{dir, ms=700, hold_ms=100}}: RB, grapple to a rope point in range.
- ability {{button: LT|RT, dir, ms=300, hold_ms?}}: trigger; defaults to holding for ms.
- look {{look: left|right|up|down, look_speed=1, dir, ms=200}}: turn the camera.
- locate_partner {{dir, ms=300, hold_ms=100}}: click RS to reveal your partner's location. \
Use this alone when you lose track of your partner; inspect the returned frame before moving.
- skip_cutscene {{ms=2000, hold_ms?}}: hold B to skip a visible cutscene, with neutral sticks. \
Defaults to holding for the entire ms. Use this as a single step and inspect the returned \
frame before resuming gameplay. Choose a longer ms if the skip prompt needs more hold time.
- press {{button, dir, ms=100, hold_ms=100}}: press any button, optionally move after release.
- raw {{ms, left: [x,y], right: [x,y], buttons: [...]}}: exact pad state for ms, for anything \
the skills cannot express; it must press or push something. Sticks in [-1, 1], +y is \
forward/up.
There is no wait: steps run back to back, so start each move straight after the last \
(give a jump enough ms to land before the next one). Your planning time between calls \
already stands you still.
- repeat {{times: 2-{max_repeat}, steps: [...]}}: play the listed steps `times` times in a row, \
for rhythmic sequences such as hopping between two walls, climbing, or mashing a button. The \
expanded chunk must still fit the {max_ms} ms limit; repeats cannot nest.

`until` (run only) turns `ms` into a timeout and ends the step early when a check \
fires; the rest of the chunk is then skipped and you are told which check fired:
- stuck: your view stopped changing while you were moving (walked into a wall or ledge).
- cut: the screen changed abruptly (cutscene, fade, respawn, menu).
Runs longer than 3000 ms automatically enable stuck and cut checks, \
even if until is omitted or empty. Shorter runs can opt in using until. For example, \
run forward ms=7000 executes without another model decision until its timeout or a check \
fires, provided it fits the chunk limit.

`observe: "keyframes"` waits for the end of the chunk and also returns {n} frames from during \
it, to see why a jump or sequence went wrong. Only use it to diagnose a failure: it gives up \
pipelining for that chunk."""


def act_description(max_chunk_ms: int, keyframes: int) -> str:
    return _ACT_DESCRIPTION.format(
        max_ms=max_chunk_ms,
        n=keyframes,
        carry_s=CARRY_MAX_MS // 1000,
        lead_s=round(LEAD_DEFAULT_MS / 1000, 1),
        long_s=round(LEAD_DEFAULT_MS / 1000 / (1 - MIN_RELEASE_FRACTION)),
        max_repeat=MAX_REPEAT,
    )


def act_schema(max_chunk_ms: int) -> dict[str, Any]:
    step_schema = deepcopy(_STEP_SCHEMA)
    for timing in ("ms", "hold_ms", "gap_ms"):
        step_schema["properties"][timing]["maximum"] = max_chunk_ms
    plain_step = deepcopy(step_schema)
    step_schema["properties"]["skill"]["enum"].append(REPEAT)
    step_schema["properties"]["times"] = {"type": "integer", "minimum": 2, "maximum": MAX_REPEAT}
    step_schema["properties"]["steps"] = {"type": "array", "items": plain_step, "minItems": 1}
    return {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "maxLength": 200,
                "description": 'The one task you are committed to, e.g. "pull the lever '
                'with Cody". Repeat it exactly on every call until the frame shows it is '
                "complete; only then name a new one (with `previous_task`).",
            },
            "previous_task": {
                "type": "string",
                "enum": ["done", "blocked"],
                "description": "Only when `task` changes: whether the previous task is "
                "complete or impossible right now.",
            },
            "intent": {
                "type": "string",
                "maxLength": 200,
                "description": "What this chunk does toward the task, in a few words.",
            },
            "steps": {"type": "array", "items": step_schema, "minItems": 1},
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
                "description": "Optional: something short to say to your human co-op "
                "partner (shown on their terminal) as this chunk starts: what you are "
                "about to do, or what you need them to do.",
            },
        },
        "required": ["task", "intent", "steps"],
        "additionalProperties": False,
    }
