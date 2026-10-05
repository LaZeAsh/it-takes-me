"""
Compact text form of a chunk's steps, so the model writes `run f 1200; jump f; run f 300` instead
of a JSON list. Every output token costs planning time; JSON keys were about half of each `act`.

    steps := step (";" step)*
    step  := <times>x(steps) | skill token*
    token := f|b|l|r|fl|fr|bl|br|none   direction (look: l|r|u|d)
           | @<deg>                     heading, 0 forward, 90 right
           | <int>                      ms
           | h<int> | g<int>            hold_ms | gap_ms
           | s<num>                     speed (look: look_speed)
           | sprint | stuck | cut       run flags
           | L<x>,<y> | R<x>,<y>        raw sticks
           | A|B|X|Y|LB|RB|LT|RT|...    button (press, ability) or held buttons (raw)
"""

from __future__ import annotations

import re
from typing import Any

from .chunks import CONDITIONS, REPEAT, SKILLS
from .io import Button

ALIASES = {
    "djump": "double_jump",
    "jdash": "jump_dash",
    "pound": "ground_pound",
    "locate": "locate_partner",
    "skip": "skip_cutscene",
}
_DIRS = {
    "f": "forward",
    "b": "back",
    "l": "left",
    "r": "right",
    "fl": "forward-left",
    "fr": "forward-right",
    "bl": "back-left",
    "br": "back-right",
    "none": "none",
}
_LOOKS = {"l": "left", "r": "right", "u": "up", "d": "down"}
_BUTTONS = {b.value for b in Button}
_REPEAT = re.compile(r"^(\d+)\s*x\s*\((.*)\)$", re.DOTALL)
_STICK = re.compile(r"^([LR])(-?\d*\.?\d+),(-?\d*\.?\d+)$")
_NUMBER = re.compile(r"^-?\d*\.?\d+$")


def _split(text: str) -> list[str]:
    """Split on `;` outside parentheses."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unmatched ')' in {text!r}")
        elif ch == ";" and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    if depth:
        raise ValueError(f"unmatched '(' in {text!r}")
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _int(token: str, value: str) -> int:
    if not value.isdigit():
        raise ValueError(f"{token!r} needs a whole number of ms")
    return int(value)


def _step(text: str) -> dict[str, Any]:
    tokens = text.split()
    skill = ALIASES.get(tokens[0].lower(), tokens[0].lower())
    if skill not in SKILLS:
        raise ValueError(f"unknown skill {tokens[0]!r}; use one of {[*SKILLS, *ALIASES]}")
    step: dict[str, Any] = {"skill": skill}
    for token in tokens[1:]:
        # Directions and flags are lowercase, buttons and sticks uppercase: `b` is back, `B` the
        # button.
        if skill == "look" and token in _LOOKS:
            step["look"] = _LOOKS[token]
        elif token in _DIRS:
            step["dir"] = _DIRS[token]
        elif token in _BUTTONS:
            if skill == "raw":
                step.setdefault("buttons", []).append(token)
            else:
                step["button"] = token
        elif token.startswith("@") and _NUMBER.match(token[1:]):
            step["heading"] = float(token[1:])
        elif token.isdigit():
            step["ms"] = int(token)
        elif token[0] in "hg" and len(token) > 1:
            step["hold_ms" if token[0] == "h" else "gap_ms"] = _int(token, token[1:])
        elif token[0] == "s" and _NUMBER.match(token[1:]):
            step["look_speed" if skill == "look" else "speed"] = float(token[1:])
        elif token == "sprint":
            step["sprint"] = True
        elif token in CONDITIONS:
            step.setdefault("until", []).append(token)
        elif m := _STICK.match(token):
            step["left" if m[1] == "L" else "right"] = [float(m[2]), float(m[3])]
        else:
            raise ValueError(f"unknown token {token!r}")
    return step


def parse_steps(text: str) -> list[dict[str, Any]]:
    """Turn the compact text into the step dicts `compile_chunk` takes."""
    steps: list[dict[str, Any]] = []
    for part in _split(text):
        if m := _REPEAT.match(part):
            inner = [_parse_one(p) for p in _split(m[2])]
            steps.append({"skill": REPEAT, "times": int(m[1]), "steps": inner})
        else:
            steps.append(_parse_one(part))
    if not steps:
        raise ValueError("steps is empty")
    return steps


def _parse_one(text: str) -> dict[str, Any]:
    if _REPEAT.match(text):
        raise ValueError(f"repeats cannot nest: {text!r}")
    try:
        return _step(text)
    except ValueError as exc:
        raise ValueError(f"in step {text!r}: {exc}") from exc
