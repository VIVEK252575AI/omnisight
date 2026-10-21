"""Structural checks on proposed patch code (Week 2, W2.6).

These checks **never execute, import or evaluate** the text they inspect. They
are pure Python string scans, applied to the *inserted* text (``new_text``) of
each edit — never to ``old_text``, which is code that already exists in the tree
and may legitimately contain APIs that must not be *introduced*.

Why heuristics, not a real parser
---------------------------------
The backend ships no CSS/JS parser and adding one is out of scope (no new
dependency without justification and approval). So this module performs
structural *heuristics*:

- delimiter balance (``{}``/``()``/``[]``), ignoring string literals;
- a JSX tag-balance heuristic, only when a closing tag (``</``) is present;
- a forbidden-construct scan.

Deep syntax validation is deferred to the **W3.3 sandbox build**, where the
candidate tree is actually compiled. Anything this module cannot confirm is
rejected (``forbidden_construct``): a false rejection is cheaper than an unsafe
acceptance.

Fragment handling
-----------------
A minimal edit is often a fragment (e.g. replacing ``height: 0;`` with
``height: auto;``) that legitimately contains no braces. The balance check is
therefore only applied when the text *opens* a structure it must also close; a
bracket-free fragment only gets the forbidden-construct scan.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.patch_schemas import RejectionReason

CSS_EXTENSIONS = frozenset({".css", ".scss"})
JS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx"})

_OPENERS = {"{": "}", "(": ")", "[": "]"}
_CLOSERS = {"}": "{", ")": "(", "]": "["}
_QUOTES = ("\"", "'", "`")

# --- CSS forbidden constructs -------------------------------------------------

# An external @import (http/https or protocol-relative) would pull remote bytes.
_CSS_EXTERNAL_IMPORT = re.compile(
    r"@import\s+(?:url\(\s*)?['\"]?(?:https?:)?//", re.IGNORECASE
)
_CSS_EXPRESSION = re.compile(r"\bexpression\s*\(", re.IGNORECASE)
_CSS_JAVASCRIPT = re.compile(r"javascript\s*:", re.IGNORECASE)

# --- JS/TS/JSX forbidden constructs ------------------------------------------

_JS_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\beval\s*\("), "eval"),
    (re.compile(r"\bFunction\s*\("), "Function"),
    (re.compile(r"\bchild_process\b"), "child_process"),
    # Any string literal naming fs / node:fs (require, import, dynamic import).
    (re.compile(r"""['\"](?:node:)?fs['\"]"""), "fs"),
    (re.compile(r"\bXMLHttpRequest\b"), "XMLHttpRequest"),
    (re.compile(r"<\s*script\b", re.IGNORECASE), "script tag"),
    (re.compile(r"\bdocument\.write\b"), "document.write"),
    (re.compile(r"dangerouslySetInnerHTML"), "dangerouslySetInnerHTML"),
    # A fetch() to an absolute URL (external origin) leaves the sandbox.
    (re.compile(r"\bfetch\s*\(\s*['\"`]\s*(?:https?:)?//"), "external fetch"),
    (re.compile(r"\bimport\s*\(\s*['\"`]\s*(?:https?:)?//"), "external dynamic import"),
)

# JSX tag heuristic. A real element tag must be ``<Name...>`` / ``</Name>`` /
# ``<Name.../>``; the name starts immediately after ``<`` or ``</`` (no space),
# so a comparison like ``a < b && c > d`` is never mistaken for a tag. React
# fragments (``<>`` / ``</>``) are matched verbatim by a separate pattern.
_JSX_TAG = re.compile(
    r"""<(/?)([A-Za-z][\w.]*)(?:[^<>'"]|'[^']*'|"[^"]*")*?(/?)>""",
)
_JSX_FRAGMENT = re.compile(r"</>|<>")


def is_balanced(text: str) -> bool:
    """True when all ``{}``/``()``/``[]`` are balanced outside string literals.

    String literals delimited by ``"``, ``'`` or a backtick are skipped, with
    ``\\`` escaping honoured. An unterminated string counts as unbalanced.
    """
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for char in text:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in _QUOTES:
            quote = char
        elif char in _OPENERS:
            stack.append(char)
        elif char in _CLOSERS:
            if not stack or stack[-1] != _CLOSERS[char]:
                return False
            stack.pop()
    return not stack and quote is None


def _opens_structure(text: str) -> bool:
    """True when ``text`` opens a delimiter it is then expected to close."""
    return any(opener in text for opener in _OPENERS)


def _first_forbidden(text: str, patterns: tuple[tuple[re.Pattern[str], str], ...]) -> str | None:
    for pattern, label in patterns:
        if pattern.search(text):
            return label
    return None


def _jsx_balanced(text: str) -> bool:
    """Best-effort JSX tag balance; only meaningful for tag-heavy fragments."""
    stack: list[str] = []
    # Collect element tags and fragments in source order.
    hits: list[tuple[int, str, bool, bool]] = []
    for match in _JSX_TAG.finditer(text):
        closing, name, self_closing = match.group(1), match.group(2), match.group(3)
        hits.append((match.start(), name, bool(closing), bool(self_closing)))
    for match in _JSX_FRAGMENT.finditer(text):
        closing = match.group(0) == "</>"
        hits.append((match.start(), "", closing, False))
    for _, name, closing, self_closing in sorted(hits):
        if closing:
            if not stack or stack.pop() != name:
                return False
        elif not self_closing:
            stack.append(name)
    return not stack


def check_css(text: str) -> RejectionReason | None:
    """Structural check for a CSS/SCSS payload (no execution)."""
    if (
        _CSS_EXPRESSION.search(text)
        or _CSS_JAVASCRIPT.search(text)
        or _CSS_EXTERNAL_IMPORT.search(text)
    ):
        return RejectionReason.forbidden_construct
    if _opens_structure(text) and not is_balanced(text):
        return RejectionReason.forbidden_construct
    return None


def check_js(text: str) -> RejectionReason | None:
    """Structural check for a JS/TS/JSX payload (no execution)."""
    if _first_forbidden(text, _JS_FORBIDDEN) is not None:
        return RejectionReason.forbidden_construct
    if _opens_structure(text) and not is_balanced(text):
        return RejectionReason.forbidden_construct
    if "</" in text and not _jsx_balanced(text):
        return RejectionReason.forbidden_construct
    return None


def check_payload(relpath: str, code: str) -> RejectionReason | None:
    """Validate the *inserted* code for ``relpath``; None when it is acceptable.

    An empty payload is a deletion (``new_text == ""``) and is allowed — there
    is no code to inspect. Unknown extensions are handled by the path layer
    (``unsupported_file_type``) and are not judged here.
    """
    if not code.strip():
        return None
    suffix = PurePosixPath(relpath).suffix.lower()
    if suffix in CSS_EXTENSIONS:
        return check_css(code)
    if suffix in JS_EXTENSIONS:
        return check_js(code)
    return None
