# Week 4: GitHub App connection check

This slice adds backend-only GitHub App authentication and a read-only
repository verification command. It does not create branches or pull requests.
The JWT is short-lived, the installation token is restricted to the requested
repository, and this step asks GitHub for `contents: read` only. Credentials
and tokens are never returned in the verification report.

## Configure a GitHub App

Create or select a GitHub App for **GitHub.com**. Grant only these repository
permissions for the later delivery flow:

- **Contents:** read and write
- **Pull requests:** read and write
- **Metadata:** read (GitHub requires this)

Install it only on the repository or repositories OmniSight should access.
Download its private key and keep the PEM outside the repository. Do not paste
the key into chat, a pull request, or an issue.

Copy `backend/.env.example` to `backend/.env` and configure:

```dotenv
OMNISIGHT_ENABLE_GITHUB_APP=true
OMNISIGHT_GITHUB_APP_ID=YOUR_APP_ID
OMNISIGHT_GITHUB_APP_INSTALLATION_ID=YOUR_INSTALLATION_ID
OMNISIGHT_GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----
OMNISIGHT_GITHUB_TARGET_OWNER=YOUR_GITHUB_OWNER
OMNISIGHT_GITHUB_TARGET_REPOSITORY=YOUR_REPOSITORY
# Optional; defaults to the repository's configured default branch.
OMNISIGHT_GITHUB_TARGET_BRANCH=main
```

The target owner/repository/branch variables are read by the local verification
script and are not application settings. The private key is read only by the
backend. Keep `backend/.env` out of source control.

## Verify the installation

From the `backend` directory:

```powershell
.\.venv\Scripts\python.exe scripts\verify_github_app.py
```

A successful check prints the numeric repository ID, canonical full name,
default branch, selected base branch, base commit SHA, and verification time.
Permission errors are reported without returning GitHub's raw response body.

The corresponding tests use a generated RSA key and mocked GitHub responses;
they do not contact GitHub or need a live App installation.

## Current boundary

This repository does not yet have project-level user authentication or durable
repository-connection storage. For that reason, this credential-backed check is
a local backend command and is not exposed as an unauthenticated public API.
Branch/commit/PR writes remain disabled until their explicit-confirmation,
validation-freshness, idempotency, and durable-operation requirements are wired.
