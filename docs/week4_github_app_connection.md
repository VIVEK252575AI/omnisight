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

The target owner/repository/branch variables are read by the local verification
script and are not application settings. The private key is read only by the
backend. Keep backend/.env out of source control.

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

## Draft publishing boundary

This repository does not yet have project-level user authentication or durable
repository-connection storage. For that reason, both operations remain backend
commands/internal adapter methods and are not exposed as an unauthenticated
public API. The publisher accepts only one exact edit to
styles/checkout.css, rechecks the repository ID and validated base SHA, and
requires an explicit confirmation flag from its caller. It creates Git blobs,
a tree, a commit, and a dedicated ref with the Git Database API. A retry checks
the deterministic branch's tree and reuses the matching open draft PR; a branch
collision or changed base fails closed. Ambiguous network outcomes return a
retry-safe error so the same operation can reconcile the remote state.

The publisher unit tests use mocked GitHub responses. They cover write scopes,
Git Database object construction, confirmation, stale-base rejection, and
idempotent draft PR creation. No live GitHub write has been run. A user-facing
workflow still needs authenticated project membership, durable operation
records, a verified sandbox report bound to the proposal/base SHA, and a
confirmation screen before it can call this adapter.
