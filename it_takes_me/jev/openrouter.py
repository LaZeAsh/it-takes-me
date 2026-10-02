"""OpenRouter access for Jev (the Decisions API). Luna runs on Codex instead."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from dotenv import load_dotenv

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


def api_key() -> str:
    """Read OPENROUTER_API_KEY from the environment or `.env` (see `.example.env`)."""
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set; copy .example.env to .env and fill it in"
        )
    return key


class DecisionsClient:
    """Minimal client for `POST /api/alpha/decisions` (typed questions in, typed answers out)."""

    def __init__(self, key: str, *, timeout_s: float = 30.0) -> None:
        self._key = key
        self._timeout_s = timeout_s

    def decide(
        self, model: str, state: dict[str, Any], questions: dict[str, Any]
    ) -> dict[str, Any]:
        body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
        request = urllib.request.Request(
            DECISIONS_URL,
            body,
            {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"decisions request failed ({exc.code}): {detail}") from exc
