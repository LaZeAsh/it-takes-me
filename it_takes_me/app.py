"""Shared wiring for live and replay gameplay sessions."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from it_takes_me.config import Settings
from it_takes_me.game.io import GameIO
from it_takes_me.inference.player import GamePlayer
from it_takes_me.inference.responses import ResponsesRuntime
from it_takes_me.inference.runtime import CodexRuntime
from it_takes_me.inference.tools import build_game_tools
from it_takes_me.recording import RunRecorder
from it_takes_me.vision import ModelFrame, ModelFrameEncoder


def play_session(
    game: GameIO,
    settings: Settings,
    *,
    console: Console | None = None,
    interactive: bool = True,
    instant_actions: bool = False,
) -> Path:
    console = console or Console()
    recorder = RunRecorder(settings.runs_dir)
    console.print(f"recording to {recorder.dir}")
    recorder.event(
        "session",
        runtime=settings.runtime,
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        character=settings.character,
        replay=instant_actions,
    )
    frame_encoder = ModelFrameEncoder(
        half=settings.screen_half,
        low_max_px=settings.model_frame_low_max_px,
        high_max_px=settings.model_frame_high_max_px,
        jpeg_quality=settings.model_frame_jpeg_quality,
    )

    def record_observation(frame: ModelFrame, reason: str) -> object:
        return recorder.observation(
            frame.data,
            reason=reason,
            media_type=frame.media_type,
            width=frame.width,
            height=frame.height,
            detail=frame.detail,
        )

    tools = build_game_tools(
        game,
        frame_after_action=settings.frame_after_action,
        keyframes=settings.keyframes,
        max_chunk_ms=settings.max_chunk_ms,
        screen_half=settings.screen_half,
        frame_encoder=frame_encoder,
        on_frame=lambda png, reason: recorder.frame(png, reason=reason),
        on_observation=record_observation,
        on_say=lambda text: console.print(
            f"[bold magenta]{settings.character}:[/bold magenta] {text}"
        ),
        max_calls_per_turn=settings.max_tool_calls_per_turn,
        instant_actions=instant_actions,
    )
    if settings.runtime == "codex":
        runtime = CodexRuntime(
            cwd=settings.cwd,
            tools=tools,
            compact_threshold=settings.compact_after_input_tokens,
        )
    elif settings.runtime == "responses":
        runtime = ResponsesRuntime(
            tools=tools,
            compact_threshold=settings.compact_after_input_tokens,
            max_output_tokens=settings.max_output_tokens,
            store=settings.api_store,
        )
    else:
        raise ValueError(f"unknown runtime: {settings.runtime}")

    try:
        with runtime:
            session = runtime.start_session(
                model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                character=settings.character,
                extra_instructions=settings.extra_instructions,
            )
            console.print(
                f"session {session.id} on [bold]{session.model}[/bold] "
                f"via {settings.runtime} ({settings.reasoning_effort})"
            )
            player = GamePlayer(
                session=session,
                io=game,
                tools=tools,
                recorder=recorder,
                settings=settings,
                frame_encoder=frame_encoder,
                console=console,
            )
            if interactive:
                player.start_stdin_listener()
            player.play()
    finally:
        recorder.close()
    return recorder.dir
