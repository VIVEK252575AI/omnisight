"""Image crop/tile service for Week 4 optimization (coordinate provenance)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from PIL import Image

MAX_PIXELS = 50_000_000  # decompression-bomb guard


@dataclass
class CropSpec:
    source_box: Tuple[int, int, int, int]  # x, y, w, h in source pixels
    padding: int = 0
    scale: float = 1.0
    reason: str = ""


@dataclass
class CropResult:
    crop_box: Tuple[int, int, int, int]  # clamped x,y,w,h in source pixels
    output_size: Tuple[int, int]
    scale: float
    candidate_ref: str | None = None
    reason: str = ""
    # mapping: source (x,y) -> crop (x',y') given by x' = (x - crop_box[0]) * scale


def clamp_box(box: Tuple[int, int, int, int], img_w: int, img_h: int) -> Tuple[int, int, int, int] | None:
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return None
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(img_w, x + w)
    y1 = min(img_h, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def apply_padding(box: Tuple[int, int, int, int], padding: int, img_w: int, img_h: int) -> Tuple[int, int, int, int]:
    x, y, w, h = box
    return clamp_box((x - padding, y - padding, w + 2 * padding, h + 2 * padding), img_w, img_h) or box


def crop_image(source: Path, spec: CropSpec, out_path: Path) -> CropResult:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(source) as im:
        im.load()
        w, h = im.size
        clamped = clamp_box(spec.source_box, w, h)
        if clamped is None:
            raise ValueError("crop box outside image or zero area")
        padded = apply_padding(clamped, spec.padding, w, h)
        region = im.crop((padded[0], padded[1], padded[0] + padded[2], padded[1] + padded[3]))
        if spec.scale != 1.0:
            new_w = max(1, int(region.width * spec.scale))
            new_h = max(1, int(region.height * spec.scale))
            region = region.resize((new_w, new_h))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        region.save(out_path)
        return CropResult(crop_box=padded, output_size=region.size, scale=spec.scale, reason=spec.reason)


def tile_image(source: Path, tile_h: int, overlap: int, out_dir: Path) -> List[CropResult]:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    results: List[CropResult] = []
    with Image.open(source) as im:
        im.load()
        w, h = im.size
        step = max(1, tile_h - overlap)
        y = 0
        i = 0
        while y < h:
            bh = min(tile_h, h - y)
            if bh <= 0:
                break
            box = (0, y, w, bh)
            out_path = out_dir / f"tile_{i}.png"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            im.crop((0, y, w, y + bh)).save(out_path)
            results.append(CropResult(crop_box=box, output_size=(w, bh), scale=1.0, reason="tile"))
            y += step
            i += 1
            if y >= h:
                break
    return results


def source_to_crop(cx: int, cy: int, crop_box: Tuple[int, int, int, int], scale: float) -> Tuple[int, int]:
    """Map a source pixel (cx,cy) into crop coordinates."""
    x0, y0, _, _ = crop_box
    return (int((cx - x0) * scale), int((cy - y0) * scale))


def crop_to_source(cx: int, cy: int, crop_box: Tuple[int, int, int, int], scale: float) -> Tuple[int, int]:
    """Map a crop pixel back to source coordinates."""
    x0, y0, _, _ = crop_box
    return (int(cx / scale + x0), int(cy / scale + y0))
