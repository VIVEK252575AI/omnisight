# Database Audit (DB-1 Phase 1)

## Area | Existing implementation | Problem | Required action

| Area | Existing implementation | Problem | Required action |
|---|---|---|---|
| Authentication | Local owner auth (`app/auth.py` require_local_owner) | No user/org tables; single local principal | Keep as-is; DB-1 does not replace auth |
| Projects | `Project` ORM in `app/db_models.py`; `local_project_id` in settings | In-memory fallback when no DB | Configure DATABASE_URL for durability |
| Scans | `ScanStore` in `app/storage.py` with PostgreSQL via `get_session_factory`; in-memory fallback singleton | Durable only when DB configured | Set DATABASE_URL (Supabase) |
| Playwright evidence | `EvidenceStore` (`app/evidence_storage.py`) + `Artifact`/`Evidence` models | In-memory fallback loses data on restart | Configure DB + artifact root |
| VLM detections | `DetectionStore` (`app/detection_storage.py`) + `Detection`/`Finding` models | In-memory fallback | Configure DB |
| Repairs | `AttemptStore` process-wide in-memory (`app/orchestrator.py`) | Lost on restart | Persist attempts to DB (future) |
| Docker validation | `ValidationReport` model (`app/db_models.py`) | Persisted when DB configured | Configure DB |
| GitHub delivery | `GitHubOperation`/`PullRequest` models | Persisted when DB configured | Configure DB |
| Audit history | `WorkflowEvent` model | Persisted when DB configured | Configure DB |

## Data lost on restart (when no DB)
- Scans, evidence, detections, attempts, validation/verification, deliveries (in-memory fallback).

## Entity relationships found
- Project -> QARun (scans), TargetEnvironment, TestFlow/TestFlowVersion, Evidence, Artifact, Detection, RepairAttempt, ValidationReport, GitHubOperation, PullRequest, WorkflowEvent.
- Evidence -> Artifact (FK).

## Reuse
- Existing SQLAlchemy 2.x models, Alembic migrations, session factory, repositories (partial). No rebuild.
