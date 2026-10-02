"""Jev: choose the next skill and its fields from the scene, by typed questions."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from it_takes_me.jev.actions import STEERED, ActionChoice, action_questions, available_skills
from it_takes_me.jev.state import Scene, Thing

EXPLORE = "explore"


def describe_thing(thing: Thing) -> str:
    return (
        f"{thing.kind}: {thing.label} ({thing.distance}, {thing.direction_deg:+d} deg, "
        f"{thing.height})"
    )


def goal_options(scene: Scene) -> dict[str, str]:
    """Every thing Luna described, unfiltered, plus exploring when none is worth pursuing."""
    options = {f"t{i}": describe_thing(thing) for i, thing in enumerate(scene.things, start=1)}
    options[EXPLORE] = "Nothing here to work toward: look around or move on to find a marker."
    return options


def briefing(character: str, partner: str) -> dict[str, str]:
    """Fixed context sent with every decision."""
    return {
        "you_are": f"{character} in the split-screen co-op game It Takes Two. A human plays "
        f"{partner}. You choose one controller input at a time from the scene description.",
        "controls": "Directions are relative to the camera: forward is away from it. Turning "
        "the camera with look changes which way every later direction points. Moving sideways "
        "makes the camera swing and curves the path, so on ledges and narrow surfaces face "
        "along them and walk forward. Jumps need a direction to travel and full speed.",
        "progress": "The game shows what to do with markers: a yellow circle on an object is "
        "the next thing to use (interact when its button prompt shows); an objective marker "
        "shows the general direction of the next area; the partner marker only shows where "
        f"{partner} is. Finish the current goal before choosing another, and leave objects "
        "that the goal does not need.",
    }


@dataclass(slots=True)
class Decision:
    action: ActionChoice
    goal: str
    goal_label: str
    task_done: float
    last_action_worked: float
    need_partner: float
    answers: dict[str, Any]
    model: str
    latency_ms: int
    usage: dict[str, Any] | None = field(default=None)


class Decider:
    def __init__(self, client: Any, model: str, *, character: str, partner: str) -> None:
        self._client = client
        self._model = model
        self._briefing = briefing(character, partner)
        self._partner = partner

    def questions(self, scene: Scene, current_goal: str | None) -> dict[str, Any]:
        questions = action_questions(available_skills(scene))
        questions["goal"] = {
            "type": "choice",
            "instructions": "What is the character working toward now? Keep the current goal "
            "unless it is done or no longer visible.",
            "criteria": goal_options(scene),
        }
        if current_goal is not None:
            questions["task_done"] = {
                "type": "noul",
                "instructions": "Is the current goal complete?",
                "criteria": {
                    "true": f"The current goal ({current_goal}) is done or no longer needed.",
                    "false": "The current goal still needs work.",
                },
            }
        questions["last_action_worked"] = {
            "type": "noul",
            "instructions": "Comparing the previous and current scene, did the last action "
            "do what it was meant to?",
            "criteria": {
                "true": "The scene changed the way the last action intended.",
                "false": "It missed, fell, got stuck, or nothing changed.",
            },
        }
        questions["need_partner"] = {
            "type": "noul",
            "instructions": f"Does progress here need {self._partner} to do something?",
            "criteria": {
                "true": f"Something only {self._partner} can do, or both players together.",
                "false": "The character can progress alone.",
            },
        }
        return questions

    def decide(
        self,
        *,
        scene: Scene,
        previous_scene: Scene | None,
        current_goal: str | None,
        history: list[dict[str, Any]],
        partner_says: list[str],
    ) -> Decision:
        state = {
            **self._briefing,
            "current_scene": scene.model_dump(),
            "previous_scene": previous_scene.model_dump() if previous_scene else None,
            "current_goal": current_goal,
            "recent_decisions": history,
            "partner_says": partner_says,
        }
        questions = self.questions(scene, current_goal)
        started = time.monotonic()
        response = self._client.decide(self._model, state, questions)
        latency_ms = round((time.monotonic() - started) * 1000)
        answers = response["answers"]

        def choice(key: str) -> str:
            return answers[key]["choice"]

        def noul(key: str) -> float:
            return float(answers[key]["noul"])

        goal = choice("goal")
        skill = choice("skill")
        direction = choice("dir")
        if skill in STEERED and direction == "none":
            # A move with no direction does nothing: take Jev's most likely real direction.
            probabilities = answers["dir"].get("probabilities") or {}
            ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])
            direction = next((d for d, _ in ranked if d != "none"), "forward")
        return Decision(
            action=ActionChoice(
                skill=skill,
                dir=direction,
                duration=choice("duration"),
                speed=choice("speed"),
                look=choice("look"),
                trigger=choice("trigger"),
            ),
            goal=goal,
            goal_label=questions["goal"]["criteria"][goal],
            task_done=noul("task_done") if "task_done" in answers else 1.0,
            last_action_worked=noul("last_action_worked"),
            need_partner=noul("need_partner"),
            answers=answers,
            model=response.get("model", self._model),
            latency_ms=latency_ms,
            usage=response.get("usage"),
        )
