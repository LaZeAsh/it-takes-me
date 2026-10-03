"""Dynamic tools exposed to the model on the Codex thread.

`ToolRegistry` turns Python callables into `dynamicTools` specs for `thread/start` and
dispatches the `item/tool/call` requests the app-server sends back while a turn runs.
`build_game_tools` wires the It Takes Two action set on top of a `GameIO`.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from PIL.Image import Image

from it_takes_me.game.chunks import (
    CARRY_MAX_MS,
    CONDITIONS,
    DIRECTIONS,
    LOOK_DIRECTIONS,
    MAX_CHUNK_MS,
    MAX_REPEAT,
    MIN_RELEASE_FRACTION,
    REPEAT,
    SKILLS,
    Job,
    PadThread,
    compile_chunk,
    expand_repeats,
    release_point,
)
from it_takes_me.game.io import Button, GameIO, grab
from it_takes_me.vision import FrameDetail, ModelFrame, ModelFrameEncoder, ScreenHalf

log = logging.getLogger(__name__)

ContentItem = dict[str, Any]
ToolHandler = Callable[[dict[str, Any]], list[ContentItem]]


def text_item(text: str) -> ContentItem:
    return {"type": "inputText", "text": text}


def image_item(frame: ModelFrame) -> ContentItem:
    b64 = base64.b64encode(frame.data).decode("ascii")
    return {"type": "inputImage", "imageUrl": f"data:{frame.media_type};base64,{b64}"}


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolBudgetExceeded(Exception):
    pass


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    # Per-turn budget. `None` disables it. Reset with `begin_turn()`.
    max_calls_per_turn: int | None = None
    calls_this_turn: int = 0
    # The one task the model has committed to via `act`; it must close it before switching.
    current_task: str | None = None
    # Called when play ends: stop anything running in the background. Each returns an optional
    # note for the model, e.g. how far a `keep_moving` run carried on.
    settle_hooks: list[Callable[[], str | None]] = field(default_factory=list)
    # Called at turn start, right before the turn's frame is captured. They must not wait for
    # a playing chunk; each returns an optional note, e.g. how much of it is left.
    progress_hooks: list[Callable[[], str | None]] = field(default_factory=list)
    # True while the newest frame the model has is a high-detail look with no action since.
    fresh_high_frame: bool = False
    # Observers get (tool, arguments, response_dict, duration_ms) after every dispatch.
    observers: list[Callable[[str, dict[str, Any], dict[str, Any], float], None]] = field(
        default_factory=list
    )

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self.tools:
            raise ValueError(f"tool already registered: {name}")
        self.tools[name] = ToolSpec(name, description, input_schema, handler)

    def specs(self) -> list[dict[str, Any]]:
        return [spec.to_wire() for spec in self.tools.values()]

    def responses_specs(self) -> list[dict[str, Any]]:
        """Return the same registry in the Responses API function-tool shape."""
        return [
            {
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
                "strict": False,
            }
            for spec in self.tools.values()
        ]

    def begin_turn(self) -> None:
        self.calls_this_turn = 0
        self.fresh_high_frame = False

    def settle(self) -> str | None:
        """Stop anything running in the background; return notes for the model, if any."""
        notes = [note for hook in self.settle_hooks if (note := hook())]
        return " ".join(notes) or None

    def progress(self) -> str | None:
        """News about background play for the turn-start message, without stopping it."""
        notes = [note for hook in self.progress_hooks if (note := hook())]
        return " ".join(notes) or None

    def dispatch(self, name: str, arguments: Any) -> dict[str, Any]:
        """Run a tool and shape the result as a `DynamicToolCallResponse`.

        Never raises: failures are reported to the model as `success: False` with a message so
        it can recover on its own.
        """
        args = arguments if isinstance(arguments, dict) else {}
        started = time.monotonic()
        self.calls_this_turn += 1
        try:
            if (
                self.max_calls_per_turn is not None
                and self.calls_this_turn > self.max_calls_per_turn
            ):
                raise ToolBudgetExceeded(
                    f"Tool budget for this turn ({self.max_calls_per_turn} calls) is spent. "
                    "Stop calling tools and end your turn with a one-line status."
                )
            spec = self.tools.get(name)
            if spec is None:
                raise KeyError(f"unknown tool: {name}")
            items = spec.handler(args)
            response = {"success": True, "contentItems": items}
        except Exception as exc:  # noqa: BLE001 - surfaced to the model on purpose
            log.warning("tool %s failed: %s", name, exc)
            response = {
                "success": False,
                "contentItems": [text_item(f"{type(exc).__name__}: {exc}")],
            }
        duration_ms = (time.monotonic() - started) * 1000
        for observer in self.observers:
            observer(name, args, response, duration_ms)
        return response


# --- It Takes Two action set --------------------------------------------------------------

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

# "end" (wait for the chunk to finish) was offered too, but Sol chose it on every call, which
# turned pipelining off. Short chunks return at the end anyway (see `release_point`).
OBSERVE_MODES = ("early", "keyframes")
# Lead time: how long before a chunk's end its result goes back, i.e. how long the model takes
# to send the next `act`. Measured act-to-act within a turn and smoothed; these bound it.
LEAD_DEFAULT_MS = 3500
LEAD_MIN_MS = 1000
LEAD_MAX_MS = 6000
LEAD_SMOOTHING = 0.3  # weight of the newest gap
IDLE_REPORT_MS = 200  # report the moving share when the model stood still at least this long


@dataclass
class LeadTimer:
    """Smoothed estimate of the model's act-to-act planning time."""

    ms: float = LEAD_DEFAULT_MS
    returned_at: float | None = None

    def arrived(self) -> None:
        """Called when an `act` arrives; samples the gap since the previous `act` returned."""
        if self.returned_at is not None:
            gap = (time.monotonic() - self.returned_at) * 1000
            blended = (1 - LEAD_SMOOTHING) * self.ms + LEAD_SMOOTHING * gap
            self.ms = min(LEAD_MAX_MS, max(LEAD_MIN_MS, blended))
        self.returned_at = None

    def returned(self) -> None:
        self.returned_at = time.monotonic()

    def interrupt(self) -> None:
        """Another tool or a turn boundary sat between two acts: skip that gap."""
        self.returned_at = None


@dataclass(eq=False)
class _Played:
    """A submitted chunk plus what the model has been told about it."""

    job: Job
    steps: list[dict[str, Any]]
    intent: str
    lead_ms: float
    told_outcome: bool = False
    told_carry: bool = False
    logged: bool = False


def build_game_tools(
    io: GameIO,
    *,
    frame_after_action: bool = True,
    keyframes: int = 2,
    max_chunk_ms: int = MAX_CHUNK_MS,
    screen_half: ScreenHalf = "left",
    frame_encoder: ModelFrameEncoder | None = None,
    on_frame: Callable[[Image, str], object] | None = None,
    on_observation: Callable[[ModelFrame, str], object] | None = None,
    on_say: Callable[[str], None] | None = None,
    on_chunk: Callable[[dict[str, Any]], None] | None = None,
    max_calls_per_turn: int | None = None,
    instant_actions: bool = False,
) -> ToolRegistry:
    reg = ToolRegistry(max_calls_per_turn=max_calls_per_turn)
    encoder = frame_encoder or ModelFrameEncoder(half=screen_half)
    step_schema = deepcopy(_STEP_SCHEMA)
    for timing in ("ms", "hold_ms", "gap_ms"):
        step_schema["properties"][timing]["maximum"] = max_chunk_ms
    plain_step = deepcopy(step_schema)
    step_schema["properties"]["skill"]["enum"].append(REPEAT)
    step_schema["properties"]["times"] = {"type": "integer", "minimum": 2, "maximum": MAX_REPEAT}
    step_schema["properties"]["steps"] = {"type": "array", "items": plain_step, "minItems": 1}

    def frame(png: Image, reason: str, detail: FrameDetail = "low") -> ContentItem:
        if on_frame is not None:
            on_frame(png, reason)
        model_frame = encoder.encode(png, detail=detail)
        if on_observation is not None:
            on_observation(model_frame, reason)
        return image_item(model_frame)

    def new_pad() -> PadThread:
        return PadThread(io, screen_half, instant=instant_actions)

    pad = new_pad()
    lead = LeadTimer()
    last: _Played | None = None
    # `pad.epoch` as of the newest frame the model has. A chunk stopped early raises the epoch,
    # so a plan made from an older frame assumed something that did not happen.
    seen_epoch = 0

    def clear_task(why: str) -> str:
        reg.current_task = None
        return f"The scene changed ({why}), so your task is cleared: define it again."

    def stop_line(p: _Played, label: str) -> str:
        assert p.job.result is not None and p.job.result.stopped is not None
        i, cond = p.job.result.stopped
        return (
            f"{label} stopped early: step {i} ({p.steps[i].get('skill')}) ended on `{cond}` after "
            f"{p.job.result.elapsed_ms} ms; skipped {len(p.steps) - i - 1} later steps "
            f"({p.intent})."
        )

    def describe(p: _Played | None, *, live: bool = False) -> list[str]:
        """What the model has not been told yet about chunk `p`, oldest news first."""
        if p is None:
            return []
        job, out = p.job, []
        if not p.told_outcome and job.status in ("done", "stopped", "error"):
            p.told_outcome = True
            if job.status == "done":
                out.append(f"Your previous chunk finished all its steps ({p.intent}).")
            elif job.status == "error":
                out.append(f"Your previous chunk failed: {job.error} ({p.intent}).")
            else:
                out.append(stop_line(p, "Your previous chunk"))
                if job.result and job.result.stopped and job.result.stopped[1] == "cut":
                    out.append(clear_task("cut"))
        elif live and job.status in ("queued", "playing"):
            left = job.total_ms
            if job.started is not None:
                left -= round((time.monotonic() - job.started) * 1000)
            out.append(
                f"Your last chunk is still playing ({p.intent}): about {max(0, left)} ms left."
            )
        if not p.told_carry and job.carry_reason is not None and job.over.is_set():
            p.told_carry = True
            out.append(
                f"You kept moving for {job.carry_ms} ms after your previous chunk "
                f"(ended: {job.carry_reason})."
            )
            if job.carry_reason == "cut":
                out.append(clear_task("cut"))
        elif live and job.carrying:
            out.append("You are still running from keep_moving until your next chunk starts.")
        return out

    def log_chunk(p: _Played | None) -> None:
        if p is None or p.logged or on_chunk is None or not p.job.over.is_set():
            return
        p.logged = True
        job, result = p.job, p.job.result
        on_chunk(
            {
                "intent": p.intent,
                "status": job.status,
                "planned_ms": job.total_ms,
                "elapsed_ms": result.elapsed_ms if result else 0,
                "stopped": list(result.stopped) if result and result.stopped else None,
                "release_ms": job.frame_ms,
                "idle_ms": job.idle_ms,
                "carry_ms": job.carry_ms,
                "carry_reason": job.carry_reason,
                "lead_ms": round(p.lead_ms),
            }
        )

    def capture_now(reason: str) -> list[ContentItem]:
        if not frame_after_action:
            return []
        return [text_item("current frame"), frame(grab(io), reason)]

    def settle() -> str | None:
        """Stop the pad (end of play): halt any chunk or carry, leave the pad neutral."""
        nonlocal pad, seen_epoch
        pad.stop()
        note = " ".join(describe(last)) or None
        log_chunk(last)
        pad, seen_epoch = new_pad(), 0
        return note

    def progress() -> str | None:
        """Turn start: report chunk news without waiting; the caller captures right after."""
        nonlocal seen_epoch
        lead.interrupt()
        epoch = pad.epoch
        note = " ".join(describe(last, live=True)) or None
        seen_epoch = epoch
        return note

    def look_at_screen(a: dict[str, Any]) -> list[ContentItem]:
        nonlocal seen_epoch
        del a
        lead.interrupt()
        if reg.fresh_high_frame:
            raise ValueError(
                "You already have a high-detail frame and nothing has happened since. Act on "
                "it; your next `act` returns a new frame."
            )
        epoch = pad.epoch
        notes = [text_item(n) for n in describe(last, live=True)]
        reg.fresh_high_frame = True
        items = [
            *notes,
            text_item(f"high-detail frame captured at {time.strftime('%H:%M:%S')}"),
            frame(grab(io), "look (high)", "high"),
        ]
        seen_epoch = epoch
        return items

    def commit_task(a: dict[str, Any]) -> str:
        """Validate the task switch before anything is pressed; return a status line."""
        task = " ".join(str(a.get("task", "")).split())
        if not task:
            raise ValueError("`task` is required: the one task you are working on.")
        current = reg.current_task
        if current is None:
            reg.current_task = task
            return f"task: {task}"
        if task.casefold() == current.casefold():
            return f"task: {current}"
        outcome = a.get("previous_task")
        if outcome not in ("done", "blocked"):
            raise ValueError(
                f"Your current task is still {current!r}. Finish it first: repeat that exact "
                "`task` and keep working on it. Only if the frame shows it is complete, or it "
                'is impossible right now, switch tasks and set `previous_task` to "done" or '
                '"blocked". Nothing was pressed.'
            )
        reg.current_task = task
        return f"task {current!r} {outcome}; new task: {task}"

    def rejected(prev: _Played | None, intent: str) -> list[ContentItem]:
        """The chunk before this one stopped early; this one was planned assuming it would not."""
        nonlocal seen_epoch
        epoch = pad.epoch
        items = [text_item(n) for n in describe(prev)]
        items.append(
            text_item(
                f"NOT PLAYED ({intent}): your previous chunk did not finish as planned, so this "
                "chunk no longer fits. Nothing was pressed. Plan again from this frame."
            )
        )
        items += capture_now(f"rejected: {intent}")
        seen_epoch = epoch
        lead.returned()
        return items

    def act(a: dict[str, Any]) -> list[ContentItem]:
        nonlocal last, seen_epoch
        lead.arrived()
        steps = a.get("steps") or []
        intent = str(a.get("intent", "")).strip()
        observe = a.get("observe", "early")
        if observe not in OBSERVE_MODES:
            raise ValueError(
                'observe can only be "keyframes" (or omit it); short chunks already return '
                "their end frame. Nothing was pressed."
            )
        segments = compile_chunk(steps, max_chunk_ms)
        said = " ".join(str(a.get("say", "")).split())
        if (
            observe != "keyframes"
            and not said
            and all(step.get("skill") == "look" for _, _, step in expand_repeats(steps))
        ):
            raise ValueError(
                "A chunk of only camera turns spends a whole planning round to play a fraction "
                "of a second. Add the move that follows the turn (look, then run or jump). To "
                'scan around, use observe: "keyframes" to see several views. Nothing was pressed.'
            )
        keep_moving = a.get("keep_moving") is True
        if keep_moving and (steps[-1].get("skill") != "run" or segments[-1].state.left == (0, 0)):
            raise ValueError("keep_moving needs the last step to be a run with a direction")
        task_line = commit_task(a)
        reg.fresh_high_frame = False
        said_items = []
        if said:
            if on_say is not None:
                on_say(said)
            said_items = [text_item("said to your co-op partner: " + said)]
        prev = last
        if pad.epoch != seen_epoch:
            return [*rejected(prev, intent), *said_items]
        total = sum(s.ms for s in segments)
        release = (
            release_point(total, lead.ms)
            if observe == "early" and not instant_actions and frame_after_action
            else None
        )
        job = pad.submit(
            Job(
                segments,
                keyframes=keyframes if observe == "keyframes" else 0,
                release_ms=release,
                capture=frame_after_action,
                keep_moving=keep_moving,
                epoch=seen_epoch,
            )
        )
        cur = last = _Played(job, steps, intent, lead.ms)
        job.ready.wait()
        if job.status == "cancelled":
            cur.told_outcome = True
            log_chunk(prev)
            log_chunk(cur)
            return [*rejected(prev, intent), *said_items]
        items = [text_item(n) for n in describe(prev)]
        log_chunk(prev)
        if job.status == "error":
            assert job.error is not None
            cur.told_outcome = True
            log_chunk(cur)
            raise job.error
        seen_epoch = job.frame_epoch
        result = job.result
        if prev is not None and job.idle_ms >= IDLE_REPORT_MS:
            played = result.elapsed_ms if result and job.frame_ms is None else total
            share = round(100 * played / (played + job.idle_ms))
            items.append(
                text_item(
                    f"You stood still for {job.idle_ms} ms planning, then played {played} ms "
                    f"({share}% of the time moving). Longer chunks waste less."
                )
            )
        if job.frame_ms is not None:
            step, left = job.remaining_from(job.frame_ms)
            then = ", then you keep running until your next chunk starts" if keep_moving else ""
            summary = (
                f"playing: this frame is {job.frame_ms} ms into the {total} ms chunk; step "
                f"{step} ({steps[step].get('skill')}) onward is still playing, about {left} ms "
                f"more{then} ({intent}). Plan your next chunk from where these steps will leave "
                "you; it starts as soon as this one ends."
            )
        else:
            cur.told_outcome = True
            assert result is not None
            if result.stopped is None:
                summary = f"done: {len(steps)} steps, {result.elapsed_ms} ms ({intent})"
            else:
                summary = stop_line(cur, "this chunk")
            if result.stopped is not None and result.stopped[1] == "cut":
                # A respawn, checkpoint reload, or cutscene can change what needs doing.
                reg.current_task = None
                task_line = (
                    "task cleared: the scene changed. Define your task again from this frame."
                )
        items += [text_item(summary), text_item(task_line), *said_items]
        for t, png in result.shots if result else []:
            items += [text_item(f"keyframe at {t} ms"), frame(png, f"keyframe {t}ms: {intent}")]
        if job.frame is not None:
            label = "end of chunk" if job.frame_ms is None else f"frame at {job.frame_ms} ms"
            items += [text_item(label), frame(job.frame, f"after: {intent}")]
        if job.frame_ms is None and job.carrying:
            items.append(text_item("still running: you keep moving while you plan."))
        lead.returned()
        return items

    reg.register(
        "look_at_screen",
        "Capture a high-detail frame of your half to read small prompts, icons, or text that "
        "are unclear in your latest frame. Not for navigation: every turn and every `act` "
        "already returns a fresh frame. At most once between actions.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        look_at_screen,
    )
    reg.settle_hooks.append(settle)
    reg.progress_hooks.append(progress)
    reg.register(
        "act",
        _ACT_DESCRIPTION.format(
            max_ms=max_chunk_ms,
            n=keyframes,
            carry_s=CARRY_MAX_MS // 1000,
            lead_s=round(LEAD_DEFAULT_MS / 1000, 1),
            long_s=round(LEAD_DEFAULT_MS / 1000 / (1 - MIN_RELEASE_FRACTION)),
            max_repeat=MAX_REPEAT,
        ),
        {
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
        },
        act,
    )
    return reg
