from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from it_takes_me.game.io import grab
from it_takes_me.inference.tools import build_game_tools
from it_takes_me.recording import RunRecorder
from it_takes_me.vision import ModelFrameEncoder
from PIL import Image
from tests.test_chunks import Clock, FakeGame


def texts(response: dict) -> list[str]:
    return [item["text"] for item in response["contentItems"] if item["type"] == "inputText"]


LOOK = {"skill": "look", "look": "left", "ms": 400}
RUN = {"skill": "run", "dir": "forward", "ms": 400}


class ActCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.io = FakeGame(self.clock)
        self.io.snapshot_fn = lambda size, n: Image.new("L", size, abs(n * 3 % 200 - 100))
        self.enterContext(patch("it_takes_me.game.playback.time.monotonic", self.clock.monotonic))
        self.enterContext(patch("it_takes_me.game.pad_thread.time.monotonic", self.clock.monotonic))
        self.enterContext(patch("it_takes_me.game.playback.time.sleep", self.clock.sleep))
        self.enterContext(patch("it_takes_me.game.pad_thread.time.sleep", self.clock.sleep))
        self.said: list[str] = []

    def tools(self):
        return build_game_tools(self.io, frame_after_action=False, on_say=self.said.append)

    def act(self, tools, steps: list[dict], **fields: object) -> dict:
        return tools.dispatch("act", {"task": "t", "intent": "go", "steps": steps, **fields})

    def test_a_chunk_of_only_camera_turns_is_refused(self) -> None:
        tools = self.tools()
        for steps in ([LOOK], [LOOK, LOOK], [{"skill": "repeat", "times": 2, "steps": [LOOK]}]):
            with self.subTest(steps=steps):
                response = self.act(tools, steps)
                self.assertFalse(response["success"])
                self.assertIn("Add the move that follows the turn", texts(response)[0])
        self.assertEqual(self.io.inputs, [])
        self.assertTrue(self.act(tools, [LOOK, RUN])["success"])

    def test_a_camera_scan_with_keyframes_or_a_line_to_say_is_allowed(self) -> None:
        self.io.capture_image = lambda: Image.new("RGB", (64, 36))  # keyframes need frames
        tools = self.tools()
        self.assertTrue(self.act(tools, [LOOK], observe="keyframes")["success"])
        self.assertTrue(self.act(tools, [LOOK], say="Looking for the marker.")["success"])

    def test_say_rides_along_with_the_chunk_and_has_no_tool_of_its_own(self) -> None:
        tools = self.tools()
        self.assertNotIn("say", tools.tools)
        self.assertIn("say", tools.tools["act"].input_schema["properties"])
        response = self.act(tools, [RUN], say="  I'll take the left   lever. ")
        self.assertEqual(self.said, ["I'll take the left lever."])
        self.assertIn("said to your co-op partner: I'll take the left lever.", texts(response))
        self.assertTrue(self.io.inputs)

    def test_a_refused_chunk_says_nothing(self) -> None:
        tools = self.tools()
        self.act(tools, [{"skill": "run", "ms": 400}], say="hello")
        self.assertEqual(self.said, [])

    def test_results_report_the_share_of_time_spent_moving(self) -> None:
        tools = self.tools()
        self.act(tools, [RUN])
        self.clock.sleep(1.2)  # the model plans for 1.2 s
        response = self.act(tools, [RUN])
        self.assertIn(
            "You stood still for 1200 ms planning, then played 400 ms (25% of the time moving). "
            "Longer chunks waste less.",
            texts(response),
        )


class FramePathTests(unittest.TestCase):
    def test_grab_prefers_the_backend_image_and_falls_back_to_decoding(self) -> None:
        image = Image.new("RGB", (8, 4), "red")

        class Direct:
            def capture_image(self) -> Image.Image:
                return image

            def capture(self) -> bytes:
                raise AssertionError("must not encode a PNG")

        self.assertIs(grab(Direct()), image)  # type: ignore[arg-type]

        class PngOnly:
            def capture(self) -> bytes:
                buf = io.BytesIO()
                image.save(buf, "PNG")
                return buf.getvalue()

        decoded = grab(PngOnly())  # type: ignore[arg-type]
        self.assertEqual((decoded.size, decoded.mode), ((8, 4), "RGB"))

    def test_encoder_takes_an_image_or_png_bytes_alike(self) -> None:
        image = Image.new("RGB", (3840, 2160), "red")
        image.paste("blue", (1920, 0, 3840, 2160))
        buf = io.BytesIO()
        image.save(buf, "PNG")
        encoder = ModelFrameEncoder("left")
        from_image, from_png = encoder.encode(image), encoder.encode(buf.getvalue())
        self.assertEqual((from_image.width, from_image.height), (455, 512))
        self.assertEqual(from_image.data, from_png.data)

    def test_recorder_writes_full_resolution_frames_in_the_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory))
            path = recorder.frame(Image.new("RGB", (64, 36), "blue"), reason="test")
            raw = recorder.frame(b"not really a png", reason="raw")
            recorder.close()
            with Image.open(path) as saved:
                self.assertEqual((saved.size, saved.getpixel((0, 0))), ((64, 36), (0, 0, 255)))
            self.assertEqual(raw.read_bytes(), b"not really a png")


if __name__ == "__main__":
    unittest.main()
