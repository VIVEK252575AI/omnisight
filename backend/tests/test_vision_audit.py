"""Tests for the MR.1 vision-audit corpus, scoring and summary logic.

These tests never call a model. They read the FIXTURE manifest and images that
were generated locally (see ``scripts/generate_vision_audit_fixtures.py``) and
exercise the pure scoring code in ``scripts/vision_audit.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "vision_audit"
MANIFEST_PATH = FIXTURES / "ground_truth.json"

_spec = importlib.util.spec_from_file_location(
    "vision_audit_script", ROOT / "scripts" / "vision_audit.py"
)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _read_png(path: Path) -> tuple[int, int, list[bytes]]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    pos, idat, width, height, color_type = 8, bytearray(), 0, 0, -1
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", chunk[:10])
            assert depth == 8
        elif tag == b"IDAT":
            idat.extend(chunk)
    assert color_type == 2
    raw = zlib.decompress(bytes(idat))
    stride = width * 3
    rows = []
    for y in range(height):
        start = y * (stride + 1)
        assert raw[start] == 0  # filter type 0 (None)
        rows.append(raw[start + 1 : start + 1 + stride])
    return width, height, rows


def _pixel(rows: list[bytes], x: int, y: int) -> tuple[int, int, int]:
    row = rows[y]
    return row[x * 3], row[x * 3 + 1], row[x * 3 + 2]


def test_manifest_is_labelled_fixture_with_all_cases() -> None:
    manifest = _manifest()
    assert manifest["provenance"]["kind"] == "FIXTURE"
    assert "not ShopLite captures" in manifest["provenance"]["note"]
    cases = manifest["cases"]
    assert [c["case_id"] for c in cases] == [
        "hidden_submit",
        "clipped_checkout",
        "overlapping_elements",
        "low_contrast_text",
        "responsive_failure",
        "healthy_control",
        "ambiguous_case",
    ]
    assert sum(1 for c in cases if c["injected_defect"] is None) == 2


def test_every_image_matches_manifest_hash_and_size() -> None:
    manifest = _manifest()
    for case in manifest["cases"]:
        path = FIXTURES / case["image"]
        assert path.is_file(), case["image"]
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == case["image_sha256"]
        assert len(data) == case["image_bytes"]
        width, height, _ = _read_png(path)
        assert (width, height) == (390, 844)


def test_injected_geometry_is_present_in_pixels() -> None:
    manifest = {c["case_id"]: c for c in _manifest()["cases"]}

    def image(case_id: str) -> list[bytes]:
        return _read_png(FIXTURES / manifest[case_id]["image"])[2]

    accent = (37, 99, 235)
    white = (255, 255, 255)
    pale = (219, 234, 254)
    near_white_text = (238, 238, 238)

    # Clipped: the button reaches the right edge and starts left of it.
    clipped = image("clipped_checkout")
    assert _pixel(clipped, 389, 680) == accent
    assert _pixel(clipped, 299, 680) != accent

    # Hidden: the submit slot is empty (white) but outlined.
    hidden = image("hidden_submit")
    assert _pixel(hidden, 200, 724) == white
    assert _pixel(hidden, 20, 700) == pale

    # Healthy: the same slot is filled by a visible button.
    assert _pixel(image("healthy_control"), 320, 720) == accent

    # Low contrast: near-white price text on a near-white card.
    assert _pixel(image("low_contrast_text"), 100, 426) == near_white_text

    # Overlap: the promo card paints over the product card region.
    assert _pixel(image("overlapping_elements"), 200, 400) == pale


def _clipped_case() -> dict:
    return {
        "case_id": "clipped_checkout",
        "expectation": "defect",
        "injected_defect": {
            "category": "clipped_element",
            "target": "#checkout",
            "region": {"x": 300, "y": 640, "width": 200, "height": 48},
        },
    }


def _record(findings: list[dict], status: str = "detected", outcome: str = "defects_found") -> dict:
    return {
        "findings": [{"finding": f} for f in findings],
        "scan_status": status,
        "outcome": outcome,
    }


def test_score_detected_with_target_and_region() -> None:
    record = _record(
        [
            {
                "category": "clipped_element",
                "target_candidate": "button#checkout",
                "region": {"x": 310, "y": 635, "width": 190, "height": 50},
                "confidence": 0.8,
            }
        ]
    )
    score = audit.score_run(_clipped_case(), record)
    assert score == {
        "verdict": "detected",
        "category_correct": True,
        "target_plausible": True,
        "region_plausible": True,
    }


def test_score_right_category_wrong_target_is_visible() -> None:
    record = _record(
        [{"category": "clipped_element", "target_candidate": "#submit", "region": {"x": 0, "y": 0, "width": 10, "height": 10}}]
    )
    score = audit.score_run(_clipped_case(), record)
    assert score["verdict"] == "detected"
    assert score["category_correct"] is True
    assert score["target_plausible"] is False
    assert score["region_plausible"] is False


def test_score_wrong_category_and_missed() -> None:
    wrong = _record([{"category": "low_contrast_text", "target_candidate": "#checkout"}])
    assert audit.score_run(_clipped_case(), wrong)["verdict"] == "wrong_category"

    missed = _record([], status="detected", outcome="no_defects")
    assert audit.score_run(_clipped_case(), missed)["verdict"] == "missed"
    assert audit.score_run(_clipped_case(), missed)["category_correct"] is False


def test_score_detection_failure_is_error() -> None:
    record = _record([], status="detection_failed", outcome="")
    record["reason_code"] = "timeout"
    score = audit.score_run(_clipped_case(), record)
    assert score["verdict"] == "error"


def test_score_healthy_control() -> None:
    clean = {"case_id": "healthy_control", "expectation": "clean", "injected_defect": None}
    assert audit.score_run(clean, _record([], outcome="no_defects"))["verdict"] == "clean"
    false_positive = _record([{"category": "clipped_element", "confidence": 0.9}])
    assert audit.score_run(clean, false_positive)["verdict"] == "false_positive"


def test_score_ambiguous_hedge_versus_confident_guess() -> None:
    ambiguous = {
        "case_id": "ambiguous_case",
        "expectation": "needs_inspection_or_hedged",
        "injected_defect": None,
    }
    hedged = _record([], outcome="needs_inspection")
    assert audit.score_run(ambiguous, hedged)["verdict"] == "hedged"

    low_confidence = _record([{"category": "clipped_element", "confidence": 0.3}])
    assert audit.score_run(ambiguous, low_confidence)["verdict"] == "hedged"

    confident = _record([{"category": "clipped_element", "confidence": 0.9}])
    assert audit.score_run(ambiguous, confident)["verdict"] == "not_hedged"


def test_target_matching_uses_selector_tokens_not_free_text() -> None:
    assert audit.target_matches("#checkout", "button#checkout") is True
    assert audit.target_matches(".price", "span.price") is True
    assert audit.target_matches("#checkout", "#submit") is False
    assert audit.target_matches("#checkout", "the checkout button") is False
    assert audit.target_matches(None, "#checkout") is False


def test_region_overlap_normalises_both_field_spellings() -> None:
    gt = {"x": 10, "y": 10, "width": 100, "height": 50}
    assert audit.regions_overlap(gt, {"x": 50, "y": 20, "width": 100, "height": 50}) is True
    assert audit.regions_overlap(gt, {"x": 200, "y": 20, "width": 10, "height": 10}) is False
    assert audit.regions_overlap(gt, {"x": 50, "y": 20, "w": 100, "h": 50}) is True


def test_summary_counts_are_derived_from_records() -> None:
    cases = [
        _clipped_case(),
        {"case_id": "hidden_submit", "expectation": "defect", "injected_defect": {"category": "hidden_or_unreachable_element", "target": "#submit", "region": {"x": 20, "y": 700, "width": 350, "height": 48}}},
        {"case_id": "healthy_control", "expectation": "clean", "injected_defect": None},
    ]
    records = []
    for case in cases:
        for mode in ("screenshot_only", "with_dom"):
            record = _record(
                [{"category": "clipped_element", "target_candidate": "#checkout", "confidence": 0.9}]
            )
            record.update(
                {
                    "case_id": case["case_id"],
                    "mode": mode,
                    "repetition": 1,
                    "wall_ms": 10.0,
                    "adapter_label": "MOCK",
                    "is_mock": True,
                    "model_inference_performed": False,
                    "scan_status": "detected",
                }
            )
            record["score"] = audit.score_run(case, record)
            records.append(record)

    summary = audit.summarize(records, cases)
    assert summary["total_records"] == 6
    assert summary["defect_runs"] == 4
    assert summary["category_correct_runs"] == 2  # only the clipped case matches
    assert summary["healthy_runs"] == 2
    assert summary["false_positive_runs"] == 2
    assert summary["model_inference_performed"] is False
    assert len(summary["hidden_submit"]) == 2
    assert all(r["plain_result"] == "missed" for r in summary["hidden_submit"])
