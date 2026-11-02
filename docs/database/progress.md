# DB-1 Progress

- Phase 1 audit: docs/database/audit.md
- Phase 2 design: docs/database/design.md
- Phase 3-5: existing SQLAlchemy/Alembic/PostgreSQL infrastructure present; Supabase Storage service added (app/supabase_storage.py).
- Supabase SQL schema generated: docs/database/supabase_schema.sql (tables + indexes).
- Phase 6 testing: real-Supabase verification BLOCKED — direct DB connection times out from this environment (network/firewall). Supabase REST API is reachable. Run alembic upgrade head when network allows.
- Phase 7 docs: setup.md.

To connect: set OMNISIGHT_DATABASE_URL with sslmode=require and run alembic upgrade head.
