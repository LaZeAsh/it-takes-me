# Harness implementation plan

Work through these steps in order. Each step keeps the existing Codex app-server path runnable.

## 1. Shared model-observation pipeline

Status: complete.

- Crop each captured frame to the controlled character's split-screen half.
- Resize low-detail observations to 512 px and high-detail observations to a 1,536 px maximum
  edge.
- Encode model observations as JPEG while retaining the original full-resolution PNG recording.
- Record the exact model observation beside each source frame for inspection and replay.

Acceptance: tests prove left/right cropping and size limits, and the existing runner sends the
processed observation to Codex without changing controller capture or full-frame recordings.

## 2. Usage accounting and compaction

Status: complete.

- Record last-request and cumulative input, cached input, output, and reasoning tokens.
- Configure Codex automatic compaction with an input-context threshold instead of cumulative usage.
- Use Responses server-side compaction for the API runtime.

Acceptance: cumulative usage cannot cause compaction on every later turn, and logs contain enough
fields to calculate tokens per action and API cost.

## 3. Observation and action policy

Status: complete.

- Tell the model that a turn's starting frame is fresh and should normally be acted on directly.
- Reserve `look_at_screen` for waiting, human-caused changes, and ambiguity.
- Reduce the default per-turn tool budget and keep every `act` result observable.

Acceptance: the prompt no longer induces an immediate duplicate look and the tool-call guardrail
matches a short gameplay turn.

## 4. Provider-neutral inference session

Status: complete.

- Define shared turn events, usage data, steering, compaction, and session lifecycle protocols.
- Move Codex-specific event translation into a Codex session adapter.
- Make the player depend only on the shared session interface.

Acceptance: the Codex runner retains streaming text, tools, steering, recording, and compaction
without importing Codex SDK types in the player.

## 5. Responses API runtime

Status: complete; validated with a fake Responses client. Live calls require an API key.

- Add a Responses session adapter using image input, function calling, `previous_response_id`, and
  server-side compaction.
- Map the existing tool registry and image-bearing tool results without duplicating game logic.
- Select the runtime and model through configuration while keeping Codex as the default.

Acceptance: unit tests cover tool schema/result conversion and a dry-run fake client completes a
multi-call turn; live API use only requires `OPENAI_API_KEY` and runtime selection.

## 6. Replay and comparison

Status: complete.

- Add a deterministic `GameIO` that replays frames from a recorded run while discarding pad input.
- Add a replay entry point and a run-summary command.
- Report decision latency, tools/actions, observations, usage breakdown, and errors.

Acceptance: a recorded run can exercise either inference runtime without launching the game, and
summaries make two runs directly comparable.
