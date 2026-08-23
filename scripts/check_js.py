#!/usr/bin/env python3
"""Syntax-check the frontend ES modules without needing Node.

The frontend has no build step, so nothing would catch a stray brace before it
reached the browser. This parses every ``web/js/**/*.js`` file with esprima
(pure Python) and reports syntax errors with line numbers.

esprima targets ES2017, so three newer constructs are *desugared in memory*
before parsing — the files on disk are never modified. The desugaring preserves
syntactic structure, which is all a syntax check cares about:

    a?.b   -> a.b        a ?? b -> a || b      catch {  -> catch (_e) {

Usage:  python scripts/check_js.py [--verbose]
Exit code is non-zero if any file fails, so it works as a pre-commit hook.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import esprima
except ImportError:  # pragma: no cover
    print("esprima is not installed. Run: pip install esprima")
    raise SystemExit(2)

WEB_JS = Path(__file__).resolve().parent.parent / "web" / "js"

# Order matters: the bracket/call forms must be rewritten before the bare `?.`.
_DESUGAR: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\?\.\["), "["),
    (re.compile(r"\?\.\("), "("),
    (re.compile(r"\?\?="), "||="),
    (re.compile(r"\?\?"), "||"),
    (re.compile(r"\?\."), "."),
    (re.compile(r"catch\s*\{"), "catch (_e) {"),
]


def desugar(source: str) -> str:
    for pattern, replacement in _DESUGAR:
        source = pattern.sub(replacement, source)
    return source


def check(path: Path) -> str | None:
    """Return an error string, or None if the file parses."""
    try:
        esprima.parseModule(desugar(path.read_text(encoding="utf-8")))
    except Exception as exc:  # esprima raises its own Error type
        line = getattr(exc, "lineNumber", None) or getattr(exc, "line", None)
        message = getattr(exc, "description", None) or str(exc)
        return f"line {line}: {message}" if line else message
    return None


# --------------------------------------------------------------------------
# API contract check
# --------------------------------------------------------------------------
def _split_top_level(text: str) -> list[str]:
    """Split an argument list on commas that are not nested or quoted."""
    parts, depth, quote, current = [], 0, None, ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                current += text[i : i + 2]
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            i += 1
            continue
        current += ch
        i += 1
    if current.strip():
        parts.append(current.strip())
    return parts


def _match_call(source: str, start: int) -> str | None:
    """Return the argument text of a call whose '(' is at ``start``."""
    depth, quote, i = 0, None, start
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : i]
        i += 1
    return None


def check_api_contract() -> list[str]:
    """Verify every ``api.foo(...)`` call matches the arity declared in api.js.

    A mismatch here is silent at runtime — the extra argument is simply dropped
    or lands in the wrong parameter, producing a malformed request body rather
    than an error. That is exactly the kind of bug a syntax check cannot see.
    """
    api_file = WEB_JS / "api.js"
    if not api_file.is_file():
        return [f"missing {api_file}"]

    src = api_file.read_text(encoding="utf-8")
    if "export const api = {" not in src:
        return ["api.js does not export an `api` object"]
    block = src.split("export const api = {", 1)[1]

    signatures: dict[str, tuple[int, int]] = {}   # name -> (min args, max args)
    for name, params in re.findall(r"^\s{2}([A-Za-z0-9_]+):\s*\(([^)]*)\)\s*=>", block, re.M):
        args = [a for a in _split_top_level(params) if a]
        optional = sum(1 for a in args if "=" in a)
        signatures[name] = (len(args) - optional, len(args))

    problems: list[str] = []
    for path in sorted(WEB_JS.rglob("*.js")):
        if path.name == "api.js":
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\bapi\.([A-Za-z0-9_]+)\s*\(", source):
            name = match.group(1)
            rel = path.relative_to(WEB_JS.parent.parent)
            line = source[: match.start()].count("\n") + 1

            if name not in signatures:
                problems.append(f"{rel}:{line}  api.{name}() is not defined in api.js")
                continue

            args_text = _match_call(source, match.end() - 1)
            if args_text is None:
                continue
            count = len(_split_top_level(args_text))
            low, high = signatures[name]
            if not (low <= count <= high):
                expected = f"{low}" if low == high else f"{low}-{high}"
                problems.append(
                    f"{rel}:{line}  api.{name}() called with {count} arg(s), api.js declares {expected}"
                )
    return problems


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    if not WEB_JS.is_dir():
        print(f"No such directory: {WEB_JS}")
        return 2

    files = sorted(WEB_JS.rglob("*.js"))
    if not files:
        print("No JavaScript files found.")
        return 1

    failures = 0
    for path in files:
        error = check(path)
        rel = path.relative_to(WEB_JS.parent.parent)
        if error:
            failures += 1
            print(f"  \033[31mFAIL\033[0m  {rel}\n        {error}")
        elif verbose:
            lines = path.read_text(encoding="utf-8").count("\n") + 1
            print(f"  \033[32m ok \033[0m  {rel}  ({lines} lines)")

    total = len(files)
    if failures:
        print(f"\n{failures} of {total} file(s) failed to parse.")
        return 1
    print(f"All {total} JavaScript file(s) parsed cleanly.")

    problems = check_api_contract()
    for problem in problems:
        print(f"  \033[31mFAIL\033[0m  {problem}")
    if problems:
        print(f"\n{len(problems)} API contract mismatch(es).")
        return 1
    print("API call sites match api.js signatures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
