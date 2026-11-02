# Database Setup (DB-1)

## Prerequisites
- PostgreSQL (local or Supabase). No local Postgres/Docker detected in this environment.

## Configure
1. Create Supabase project (or local Postgres).
2. Copy `.env.example` to `.env` (git-ignored).
3. Set `DATABASE_URL=postgresql://postgres:<PASSWORD>@<host>:5432/postgres` (or `OMNISIGHT_DATABASE_URL`).
4. Optionally set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_STORAGE_BUCKET.

## Migrate
```
cd backend
alembic upgrade head
```

## Verify
```
alembic current
alembic check   # autogenerate diff should be empty
```

## Start
```
python run_dev.py
```

## Test
```
python -m pytest tests\ -q
```

## Real Supabase
BLOCKED: requires actual project password and network. Provide password to run migrations.
```
