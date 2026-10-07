from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from it_takes_me.game.io import NEUTRAL, PadState
from it_takes_me.jev.actions import (
    DIRECTIONS,
    SKILLS,
    ActionChoice,
    action_questions,
    available_skills,
    build_step,
    compile_step_safely,
    run_ms,
)
from it_takes_me.jev.config import JevSettings
from it_takes_me.jev.decide import EXPLORE, Decider, goal_options
from it_takes_me.jev.loop import JevPlayer, same_goal
from it_takes_me.jev.perceive import Perception
from it_takes_me.jev.state import Character, Scene, Thing
from it_takes_me.recording import RunRecorder
from PIL import Image
from rich.console import Console


def choice(**fields: str) -> ActionChoice:
    base = {
        "skill": "run",
        "dir": "forward",
        "duration": "ms_600",
        "speed": "run",
        "look": "left",
        "trigger": "RT",
    }
    return ActionChoice(**{**base, **fields})


def step_for(action: ActionChoice) -> dict[str, Any]:
    return build_step(action, run_ms=1800)


def scene(*things: Thing, kind: str = "gameplay", edges: list[str] | None = None) -> Scene:
    return Scene(
        scene=kind,
        character=Character(
            visible=True, airborne=False, surface="floor", edges=edges or [], holding=None
        ),
        things=list(things),
        screen_text=[],
    )


FUSE = Thing(
    kind="yellow_circle",
    label="fuse with yellow circle",
    screen_x=0.4,
    screen_y=0.6,
    direction_deg=-10,
    distance="near",
    height="level",
)
HEXAGON = Thing(
    kind="objective_marker",
    label="white hexagon above boxes",
    screen_x=0.7,
    screen_y=0.1,
    direction_deg=25,
    distance="far",
    height="above",
)


class ActionSpaceTests(unittest.TestCase):
    def test_questions_are_static_choices_over_sols_skills(self) -> None:
        questions = action_questions()
        self.assertEqual(set(questions["skill"]["criteria"]), set(SKILLS))
        self.assertEqual(set(questions["dir"]["criteria"]), set(DIRECTIONS))
        self.assertNotIn("raw", SKILLS)
        self.assertNotIn("press", SKILLS)
        self.assertEqual(questions, action_questions())

    def test_build_step_keeps_only_fields_the_skill_uses(self) -> None:
        self.assertEqual(
            step_for(choice(skill="run", dir="left", duration="ms_1500", speed="walk")),
            {"skill": "run", "dir": "left", "speed": 0.4, "ms": 1800},
        )
        self.assertEqual(
            step_for(choice(skill="look", look="up", duration="ms_4000")),
            {"skill": "look", "look": "up", "ms": 1500},
        )
        self.assertEqual(
            step_for(choice(skill="interact", duration="ms_300")),
            {"skill": "interact", "ms": 300},
        )
        self.assertEqual(
            step_for(choice(skill="run", dir="none", speed="run")),
            {"skill": "run", "dir": "forward", "speed": 1.0, "ms": 1800},
        )
        self.assertEqual(
            step_for(choice(skill="ability", trigger="LT", duration="ms_1000")),
            {"skill": "ability", "button": "LT", "ms": 1000},
        )
        self.assertEqual(
            step_for(choice(skill="skip_cutscene", duration="ms_300")),
            {"skill": "skip_cutscene", "ms": 2000},
        )

    def test_jumps_use_their_airtime_unless_the_duration_is_longer(self) -> None:
        self.assertEqual(
            step_for(choice(skill="double_jump", duration="ms_300", speed="walk")),
            {"skill": "double_jump", "dir": "forward"},
        )
        self.assertEqual(
            step_for(choice(skill="jump", duration="ms_1000")),
            {"skill": "jump", "dir": "forward", "ms": 1000},
        )

    def test_runs_are_sized_from_goal_distance_and_capped_near_drops(self) -> None:
        self.assertEqual(run_ms(scene(HEXAGON), HEXAGON), 3500)
        self.assertEqual(run_ms(scene(FUSE), FUSE), 800)
        self.assertEqual(run_ms(scene(), None), 1500)
        self.assertEqual(run_ms(scene(HEXAGON, edges=["front"]), HEXAGON), 800)

    def test_unusable_skills_are_not_offered(self) -> None:
        self.assertNotIn("interact", available_skills(scene(HEXAGON)))
        self.assertIn("interact", available_skills(scene(FUSE)))
        self.assertNotIn("skip_cutscene", available_skills(scene(FUSE)))
        self.assertEqual(set(available_skills(scene(kind="cutscene"))), {"skip_cutscene"})

    def test_invalid_timing_falls_back_to_the_skill_default(self) -> None:
        step, segments, note = compile_step_safely(
            {"skill": "jump", "dir": "forward", "ms": 250}, 10_000
        )
        self.assertEqual(step, {"skill": "jump", "dir": "forward"})
        self.assertIn("default timing", note)
        self.assertEqual(sum(s.ms for s in segments), 650)


class DecisionTests(unittest.TestCase):
    def test_goal_options_list_every_thing_plus_explore(self) -> None:
        options = goal_options(scene(FUSE, HEXAGON))
        self.assertEqual(list(options), ["t1", "t2", EXPLORE])
        self.assertIn("fuse with yellow circle", options["t1"])

    def test_same_goal_matches_relabelled_things(self) -> None:
        options = goal_options(scene(FUSE, HEXAGON))
        self.assertEqual(same_goal("yellow_circle: fuse with yellow circle (near)", options), "t1")
        self.assertIsNone(same_goal("green door lever", options))

    def test_decider_parses_typed_answers(self) -> None:
        client = FakeDecisions([answers("double_jump", goal="t2")])
        decider = Decider(client, "jev", character="May", partner="Cody")
        decision = decider.decide(
            scene=scene(FUSE, HEXAGON),
            previous_scene=None,
            current_goal=None,
            history=[],
            partner_says=["go left"],
        )
        self.assertEqual(decision.action.skill, "double_jump")
        self.assertIn("white hexagon", decision.goal_label)
        state, questions = client.calls[0]
        self.assertEqual(state["partner_says"], ["go left"])
        self.assertEqual(set(questions["goal"]["criteria"]), {"t1", "t2", EXPLORE})

    def test_a_move_without_direction_takes_the_next_likeliest_direction(self) -> None:
        response = answers("run")
        response["answers"]["dir"] = {
            "type": "choice",
            "choice": "none",
            "probabilities": {"none": 0.5, "forward-left": 0.3, "forward": 0.2},
        }
        decider = Decider(FakeDecisions([response]), "jev", character="May", partner="Cody")
        decision = decider.decide(
            scene=scene(FUSE), previous_scene=None, current_goal=None, history=[], partner_says=[]
        )
        self.assertEqual(decision.action.dir, "forward-left")


def answers(skill: str, *, goal: str = "t1", task_done: float = 0.1) -> dict[str, Any]:
    picked = {
        "skill": skill,
        "dir": "forward",
        "duration": "ms_150",
        "speed": "run",
        "look": "left",
        "trigger": "RT",
        "goal": goal,
    }
    result: dict[str, Any] = {
        key: {"type": "choice", "choice": value, "confidence": 0.5} for key, value in picked.items()
    }
    for key, value in (("task_done", task_done), ("last_action_worked", 0.8), ("need_partner", 0)):
        result[key] = {"type": "noul", "noul": value}
    return {"answers": result, "model": "typesafe/jev-test"}


class FakeDecisions:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def decide(self, model: str, state: dict[str, Any], questions: dict[str, Any]) -> dict:
        self.calls.append((state, questions))
        return self.responses[len(self.calls) - 1]


class FakePerceiver:
    def __init__(self, scenes: list[Scene]) -> None:
        self.scenes = scenes
        self.frames = 0

    def describe(self, frame: Any) -> Perception:
        self.frames += 1
        return Perception(self.scenes[self.frames - 1], "luna-test", 5, None)


class FakeGame:
    def __init__(self) -> None:
        self.inputs: list[PadState] = []
        png = io.BytesIO()
        Image.new("RGB", (64, 36)).save(png, "PNG")
        self.png = png.getvalue()

    def capture(self) -> bytes:
        return self.png

    def snapshot(self, size: tuple[int, int]) -> Image.Image:
        return Image.new("L", size, 0)

    def set_pad(self, state: PadState) -> None:
        self.inputs.append(state)

    def close(self) -> None:
        pass


class LoopTests(unittest.TestCase):
    def test_loop_describes_decides_acts_and_keeps_its_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = JevSettings(runs_dir=Path(tmp), max_steps=2, instant_actions=True)
            game = FakeGame()
            client = FakeDecisions([answers("run", goal="t1"), answers("interact", goal="t2")])
            recorder = RunRecorder(settings.runs_dir)
            player = JevPlayer(
                io=game,
                perceiver=FakePerceiver([scene(FUSE, HEXAGON), scene(FUSE, HEXAGON)]),
                decider=Decider(client, "jev", character="May", partner="Cody"),
                recorder=recorder,
                settings=settings,
                console=Console(file=io.StringIO()),
            )
            player.play()
            recorder.close()
            events = [
                json.loads(line)
                for line in (recorder.dir / "events.jsonl").read_text().splitlines()
            ]

        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds.count("perception"), 2)
        self.assertEqual(kinds.count("decision"), 2)
        acts = [e for e in events if e["kind"] == "act"]
        self.assertEqual(acts[0]["input"]["skill"], "run")
        self.assertEqual(acts[1]["input"]["skill"], "interact")
        # Task not done and the fuse is still visible, so the goal stays on the fuse.
        self.assertIn("fuse", player.goal)
        self.assertEqual(game.inputs[-1], NEUTRAL)
        # The second decision sees the first one in its history and the previous scene.
        state = client.calls[1][0]
        self.assertEqual(state["recent_decisions"][0]["step"]["skill"], "run")
        self.assertIsNotNone(state["previous_scene"])


if __name__ == "__main__":
    unittest.main()
