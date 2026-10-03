# Luna + Jev player

A second technique for playing _It Takes Two_, separate from the Sol/Codex player
(`run.py`, `it_takes_me/sol/`). It shares only the game layer: capture and controller
(`it_takes_me/game/`), the action runner and its checks (`chunks.py`, `playback.py`), frame cropping
(`vision.py`), and recording (`recording.py`).

```bash
cp .example.env .env      # then set OPENROUTER_API_KEY (for Jev); Luna uses your Codex login
uv run python pad.py      # optional: keep one controller across runs
uv run python run_jev.py  # hyperparameters are constants at the top
```

Sessions are recorded to `runs/jev/<timestamp>/`.

## How a step works

1. **Capture** the controlled half of the screen.
2. **Luna** (`gpt-6-luna` on the local Codex subscription, Fast tier, `low` effort) describes
   the frame as a `Scene` (`state.py`), using a fresh ephemeral Codex thread per frame with the
   perceiver instructions as its base instructions and the `Scene` JSON schema as its output
   schema: scene type, the character's surface and nearby drops, up to
   8 things (markers first) with screen position, angle, distance, and height, and on-screen
   text. Luna never sees actions, goals, or the action space.
3. **Jev** (`~typesafe/jev-latest` via OpenRouter's Decisions API) gets the current and previous
   scene, the current goal, recent decisions, and anything typed in the terminal. In one request
   it answers typed questions:
   - `skill`: one of Sol's skills (`run`, `jump`, `double_jump`, `dash`, `jump_dash`,
     `ground_pound`, `interact`, `grapple`, `ability`, `look`, `locate_partner`,
     `skip_cutscene`). `raw` and `press` are left out: they need free-form input. There is
     no `wait`, and a `run` with direction `none` runs forward.
   - `dir`, `duration` (150 ms to 4 s), `speed` (walk/jog/run), `look`, `trigger`: the skill's
     fields as fixed options. Only the fields the chosen skill uses are applied.
   - `goal`: which thing Luna described to work toward (or explore); `task_done`,
     `last_action_worked`, `need_partner`: yes/no probabilities.
4. **Code** builds one step from the answers (`actions.py`) and runs it through the shared runner,
   with its timing and mid-air steering checks. A timing that fails the checks falls back to the
   skill's default and is logged.
5. **Record** a `perception`, `decision` (all answers with probabilities), and `act` event.

The goal is kept until Jev judges it done or it no longer appears in the scene. When
`need_partner` is high, the terminal shows a message for the partner.

`SKILLS_PER_LOOK` lets Jev pick several skills in a row from one Luna description, since Jev
takes about 0.2 s and Luna about 6-8 s.
