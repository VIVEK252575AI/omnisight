"""VLM metric helpers (pure functions) with hand-computed tests."""

from __future__ import annotations


def precision(tp: int, fp: int) -> float:
    return tp / (tp + fp) if (tp + fp) else 0.0


def recall(tp: int, fn: int) -> float:
    return tp / (tp + fn) if (tp + fn) else 0.0


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def test_precision_recall_iou():
    assert precision(3, 1) == 0.75
    assert recall(3, 2) == 0.6
    assert iou((0, 0, 10, 10), (5, 5, 10, 10)) == 25 / 175
