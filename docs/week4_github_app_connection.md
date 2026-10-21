# Week 4: GitHub App connection and draft publishing

This slice adds backend-only GitHub App authentication, a read-only repository
verification command, and an internal Git Database publisher for one validated
ShopLite checkout CSS edit. The publisher creates a dedicated branch and draft
PR; it does not update the base branch or merge. Credentials and tokens are
never returned in reports.

## Configure a GitHub App

Create or select a GitHub App for GitHub.com. Grant only these repository
permissions:

- Contents: read and write
- Pull requests: read and write
- Metadata: read (GitHub requires this)

Install it only on the repository or repositories OmniSight should access.
Download its private key and keep the PEM outside the repository. Do not paste
the key into chat, a pull request, or an issue.

Copy backend/.env.example to backend/.env and configure:

    OMNISIGHT_ENABLE_GITHUB_APP=true
    OMNISIGHT_GITHUB_APP_ID=YOUR_APP_ID
    OMNISIGHT_GITHUB_APP_INSTALLATION_ID=YOUR_INSTALLATION_ID
    OMNISIGHT_GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----
    OMNISIGHT_GITHUB_TARGET_OWNER=YOUR_GITHUB_OWNER
    OMNISIGHT_GITHUB_TARGET_REPOSITORY=YOUR_REPOSITORY
    # Optional; defaults to the repository's configured default branch.
    OMNISIGHT_GITHUB_TARGET_BRANCH=main

The target owner/repository/branch variables are fixed server-side settings;
the API does not accept an arbitrary repository from the browser. The private
key is read only by the backend. Keep backend/.env out of source control.

## Verify the installation

From the backend directory:

    .\.venv\Scripts\python.exe scripts\verify_github_app.py

A successful check prints the numeric repository ID, canonical full name,
default branch, selected base branch, base commit SHA, and verification time.
Permission errors are reported without returning GitHub's raw response body.

The repository verification command asks for a single-repository
contents: read token. The publisher requests a single-repository token with
contents: write and pull_requests: write; it verifies GitHub's returned
repository scope and permissions before proceeding.

## Local owner flow

The HTTP flow is disabled unless every gate is configured: PostgreSQL,
`OMNISIGHT_ENABLE_GITHUB_PUBLISH_API=true`,
`OMNISIGHT_AUTH_MODE=local_single_user`, a random
`OMNISIGHT_LOCAL_AUTH_TOKEN` of at least 32 characters, the GitHub App settings,
and the ShopLite sandbox token/image. Requests also have to arrive from
loopback. This is a controlled local demo profile, not multi-user or public
hosting authentication.

Install PostgreSQL, create a database and user, set `OMNISIGHT_DATABASE_URL`,
then apply the schema from `backend/`:

    .\.venv\Scripts\alembic.exe upgrade head

The API flow is:

1. `POST /api/v1/local/github/connection/verify` checks the server-configured
   repository and stores the project and repository connection.
2. `POST /api/v1/local/shoplite/proposals/{proposal_id}/validate` reads the
   fixed ShopLite runtime files at the current GitHub base SHA, stages them in
   the no-network Docker sandbox, and saves a report with the base SHA, diff
   hash, policy version, and checks.
3. Review the saved base SHA and diff hash. Then
   `POST /api/v1/local/github/draft-prs` requires the exact confirmation string
   `create-draft-pr` and an `Idempotency-Key`. It creates a branch, commit, and
   draft PR with the Git Database API. It never merges or deploys.
4. `GET /api/v1/local/github/operations/{operation_id}` reads the durable
   operation state. Reuse the same idempotency key to reconcile an unknown
   remote outcome.

Every call uses the `X-OmniSight-Local-Token` header. The Models page has a
local publishing panel for verification, validation, review, and confirmation.
The main scan screen is still simulated and does not create backend proposal
IDs; generate a real proposal through the backend patch flow first. The patch
proposal store itself is currently process-memory, so a server restart loses
the editable proposal even though its validation and publication records are
durable. Generate and publish within one backend process.

The existing provider tests use mocked GitHub responses. No live App
credentials, PostgreSQL instance, or sandbox image are configured here, so a
real repository write has not been run. Configure local secrets and use a
disposable test repository before enabling the publishing flag.
