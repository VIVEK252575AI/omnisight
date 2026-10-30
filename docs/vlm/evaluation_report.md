# VLM Evaluation Report

## Dataset
7 cases in evaluation/vlm/ground_truth.json (vision_audit fixtures).

## Ground truth method
Independent of VLM: known injected conditions + Playwright geometry / contrast calc.

## Model/config
gemini-3-flash-preview via env. Target 3 runs/case; actual runs limited by quota.

## Results
Real inference demonstrated on clipped-checkout.png: category=clipped_element, target=#place-order, confidence=1.0, provider=GEMINI, is_mock=False (run 2026-10-31). Full N-run metrics PENDING due to quota/time.

## MOCK vs real
MOCK adapter results are separate and not combined. Live results require OMNISIGHT_LIVE_GEMINI=1.

## Limitations
Small sample (n=7 images, single run shown). Not a benchmark.
