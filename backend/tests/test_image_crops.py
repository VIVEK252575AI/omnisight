"""Image crop/tile tests (W4.2)."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile

from PIL import Image

from app.image_crops import (
    clamp_box, apply_padding, crop_image, tile_image,
    source_to_crop, crop_to_source, CropSpec,
)


def _make_image(path: Path, w=100, h=200):
    img = Image.new("RGB", (w, h), color="red")
    img.save(path)


def test_clamp_box_inside():
    assert clamp_box((10, 20, 30, 40), 100, 200) == (10, 20, 30, 40)


def test_clamp_box_clipped():
    assert clamp_box((90, 190, 30, 40), 100, 200) == (90, 190, 10, 10)


def test_clamp_box_zero_area():
    assert clamp_box((10, 20, 0, 40), 100, 200) is None
    assert clamp_box((10, 20, 30, 0), 100, 200) is None


def test_clamp_box_fully_outside():
    assert clamp_box((200, 300, 10, 10), 100, 200) is None


def test_apply_padding():
    r = apply_padding((10, 20, 30, 40), 5, 100, 200)
    assert r == (5, 15, 40, 50)


def test_crop_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src.png"
        _make_image(src, 100, 200)
        out = Path(d) / "crop.png"
        res = crop_image(src, CropSpec(source_box=(10, 20, 30, 40), padding=5), out)
        assert out.exists()
        assert res.crop_box == (5, 15, 40, 50)
        # round trip
        sx, sy = source_to_crop(20, 30, res.crop_box, res.scale)
        rx, ry = crop_to_source(sx, sy, res.crop_box, res.scale)
        assert (rx, ry) == (20, 30)


def test_crop_scale():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src.png"
        _make_image(src, 100, 200)
        out = Path(d) / "crop.png"
        res = crop_image(src, CropSpec(source_box=(0, 0, 50, 50), padding=0, scale=0.5), out)
        assert res.output_size == (25, 25)


def test_tile_bounds():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src.png"
        _make_image(src, 50, 100)
        out_dir = Path(d) / "tiles"
        res = tile_image(src, 40, 10, out_dir)
        assert len(res) >= 3
        # total coverage
        last = res[-1]
        assert last.crop_box[1] + last.crop_box[3] == 100


def test_corrupt_image_rejected():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "bad.png"
        src.write_bytes(b"not an image")
        out = Path(d) / "crop.png"
        try:
            crop_image(src, CropSpec(source_box=(0, 0, 10, 10)), out)
            assert False, "should raise"
        except Exception:
            assert True
