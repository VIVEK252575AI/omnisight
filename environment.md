# Environment configuration

## The rule

Anything a browser loads (HTML, CSS, JavaScript, `js/env.js`) can be read by every visitor, including with "View source". So:

| | Frontend `.env` (`PUBLIC_` only) | Backend secrets |
|---|---|---|
| Safe if the whole world sees it | Yes | **No** |
| Who owns it | Frontend | Backend (Laksh) |
| Examples | API base URL, timeouts, feature flags, default model **names** | VLM/LLM API keys, JWT/session secrets, database URL, GitHub token or app key |

An API base URL such as `https://api.omnisight.dev/v1` is **not** a secret: anyone can see it in the browser's network tab. What protects the API is authentication and authorization on the backend, not hiding the URL.

## What must stay on the backend

| Secret | Why it can't be in the browser |
|---|---|
| `VLM_API_KEY`, `LLM_API_KEY` | Anyone could copy the key and run up your AI bill |
| `JWT_SECRET`, `SESSION_SECRET` | Whoever has it can forge logins |
| `DATABASE_URL`, `REDIS_URL` | Direct access to your data |
| `GITHUB_APP_PRIVATE_KEY` / tokens | Could push code to users' repositories |
| Screenshot storage keys | Could read or delete stored screenshots |

Consequence for the design: **the frontend never calls an AI provider.** It calls the OmniSight REST API (for example `POST /scans`), and the backend calls the VLM and LLM with its own keys.

The full list for Laksh is in `docs/backend.env.example`.

## How the frontend gets its config

```
.env  --npm run env-->  js/env.js  -->  js/core/config.js  -->  your code
(PUBLIC_ only)          (window.__ENV__)   (typed, frozen)       import { config }
```

1. `cp .env.example .env`, then edit values.
2. `npm run env` writes `js/env.js` containing only `PUBLIC_*` values.
3. Code imports `config` from `js/core/config.js`. It never reads `window.__ENV__` directly.

`js/env.js` is committed with safe defaults (mock mode on), so a fresh clone runs with no setup.

## Guardrails in `scripts/generate-env.mjs`

- Only `PUBLIC_*` variables are copied. Everything else is skipped.
- A `PUBLIC_` name containing `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `PRIVATE` or `CREDENTIAL` stops the script with an error.
- A value that looks like a credential (`sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, `xox…-`, JWTs) stops the script with an error.
- `.gitignore` excludes `.env` and `.env.*` but keeps `.env.example`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `PUBLIC_APP_ENV` | `development` | `development`, `staging` or `production` |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000/api/v1` | REST API root, no trailing slash |
| `PUBLIC_API_TIMEOUT_MS` | `15000` | Request timeout |
| `PUBLIC_USE_MOCKS` | `true` | `true` uses `js/services/mock`, `false` calls the real API |
| `PUBLIC_AUTH_MODE` | `cookie` | `cookie` (HttpOnly session cookie, recommended) or `bearer` (token kept in memory) |
| `PUBLIC_DEFAULT_VLM` / `PUBLIC_DEFAULT_LLM` | model names | Pre-selected models in the UI |
| `PUBLIC_ENABLE_DEEP_SCAN` | `true` | Show the Deep scan option |
| `PUBLIC_ENABLE_PR_CREATION` | `false` | Show "Ship PR" (needs backend GitHub support) |

## Deployment

Each environment (staging, production) sets its own `PUBLIC_*` values, then runs `npm run env` before publishing the static files. Never reuse a development `.env` for production.

## Auth storage note

Do not store tokens in `localStorage`; any injected script can read it. With `cookie` mode the browser holds the session and JavaScript never sees it. With `bearer` mode the token stays in memory only, so a page reload requires a refresh call or a new login. Details are in Step 10.
