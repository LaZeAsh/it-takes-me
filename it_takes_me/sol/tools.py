"""The It Takes Two tools (`act`, `look_at_screen`) wired on top of a `GameIO`."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PIL.Image import Image

from it_takes_me.game.chunks import compile_chunk, expand_repeats
from it_takes_me.game.io import GameIO, grab
from it_takes_me.game.playback import release_point
from it_takes_me.game.shorthand import parse_steps
from it_takes_me.game.tuning import (
    IDLE_REPORT_MS,
    LEAD_DEFAULT_MS,
    LEAD_MAX_MS,
    LEAD_MIN_MS,
    LEAD_SMOOTHING,
    MAX_CHUNK_MS,
)
from it_takes_me.sol.act_spec import act_description, act_schema
from it_takes_me.sol.pad_thread import Job, PadThread
from it_takes_me.sol.registry import ContentItem, ToolRegistry, image_item, text_item
from it_takes_me.vision import FrameDetail, ModelFrame, ModelFrameEncoder, ScreenHalf

# "end" (wait for the chunk to finish) was offered too, but Sol chose it on every call, which
# turned pipelining off. Short chunks return at the end anyway (see `release_point`).
OBSERVE_MODES = ("early", "keyframes")


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
        current = reg.current_task
        if not task:
            if current is None:
                raise ValueError(
                    "You have no current task: set `task` to the one task you are working on. "
                    "Nothing was pressed."
                )
            return f"task: {current}"
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
        if isinstance(steps, str):
            text = " ".join(steps.split())
            steps = parse_steps(text)
        else:
            text = ""
        intent = str(a.get("intent", "")).strip() or text[:60]
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
        act_description(max_chunk_ms, keyframes),
        act_schema(max_chunk_ms),
        act,
    )
    return reg
