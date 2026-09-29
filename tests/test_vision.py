from __future__ import annotations

import io
import unittest

from it_takes_me.vision import ModelFrameEncoder
from PIL import Image


def split_frame(width: int = 3840, height: int = 2160) -> bytes:
    image = Image.new("RGB", (width, height), "red")
    image.paste("blue", (width // 2, 0, width, height))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class ModelFrameEncoderTests(unittest.TestCase):
    def test_low_detail_crops_left_half_and_limits_long_edge(self) -> None:
        frame = ModelFrameEncoder("left").encode(split_frame(), detail="low")

        self.assertEqual((frame.width, frame.height), (455, 512))
        self.assertEqual(frame.media_type, "image/jpeg")
        with Image.open(io.BytesIO(frame.data)) as image:
            red, green, blue = image.getpixel((image.width // 2, image.height // 2))
        self.assertGreater(red, 240)
        self.assertLess(green, 15)
        self.assertLess(blue, 15)

    def test_high_detail_crops_right_half_and_limits_long_edge(self) -> None:
        frame = ModelFrameEncoder("right").encode(split_frame(), detail="high")

        self.assertEqual((frame.width, frame.height), (1365, 1536))
        with Image.open(io.BytesIO(frame.data)) as image:
            red, green, blue = image.getpixel((image.width // 2, image.height // 2))
        self.assertLess(red, 15)
        self.assertLess(green, 15)
        self.assertGreater(blue, 240)


if __name__ == "__main__":
    unittest.main()
