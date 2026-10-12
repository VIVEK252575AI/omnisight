# AGENTS.md — OmniSight

Instructions for AI coding agents working in this repository.

## Repository / git workflow (owner-set rule)

- Working branch: **`lakshy`** (remote `origin`, owner `VIVEK252575AI/omnisight`).
- After completing a task, **commit and push to `lakshy` automatically**:
  - Stage only the files the task touched. **Never** `git add .` / `git add -A`.
  - Run the backend test suite before committing (`cd backend && .\.venv\Scripts\python.exe -m pytest tests\ -q`).
  - Write a concise, descriptive commit message.
- **`main` is shared and must be updated via Pull Request only.**
  - Open a PR from `lakshy` → `main` after pushing.
  - **Never** push directly to `main`, and **never force-push** any branch.
- Never commit secrets, tokens, keys, `.env` files, virtualenvs, or caches.

## Project layout

- `backend/` — FastAPI service (see `backend/app/main.py`). Start with
  `.\\.venv\\Scripts\\uvicorn.exe app.main:app --reload` from `backend/`.
- Backend tests: `cd backend && .\\.venv\\Scripts\\python.exe -m pytest tests\ -v`.

## Backend conventions

- Single error envelope for all non-2xx: `{"error": {"code", "message", "details"}}`
  (`app/errors.py`, `ErrorCode`).
- Persistence is in-memory behind a small store interface (`app/storage.py`,
  `app/event_storage.py`, `app/evidence_storage.py`) — swappable later.
- Pydantic schemas live in `app/schemas.py`; routers in `app/routers/`.
- Simulation only: the CI webhook receiver is **not** a real CI integration.
