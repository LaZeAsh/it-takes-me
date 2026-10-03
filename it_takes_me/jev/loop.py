"""The Luna + Jev play loop: capture, describe (Luna), decide (Jev), act, record."""

from __future__ import annotations

import queue
import re
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openai_codex import Codex
from rich.console import Console

from it_takes_me.game.io import GameIO, grab
from it_takes_me.game.playback import play_chunk
from it_takes_me.game.tuning import MAX_CHUNK_MS
from it_takes_me.jev.actions import build_step, compile_step_safely, run_ms
from it_takes_me.jev.config import JevSettings
from it_takes_me.jev.decide import EXPLORE, Decider, Decision, goal_options
from it_takes_me.jev.openrouter import DecisionsClient, api_key
from it_takes_me.jev.perceive import Perceiver
from it_takes_me.jev.state import Scene, Thing
from it_takes_me.recording import RunRecorder
from it_takes_me.vision import ModelFrameEncoder

TASK_DONE_AT = 0.5
NEED_PARTNER_AT = 0.7


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def same_goal(goal: str, options: dict[str, str]) -> str | None:
    """The option that best matches an earlier goal label, if one still looks like it."""
    want = _words(goal)
    best, best_overlap = None, 0.5
    for key, label in options.items():
        have = _words(label)
        overlap = len(want & have) / max(len(want), 1)
        if overlap >= best_overlap:
            best, best_overlap = key, overlap
    return best


class JevPlayer:
    def __init__(
        self,
        *,
        io: GameIO,
        perceiver: Perceiver,
        decider: Decider,
        recorder: RunRecorder,
        settings: JevSettings,
        console: Console | None = None,
    ) -> None:
        self.io = io
        self.perceiver = perceiver
        self.decider = decider
        self.recorder = recorder
        self.settings = settings
        self.console = console or Console()
        self.encoder = ModelFrameEncoder(
            half=settings.screen_half,
            low_max_px=settings.frame_max_px,
            jpeg_quality=settings.frame_jpeg_quality,
        )
        self.goal: str | None = None
        self.history: list[dict[str, Any]] = []
        self.previous_scene: Scene | None = None
        self._hints: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()

    # -- human input --------------------------------------------------------------------------

    def start_stdin_listener(self) -> None:
        """Lines typed on the terminal reach Jev with the next decision. `/quit` stops."""

        def _reader() -> None:
            for line in sys.stdin:
                text = line.strip()
                if text in ("/quit", "/q"):
                    self._stop.set()
                    return
                if text:
                    self._hints.put(text)

        threading.Thread(target=_reader, daemon=True, name="stdin-hints").start()

    def _drain_hints(self) -> list[str]:
        hints: list[str] = []
        while not self._hints.empty():
            hints.append(self._hints.get_nowait())
        return hints

    # -- loop ---------------------------------------------------------------------------------

    def play(self) -> None:
        step_no = 0
        while not self._stop.is_set():
            if self.settings.max_steps is not None and step_no >= self.settings.max_steps:
                return
            step_no = self.look_and_act(step_no)

    def look_and_act(self, step_no: int) -> int:
        """One Luna description, then up to `skills_per_look` Jev decisions. Returns the
        updated step count."""
        png = grab(self.io)
        self.recorder.frame(png, reason=f"step {step_no + 1}")
        frame = self.encoder.encode(png)
        image_path = self.recorder.observation(
            frame.data,
            reason=f"step {step_no + 1}",
            media_type=frame.media_type,
            width=frame.width,
            height=frame.height,
            detail=frame.detail,
        )
        perception = self.perceiver.describe(image_path)
        scene = perception.scene
        self.recorder.event(
            "perception",
            model=perception.model,
            latency_ms=perception.latency_ms,
            usage=perception.usage,
            scene=scene.model_dump(),
        )
        self.console.print(
            f"[cyan]luna[/cyan] {scene.scene}, {len(scene.things)} things "
            f"({perception.latency_ms} ms)"
        )
        for _ in range(self.settings.skills_per_look):
            if self._stop.is_set() or (
                self.settings.max_steps is not None and step_no >= self.settings.max_steps
            ):
                break
            step_no += 1
            stopped = self._decide_and_act(step_no, scene)
            if stopped:
                break
        self.previous_scene = scene
        return step_no

    def _decide_and_act(self, step_no: int, scene: Scene) -> bool:
        """Returns True when the screen changed abruptly, so the scene is stale."""
        hints = self._drain_hints()
        decision = self.decider.decide(
            scene=scene,
            previous_scene=self.previous_scene,
            current_goal=self.goal,
            history=self.history[-self.settings.history_len :],
            partner_says=hints,
        )
        self._update_goal(decision, scene)
        step, segments, note = compile_step_safely(
            build_step(decision.action, run_ms=run_ms(scene, self._goal_thing(scene))),
            MAX_CHUNK_MS,
        )
        result = play_chunk(
            self.io,
            segments,
            half=self.settings.screen_half,
            instant=self.settings.instant_actions,
        )
        outcome = "done" if result.stopped is None else f"stopped on {result.stopped[1]}"
        self.recorder.event(
            "decision",
            step=step_no,
            model=decision.model,
            latency_ms=decision.latency_ms,
            usage=decision.usage,
            answers=decision.answers,
            choice=asdict(decision.action),
            goal=self.goal,
            hints=hints,
        )
        self.recorder.event(
            "act", step=step_no, input=step, note=note, outcome=outcome, ms=result.elapsed_ms
        )
        self.history.append(
            {"step": step, "goal": self.goal, "outcome": outcome, "elapsed_ms": result.elapsed_ms}
        )
        confidence = decision.answers.get("skill", {}).get("confidence")
        self.console.print(
            f"[magenta]jev[/magenta] #{step_no} {step} goal={self.goal!r} "
            f"conf={confidence} ({decision.latency_ms} ms) -> {outcome}"
            + (f" [yellow]{note}[/yellow]" if note else "")
        )
        if decision.need_partner >= NEED_PARTNER_AT:
            self.console.print(
                f"[bold magenta]{self.settings.character}:[/bold magenta] "
                f"{self.settings.partner}, I need your help with: {self.goal}"
            )
        return result.stopped is not None and result.stopped[1] == "cut"

    def _goal_thing(self, scene: Scene) -> Thing | None:
        """The thing in this scene that the current goal refers to, if any."""
        if self.goal is None:
            return None
        key = same_goal(self.goal, goal_options(scene))
        if key is None or key == EXPLORE:
            return None
        return scene.things[int(key[1:]) - 1]

    def _update_goal(self, decision: Decision, scene: Scene) -> None:
        """Keep the current goal until Jev judges it done or it is no longer visible."""
        if (
            self.goal is not None
            and decision.task_done < TASK_DONE_AT
            and same_goal(self.goal, goal_options(scene)) is not None
        ):
            return
        self.goal = None if decision.goal == EXPLORE else decision.goal_label


def play_session(game: GameIO, settings: JevSettings, *, console: Console | None = None) -> Path:
    console = console or Console()
    key = api_key()
    recorder = RunRecorder(settings.runs_dir)
    console.print(f"recording to {recorder.dir}")
    recorder.event(
        "session",
        technique="luna+jev",
        perceiver=settings.perceiver_model,
        perceiver_effort=settings.perceiver_effort,
        perceiver_service_tier=settings.perceiver_service_tier,
        decider=settings.decider_model,
        character=settings.character,
    )
    try:
        with Codex() as codex:
            player = JevPlayer(
                io=game,
                perceiver=Perceiver(
                    codex,
                    settings.perceiver_model,
                    character=settings.character,
                    partner=settings.partner,
                    effort=settings.perceiver_effort,
                    service_tier=settings.perceiver_service_tier,
                ),
                decider=Decider(
                    DecisionsClient(key),
                    settings.decider_model,
                    character=settings.character,
                    partner=settings.partner,
                ),
                recorder=recorder,
                settings=settings,
                console=console,
            )
            player.start_stdin_listener()
            player.play()
    finally:
        recorder.close()
    return recorder.dir
