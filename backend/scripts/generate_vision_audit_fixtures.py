"""Generate the MR.1 vision-audit fixture corpus.

There is no ShopLite collector output and no ShopLite application in this
repository, so this script builds a small, fully controlled FIXTURE corpus of
PNG images with deliberately injected defects. The accompanying
``ground_truth.json`` records exactly what was injected into each image.

These are FIXTURES, not real ShopLite captures. They exist so the audit harness
and its scoring logic can be exercised end to end. They say nothing about how a
real VLM would perform.

Pure standard library (no Pillow): a minimal RGB PNG encoder plus rectangle
drawing. Run from ``backend/``:

    .\\.venv\\Scripts\\python.exe scripts\\generate_vision_audit_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

WIDTH = 390
HEIGHT = 844

WHITE = (255, 255, 255)
INK = (30, 41, 59)
MUTED = (100, 116, 139)
LIGHT = (226, 232, 240)
CARD = (248, 250, 252)
ACCENT = (37, 99, 235)
PALE = (219, 234, 254)
NEAR_WHITE = (238, 238, 238)


class Canvas:
    def __init__(self, width: int, height: int, color: tuple[int, int, int]) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(color) * (width * height))

    def _idx(self, x: int, y: int) -> int:
        return (y * self.width + x) * 3

    def set_pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = self._idx(x, y)
            self.pixels[i : i + 3] = bytes(color)

    def rect(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        if w <= 0 or h <= 0:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        row = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            start = self._idx(x0, yy)
            self.pixels[start : start + len(row)] = row

    def outline(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        color: tuple[int, int, int],
        thickness: int = 1,
    ) -> None:
        self.rect(x, y, w, thickness, color)
        self.rect(x, y + h - thickness, w, thickness, color)
        self.rect(x, y, thickness, h, color)
        self.rect(x + w - thickness, y, thickness, h, color)

    def bar(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        """A filled bar standing in for a run of text."""
        self.rect(x, y, w, h, color)

    def png_bytes(self) -> bytes:
        raw = bytearray()
        stride = self.width * 3
        for y in range(self.height):
            raw.append(0)
            raw.extend(self.pixels[y * stride : (y + 1) * stride])
        compressed = zlib.compress(bytes(raw), 9)

        def chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", compressed)
            + chunk(b"IEND", b"")
        )


def _chrome(c: Canvas) -> None:
    c.bar(20, 24, 160, 20, INK)
    c.bar(300, 26, 60, 14, MUTED)


def _form(c: Canvas, top: int) -> None:
    c.rect(20, top, 350, 180, CARD)
    c.outline(20, top, 350, 180, LIGHT)
    c.bar(40, top + 24, 240, 10, MUTED)
    c.bar(40, top + 48, 200, 10, MUTED)
    c.bar(40, top + 72, 260, 10, MUTED)


def draw_hidden_submit() -> Canvas:
    c = Canvas(WIDTH, HEIGHT, WHITE)
    _chrome(c)
    _form(c, 80)
    c.rect(20, 290, 350, 140, CARD)
    c.outline(20, 290, 350, 140, LIGHT)
    c.bar(40, 314, 160, 10, MUTED)
    c.bar(40, 338, 220, 10, MUTED)
    c.bar(40, 362, 180, 10, MUTED)
    c.rect(20, 470, 350, 120, CARD)
    c.outline(20, 470, 350, 120, LIGHT)
    c.bar(40, 494, 120, 10, MUTED)
    c.bar(40, 518, 300, 10, LIGHT)
    c.bar(40, 542, 280, 10, LIGHT)
    # The checkout submit button is not rendered: an empty outlined slot remains
    # where it belongs. Ground truth: hidden/unreachable #submit.
    c.outline(20, 700, 350, 48, PALE, thickness=2)
    return c


def draw_clipped_checkout() -> Canvas:
    c = Canvas(WIDTH, HEIGHT, WHITE)
    _chrome(c)
    _form(c, 80)
    c.rect(20, 290, 350, 140, CARD)
    c.outline(20, 290, 350, 140, LIGHT)
    c.bar(40, 314, 160, 10, MUTED)
    c.bar(40, 338, 220, 10, MUTED)
    c.bar(40, 362, 180, 10, MUTED)
    # Button starts at x=300 and is 200px wide -> 110px past the 390px edge.
    c.rect(300, 640, 200, 48, ACCENT)
    c.bar(320, 656, 120, 12, WHITE)
    return c


def draw_overlapping_elements() -> Canvas:
    c = Canvas(WIDTH, HEIGHT, WHITE)
    _chrome(c)
    c.rect(20, 280, 300, 180, CARD)
    c.outline(20, 280, 300, 180, LIGHT)
    c.bar(40, 304, 180, 10, MUTED)
    c.bar(40, 328, 220, 10, MUTED)
    c.bar(40, 352, 160, 10, MUTED)
    # Promo card overlaps the product card and covers its text.
    c.rect(120, 360, 250, 170, PALE)
    c.outline(120, 360, 250, 170, ACCENT)
    c.bar(140, 384, 160, 12, INK)
    c.bar(140, 410, 200, 10, MUTED)
    c.bar(140, 434, 180, 10, MUTED)
    return c


def draw_low_contrast_text() -> Canvas:
    c = Canvas(WIDTH, HEIGHT, WHITE)
    _chrome(c)
    _form(c, 80)
    c.rect(20, 290, 350, 120, CARD)
    c.outline(20, 290, 350, 120, LIGHT)
    # Near-white price text on a near-white card: unreadable.
    c.bar(40, 414, 160, 24, NEAR_WHITE)
    c.bar(40, 450, 100, 16, NEAR_WHITE)
    c.bar(40, 500, 240, 10, MUTED)
    c.rect(20, 520, 350, 48, ACCENT)
    c.bar(140, 538, 110, 12, WHITE)
    return c


def draw_responsive_failure() -> Canvas:
    c = Canvas(WIDTH, HEIGHT, WHITE)
    _chrome(c)
    # Desktop three-column grid (720px wide) inside a 390px viewport.
    c.rect(0, 200, 720, 300, CARD)
    c.outline(0, 200, 720, 300, LIGHT)
    for i in range(3):
        x = i * 240
        c.rect(x + 10, 220, 220, 260, WHITE)
        c.outline(x + 10, 220, 220, 260, LIGHT)
        c.bar(x + 26, 240, 160, 12, INK)
        c.bar(x + 26, 264, 120, 8, MUTED)
        c.bar(x + 26, 300, 180, 60, LIGHT)
    c.bar(20, 540, 200, 12, MUTED)
    return c


def draw_healthy_control() -> Canvas:
    c = Canvas(WIDTH, HEIGHT, WHITE)
    _chrome(c)
    _form(c, 80)
    c.rect(20, 290, 350, 140, CARD)
    c.outline(20, 290, 350, 140, LIGHT)
    c.bar(40, 314, 160, 10, MUTED)
    c.bar(40, 338, 220, 10, MUTED)
    c.bar(40, 362, 180, 10, MUTED)
    c.rect(20, 470, 350, 120, CARD)
    c.outline(20, 470, 350, 120, LIGHT)
    c.bar(40, 494, 120, 10, MUTED)
    c.bar(40, 518, 300, 10, LIGHT)
    c.bar(40, 542, 280, 10, LIGHT)
    # Fully inside the viewport, readable, not overlapped.
    c.rect(20, 700, 350, 48, ACCENT)
    c.bar(140, 718, 110, 12, WHITE)
    return c


def draw_ambiguous_case() -> Canvas:
    c = Canvas(WIDTH, HEIGHT, WHITE)
    _chrome(c)
    _form(c, 80)
    c.rect(26, 296, 350, 140, CARD)
    c.outline(26, 296, 350, 140, LIGHT)
    c.bar(46, 320, 160, 10, MUTED)
    c.bar(46, 344, 220, 10, MUTED)
    c.bar(46, 368, 180, 10, MUTED)
    c.rect(20, 470, 350, 120, CARD)
    c.outline(20, 470, 350, 120, LIGHT)
    c.bar(40, 494, 120, 10, MUTED)
    c.bar(40, 518, 300, 10, LIGHT)
    # Slightly lighter than normal supporting copy, but still legible.
    c.bar(40, 542, 280, 10, (176, 184, 196))
    c.rect(20, 700, 350, 48, ACCENT)
    c.bar(140, 718, 110, 12, WHITE)
    return c


CASES = [
    {
        "case_id": "hidden_submit",
        "filename": "hidden-submit.png",
        "draw": draw_hidden_submit,
        "route": "/checkout",
        "injected_defect": {
            "category": "hidden_or_unreachable_element",
            "target": "#submit",
            "region": {"x": 20, "y": 700, "width": 350, "height": 48},
        },
        "expectation": "defect",
        "dom_elements": [
            {
                "selector": "#submit",
                "tag": "button",
                "text": "Place order",
                "visible": False,
                "in_viewport": True,
                "box": {"x": 20, "y": 700, "w": 350, "h": 48},
            }
        ],
        "notes": "Submit button not rendered; an empty slot remains where it belongs.",
    },
    {
        "case_id": "clipped_checkout",
        "filename": "clipped-checkout.png",
        "draw": draw_clipped_checkout,
        "route": "/checkout",
        "injected_defect": {
            "category": "clipped_element",
            "target": "#checkout",
            "region": {"x": 300, "y": 640, "width": 200, "height": 48},
        },
        "expectation": "defect",
        "dom_elements": [
            {
                "selector": "#checkout",
                "tag": "button",
                "text": "Checkout",
                "visible": True,
                "in_viewport": False,
                "box": {"x": 300, "y": 640, "w": 200, "h": 48},
            }
        ],
        "notes": "Button extends 110px past the right edge of a 390px viewport.",
    },
    {
        "case_id": "overlapping_elements",
        "filename": "overlapping-elements.png",
        "draw": draw_overlapping_elements,
        "route": "/products",
        "injected_defect": {
            "category": "overlapping_elements",
            "target": ".promo",
            "region": {"x": 120, "y": 360, "width": 250, "height": 170},
        },
        "expectation": "defect",
        "dom_elements": [
            {
                "selector": ".product-card",
                "tag": "div",
                "text": None,
                "visible": True,
                "in_viewport": True,
                "box": {"x": 20, "y": 280, "w": 300, "h": 180},
            },
            {
                "selector": ".promo",
                "tag": "div",
                "text": "Summer sale",
                "visible": True,
                "in_viewport": True,
                "box": {"x": 120, "y": 360, "w": 250, "h": 170},
            },
        ],
        "notes": "Promo card is painted over the product card and its copy.",
    },
    {
        "case_id": "low_contrast_text",
        "filename": "low-contrast-text.png",
        "draw": draw_low_contrast_text,
        "route": "/products",
        "injected_defect": {
            "category": "low_contrast_text",
            "target": ".price",
            "region": {"x": 40, "y": 414, "width": 160, "height": 24},
        },
        "expectation": "defect",
        "dom_elements": [
            {
                "selector": ".price",
                "tag": "span",
                "text": "$49.00",
                "visible": True,
                "in_viewport": True,
                "box": {"x": 40, "y": 414, "w": 160, "h": 24},
                "computed_style": {"color": "rgb(238, 238, 238)", "background-color": "rgb(248, 250, 252)"},
            }
        ],
        "notes": "Price text is near-white on a near-white card.",
    },
    {
        "case_id": "responsive_failure",
        "filename": "responsive-failure.png",
        "draw": draw_responsive_failure,
        "route": "/products",
        "injected_defect": {
            "category": "responsive_layout_failure",
            "target": ".product-grid",
            "region": {"x": 0, "y": 200, "width": 720, "height": 300},
        },
        "expectation": "defect",
        "dom_elements": [
            {
                "selector": ".product-grid",
                "tag": "div",
                "text": None,
                "visible": True,
                "in_viewport": False,
                "box": {"x": 0, "y": 200, "w": 720, "h": 300},
            }
        ],
        "notes": "A 720px three-column grid is rendered in a 390px viewport.",
    },
    {
        "case_id": "healthy_control",
        "filename": "healthy-control.png",
        "draw": draw_healthy_control,
        "route": "/checkout",
        "injected_defect": None,
        "expectation": "clean",
        "dom_elements": [
            {
                "selector": "#submit",
                "tag": "button",
                "text": "Place order",
                "visible": True,
                "in_viewport": True,
                "box": {"x": 20, "y": 700, "w": 350, "h": 48},
            }
        ],
        "notes": "No defect injected: readable, non-overlapping, fully in viewport.",
    },
    {
        "case_id": "ambiguous_case",
        "filename": "ambiguous-case.png",
        "draw": draw_ambiguous_case,
        "route": "/checkout",
        "injected_defect": None,
        "expectation": "needs_inspection_or_hedged",
        "dom_elements": [
            {
                "selector": "#summary",
                "tag": "section",
                "text": "Order summary",
                "visible": True,
                "in_viewport": True,
                "box": {"x": 20, "y": 296, "w": 350, "h": 140},
            }
        ],
        "notes": "Slight misalignment and lighter supporting copy; no unambiguous defect.",
    },
]


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "vision_audit"
    images = root / "images"
    images.mkdir(parents=True, exist_ok=True)

    manifest_cases = []
    for case in CASES:
        canvas = case["draw"]()
        data = canvas.png_bytes()
        path = images / case["filename"]
        path.write_bytes(data)
        manifest_cases.append(
            {
                "case_id": case["case_id"],
                "image": f"images/{case['filename']}",
                "image_sha256": hashlib.sha256(data).hexdigest(),
                "image_bytes": len(data),
                "viewport": {"width": WIDTH, "height": HEIGHT},
                "route": case["route"],
                "expectation": case["expectation"],
                "injected_defect": case["injected_defect"],
                "dom_elements": case["dom_elements"],
                "notes": case["notes"],
            }
        )

    manifest = {
        "corpus_id": "vision-audit-fixtures-v1",
        "schema_version": 1,
        "provenance": {
            "kind": "FIXTURE",
            "label": "Locally generated controlled defects",
            "generator": "backend/scripts/generate_vision_audit_fixtures.py",
            "note": (
                "No ShopLite collector output, defect screenshots or defect "
                "manifest exists in this repository. These images were drawn "
                "locally with known geometry; they are not ShopLite captures."
            ),
        },
        "adapter_note": (
            "No real VLM provider is configured. Audit results produced from "
            "this corpus are MOCK results and verify the harness only."
        ),
        "cases": manifest_cases,
    }
    (root / "ground_truth.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(manifest_cases)} cases to {root}")


if __name__ == "__main__":
    main()
