# it-takes-me

GPT-6 Astra plays one half of _It Takes Two_ while you play the other.

The agent talks to the model through the **Codex app-server** using the official
`openai-codex` Python SDK, so it runs on your ChatGPT/Codex subscription (no API key).
Game actions are exposed to the model as dynamic tools (`look_at_screen`, `move`,
`press`, ...) on one persistent thread.

## Setup

```bash
uv sync
```

Uses your existing Codex login (`~/.codex/auth.json`).

## Run

Hyperparameters (model, effort, character, budgets) are constants at the top of `run.py`.
On the Windows gaming PC with the game on screen:

```bash
uv run python run.py
```

Every session is recorded to `runs/<timestamp>/` (frames + `events.jsonl`).

The Windows backend captures the screen with `mss`; controller input (virtual Xbox pad) is
not wired up yet.
