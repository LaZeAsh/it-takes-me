"""Run Claude (Fable or Opus) as your It Takes Two co-op partner, on your Claude subscription.

The bundled Claude Code uses the same login as the `claude` CLI (sign in there once), then
run:  uv run python opus.py
Everything else (monitor, budgets, frames, character) comes from the hyperparameters in run.py.
"""

from __future__ import annotations

import run

MODEL = "claude-fable-5-1"  # or "claude-opus-5-5" (it_takes_me.opus.runtime.OPUS_MODEL)
REASONING_EFFORT = "low"  # low, medium, high, xhigh or max

if __name__ == "__main__":
    run.RUNTIME = "claude"
    run.MODEL = MODEL
    run.REASONING_EFFORT = REASONING_EFFORT
    run.SERVICE_TIER = None
    run.main()
