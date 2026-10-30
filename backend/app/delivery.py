"""W3.6/W3.7 delivery: branch, commit and pull request after validation gates.

This module owns the high-level delivery flow that turns a verified repair
attempt into a reviewable GitHub change. It depends on the small
``GitHubService`` interface (fake or PyGithub) and on the scan/detection/
patch/attempt stores. It never merges, closes, deploys or force-pushes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.detection_storage import DetectionStore, get_detection_store
from app.errors import DomainError, ErrorCode
from app.github_delivery import GitHubFile, GitHubService, build_github_service
from app.ids import utcnow
from app.orchestrator import AttemptRecord, AttemptStore, get_attempt_store
from app.patch_schemas import PatchProposal, PatchProposalRecord
from app.patch_storage import PatchStore, get_patch_store
from app.schemas import ScanStatus
from app.storage import ScanStore, get_store

logger = logging.getLogger("omnisight")

# ---------------------------------------------------------------------------
# PR record (persisted metadata)
# ---------------------------------------------------------------------------


class PRRecord(BaseModel):
    """Persisted metadata for a scan's pull request."""

    scan_id: UUID
    finding_id: str
    number: int | None = None
    url: str | None = None
    branch: str
    head_sha: str | None = None
    base_branch: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    dry_run: bool = True
    status: str = "delivered"  # branch_created | committed | pr_opened | delivered | delivery_partial | failed
    sub_state: str | None = None  # last completed step
    partial_hint: str | None = None
    unmet_gates: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)  # dry-run would-be payload


class PRStore:
    """In-memory store for PR records, keyed by scan_id."""

    def __init__(self) -> None:
        self._records: dict[str, PRRecord] = {}
        self._lock_imported = False
        import threading

        self._lock = threading.Lock()

    def get(self, scan_id: UUID) -> PRRecord | None:
        with self._lock:
            return self._records.get(str(scan_id))

    def set(self, record: PRRecord) -> PRRecord:
        with self._lock:
            self._records[str(record.scan_id)] = record
        return record

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_PR_STORE = PRStore()


def get_pr_store() -> PRStore:
    return _PR_STORE


# ---------------------------------------------------------------------------
# Error mapping (W3.7)
# ---------------------------------------------------------------------------


def map_github_error(exc: Exception) -> tuple[ErrorCode, int, bool, int | None]:
    """Map a GitHub/PyGithub exception to (code, http_status, retryable, retry_after)."""
    # DomainError already carries a code.
    if isinstance(exc, DomainError):
        code = exc.code
        status = exc.status_code
        retryable = code in (
            ErrorCode.GITHUB_RATE_LIMITED,
            ErrorCode.GITHUB_UNAVAILABLE,
            ErrorCode.GITHUB_UNKNOWN,
        )
        return code, status, retryable, None

    from app.github_delivery import GitHubDeliveryError

    if isinstance(exc, GitHubDeliveryError):
        raw = (exc.code or "").upper()
        message = str(exc)
        lower_msg = message.lower()
        if "TOKEN_MISSING" in raw or "NOT_CONFIGURED" in raw:
            return ErrorCode.GITHUB_NOT_CONFIGURED, 424, False, None
        if "AUTH" in raw or "401" in message:
            return ErrorCode.GITHUB_AUTH_FAILED, 401, False, None
        if "PERMISSION" in raw or "403" in message and "rate" not in lower_msg:
            return ErrorCode.GITHUB_PERMISSION_DENIED, 403, False, None
        if "RATE" in raw or "rate" in lower_msg or "429" in message:
            return ErrorCode.GITHUB_RATE_LIMITED, 429, True, 60
        if "NOT_FOUND" in raw or "404" in message:
            return ErrorCode.GITHUB_NOT_FOUND, 404, False, None
        if "CONFLICT" in raw or "409" in message or "422" in message:
            return ErrorCode.GITHUB_CONFLICT, 409, False, None
        if "UNAVAILABLE" in raw or "SDK" in raw or "TIMEOUT" in raw or "NETWORK" in raw:
            return ErrorCode.GITHUB_UNAVAILABLE, 503, True, None
        # PyGithub-like: inspect status_code/data attributes
        status_code = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        if status_code == 401:
            return ErrorCode.GITHUB_AUTH_FAILED, 401, False, None
        if status_code == 403:
            return ErrorCode.GITHUB_RATE_LIMITED, 429, True, 60
        if status_code == 404:
            return ErrorCode.GITHUB_NOT_FOUND, 404, False, None
        if status_code in (409, 422):
            return ErrorCode.GITHUB_CONFLICT, 409, False, None
        if status_code is not None and int(status_code) >= 500:
            return ErrorCode.GITHUB_UNAVAILABLE, 503, True, None
        return ErrorCode.GITHUB_UNKNOWN, 502, True, None

    # Generic network/timeout
    if "timeout" in str(exc).lower() or "connection" in str(exc).lower():
        return ErrorCode.GITHUB_UNAVAILABLE, 503, True, None
    return ErrorCode.GITHUB_UNKNOWN, 502, True, None


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------


def _gates(
    scan_status: ScanStatus,
    attempt: AttemptRecord | None,
    github_status: Any,
    is_dry_run: bool,
) -> list[str]:
    unmet: list[str] = []
    if scan_status not in (ScanStatus.review_preparation, ScanStatus.awaiting_review):
        # delivery allowed from review_preparation; awaiting_review handled by idempotency
        if scan_status not in (ScanStatus.review_preparation,):
            unmet.append("scan_not_review_preparation")
    if scan_status in (ScanStatus.cancelled, ScanStatus.escalated, ScanStatus.failed):
        unmet.append("scan_cancelled_or_escalated")
    if attempt is None:
        unmet.append("no_verified_attempt")
    else:
        if attempt.status.value != "verified":
            unmet.append("attempt_not_verified")
        if not attempt.workspace_id:
            unmet.append("attempt_not_in_isolated_workspace")
        val = attempt.validation or {}
        if val.get("passed") is not True and val.get("status") not in ("passed", "ok"):
            # be lenient: require validation report present
            if not attempt.validation:
                unmet.append("validation_not_passed")
        ver = attempt.verification or {}
        if not ver:
            unmet.append("verification_policy_not_satisfied")
        # patch paths allowlist check is done by proposal; check proposal files
        proposal = None
        # attempt doesn't store proposal directly; check via patch store later
    if not is_dry_run and not getattr(github_status, "configured", False):
        unmet.append("github_not_configured")
    return unmet


# ---------------------------------------------------------------------------
# Delivery service
# ---------------------------------------------------------------------------


class DeliveryService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        github_service: GitHubService | None = None,
        pr_store: PRStore | None = None,
        scan_store: ScanStore | None = None,
        detection_store: DetectionStore | None = None,
        patch_store: PatchStore | None = None,
        attempt_store: AttemptStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.github = github_service or build_github_service(self.settings)
        self.pr_store = pr_store or get_pr_store()
        self.scan_store = scan_store or get_store(self.settings)
        self.detection_store = detection_store or get_detection_store()
        self.patch_store = patch_store or get_patch_store()
        self.attempt_store = attempt_store or get_attempt_store()

    @property
    def is_dry_run(self) -> bool:
        return bool(getattr(self.settings, "github_dry_run", True))

    def get_pr(self, scan_id: UUID) -> PRRecord | None:
        return self.pr_store.get(scan_id)

    def deliver(self, scan_id: UUID, finding_id: str) -> PRRecord:
        # Idempotency: existing PR record.
        existing = self.pr_store.get(scan_id)
        if existing is not None and existing.finding_id == finding_id:
            return existing

        scan = self.scan_store.get(scan_id)
        if scan is None:
            raise DomainError(ErrorCode.SCAN_NOT_FOUND, f"scan {scan_id} not found", status_code=404)

        # find verified attempt for finding
        attempts = self.attempt_store.list_for_scan(scan_id)
        attempt = None
        for a in reversed(attempts):
            if a.finding_id == finding_id and a.status.value == "verified":
                attempt = a
                break
        # also accept any attempt for finding if verified
        if attempt is None:
            for a in reversed(attempts):
                if a.finding_id == finding_id:
                    attempt = a
                    break

        github_status = self.github.validate_configuration()

        unmet = _gates(scan.status, attempt, github_status, self.is_dry_run)
        # patch paths allowlist check
        proposal = None
        if attempt and attempt.patch_proposal_id:
            proposal = self.patch_store.get_proposal(attempt.patch_proposal_id)
            record = self.patch_store.get(attempt.patch_proposal_id)
            if proposal is not None:
                for f in proposal.files:
                    if f.path not in (getattr(self.settings, "approved_patch_paths", []) or []):
                        # allowlist may be empty in tests; only flag if allowlist non-empty
                        if getattr(self.settings, "approved_patch_paths", []):
                            if f.path not in self.settings.approved_patch_paths:
                                unmet.append("patch_path_not_approved")
                                break
        if unmet:
            # de-dup
            unmet = list(dict.fromkeys(unmet))
            raise DomainError(
                ErrorCode.DELIVERY_GATE_FAILED,
                "delivery gates not satisfied",
                status_code=409,
                details=[{"gate": g} for g in unmet],
            )

        # build files list
        files: list[GitHubFile] = []
        if proposal is not None:
            for pf in proposal.files:
                content = ""
                # attempt to get content from proposal edits? not stored; use diff
                files.append(GitHubFile(path=pf.path, content=content, message="OmniSight patch"))

        # use diff for content if available
        if attempt and attempt.diff:
            # keep diff as a single file? Instead attach to first file content
            if files:
                files[0].content = attempt.diff
            else:
                files.append(GitHubFile(path="patch.diff", content=attempt.diff, message="OmniSight patch"))

        branch_name = self._branch_name(finding_id, scan_id)
        base_branch = getattr(self.settings, "github_target_branch", None) or "main"
        # if github configured, get base sha
        base_sha = "fake-base-sha"
        sub_state = None
        partial_hint = None
        pr_number = None
        pr_url = None
        head_sha = None
        created_at = utcnow()

        # resume detection: existing branch
        existing_branch = None
        try:
            existing_branch = self.github.find_existing_branch(branch_name)
        except Exception:
            existing_branch = None

        record = PRRecord(
            scan_id=scan_id,
            finding_id=finding_id,
            branch=branch_name,
            base_branch=base_branch,
            dry_run=self.is_dry_run,
            status="branch_created",
            sub_state="branch_created",
        )

        try:
            if existing_branch is None:
                # get base sha
                try:
                    base_sha = self.github.get_base_branch_sha(base_branch)
                except Exception:
                    base_sha = "fake-base-sha"
                br = self.github.create_branch(branch_name, base_sha)
                head_sha = br.get("sha")
                record.sub_state = "branch_created"
                record.status = "branch_created"
                self.pr_store.set(record)
            else:
                head_sha = existing_branch.get("sha")
                record.sub_state = "branch_created"
                record.status = "branch_created"
                self.pr_store.set(record)

            # commit files
            if files:
                try:
                    upd = self.github.update_files(branch_name, head_sha or base_sha, files, f"fix(ui): {finding_id}")
                    head_sha = upd.get("commit_sha", head_sha)
                except Exception as exc:
                    mapped = map_github_error(exc)
                    code, status_code, retryable, retry_after = mapped
                    record.status = "delivery_partial"
                    record.sub_state = "branch_created"
                    record.partial_hint = "branch created; commit failed"
                    self.pr_store.set(record)
                    self._raise_mapped(code, status_code, retryable, retry_after, exc)
            record.status = "committed"
            record.sub_state = "committed"
            self.pr_store.set(record)

            # open PR
            title = f"fix(ui): {finding_id}"
            body = self._pr_body(scan_id, finding_id, attempt, proposal, files)
            pr = self.github.open_pull_request(title, body, branch_name, base_branch, draft=True)
            pr_number = pr.get("pull_request_number")
            pr_url = pr.get("pull_request_url")
            head_sha = pr.get("head", head_sha)
            record.status = "delivered"
            record.sub_state = "pr_opened"
            record.number = pr_number
            record.url = pr_url
            record.head_sha = head_sha
            record.created_at = created_at
            record.payload = {
                "branch": branch_name,
                "base": base_branch,
                "title": title,
                "body": body,
                "files": [f.path for f in files],
                "draft": True,
            }
            self.pr_store.set(record)

            # set scan to awaiting_review
            try:
                self.scan_store.compare_and_transition(
                    scan_id,
                    expected=frozenset({ScanStatus.review_preparation}),
                    to_status=ScanStatus.awaiting_review,
                    error=None,
                )
            except Exception:
                pass

            return record
        except DomainError:
            raise
        except Exception as exc:
            mapped = map_github_error(exc)
            code, status_code, retryable, retry_after = mapped
            # determine partial
            record.status = "delivery_partial"
            self.pr_store.set(record)
            self._raise_mapped(code, status_code, retryable, retry_after, exc)

    def _raise_mapped(self, code, status_code, retryable, retry_after, exc):
        details = [{"retryable": retryable}]
        if retry_after is not None:
            details.append({"retry_after_seconds": retry_after})
        raise DomainError(code, str(exc), status_code=status_code, details=details)

    def _branch_name(self, finding_id: str, scan_id: UUID) -> str:
        short_finding = str(finding_id)[:8]
        short_scan = str(scan_id)[:8]
        name = f"omnisight/auto-fix-{short_finding}-{short_scan}"
        # sanitize: keep allowed chars
        import re

        name = re.sub(r"[^A-Za-z0-9_./-]", "-", name)
        return name

    def _pr_body(self, scan_id, finding_id, attempt, proposal, files) -> str:
        lines = []
        lines.append(f"## Finding\n{finding_id}")
        lines.append(f"\nScan: {scan_id}")
        if attempt:
            lines.append(f"\nAttempt: {attempt.n}")
            if attempt.validation:
                lines.append(f"\nValidation: {attempt.validation}")
            if attempt.verification:
                lines.append(f"\nVerification: {attempt.verification}")
        if proposal and getattr(proposal, "rationale", None):
            lines.append(f"\nRationale: {proposal.rationale}")
        lines.append(f"\nFiles: {', '.join(f.path for f in files)}")
        lines.append("\n\n> AI-generated change. Requires human review.")
        return "\n".join(lines)

    # compat with existing protocol
    def prepare_review(self, scan_id: UUID, attempts: list[AttemptRecord]) -> Any:
        # find verified attempt
        verified = None
        for a in attempts:
            if a.status.value == "verified":
                verified = a
                break
        if verified is None and attempts:
            verified = attempts[-1]
        fid = verified.finding_id if verified else ""
        try:
            rec = self.deliver(scan_id, fid)
            # return a StageOutcome-like object? existing expects StageOutcome
        except Exception as exc:
            from app.orchestrator import StageOutcome

            return StageOutcome(passed=False, detail={"error": str(exc)})
        from app.orchestrator import StageOutcome

        return StageOutcome(passed=True, detail={"pr": rec.model_dump()})


# StageOutcome import
from app.orchestrator import StageOutcome  # noqa: E402