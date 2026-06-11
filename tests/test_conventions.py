"""
Repo-convention enforcement tests.

These exist because two whole classes of bugs slipped in mechanically:

1. The stated Python 3.8 floor was unenforced — `Path.is_relative_to` (3.9+)
   shipped inside broad exception handling and silently broke all of
   nb-search on 3.8 (every query returned "no matches").
2. The "verbatim copy — keep in sync" contract between standalone scripts
   (no shared imports by design) had no teeth — copies drifted: a guarded
   os.stat in one script was unguarded in another, case-insensitivity was
   fixed in two scripts out of five.

Each test here turns one of those conventions from prose into a failure.
"""

import ast
import re
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SCRIPTS = sorted(SCRIPTS_DIR.glob("nb-*.py"))

# Python-3.9+ (or newer) stdlib APIs that must not appear in the scripts.
# Extend this list whenever a new too-new API is discovered.
BANNED_ATTRIBUTES = {
    "is_relative_to": "pathlib.PurePath.is_relative_to is Python 3.9+ "
                      "(use relative_to() + except ValueError)",
    "removeprefix":   "str.removeprefix is Python 3.9+",
    "removesuffix":   "str.removesuffix is Python 3.9+",
    "with_stem":      "pathlib.PurePath.with_stem is Python 3.9+",
    "readlink":       "pathlib.Path.readlink is Python 3.9+",
    "hardlink_to":    "pathlib.Path.hardlink_to is Python 3.10+",
}

# Functions that must be byte-identical across the scripts that carry them
# (the repo convention is verbatim copies instead of a shared module).
VERBATIM_FUNCTIONS = {
    "_is_git_root_entry": ["nb-read.py", "nb-search.py", "nb-index.py"],
    "_replace_with_retry": ["nb-write.py", "nb-index.py"],
    "_lock_file": ["nb-write.py", "nb-index.py"],
    "_unlock_file": ["nb-write.py", "nb-index.py"],
}


def _read(script_name):
    return (SCRIPTS_DIR / script_name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Python 3.8 floor
# ---------------------------------------------------------------------------

class TestPython38Floor:

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_parses_as_python_38(self, script):
        """Syntax-level floor: the file must parse as Python 3.8."""
        src = script.read_text(encoding="utf-8")
        try:
            ast.parse(src, filename=str(script), feature_version=(3, 8))
        except SyntaxError as e:
            pytest.fail(f"{script.name} uses post-3.8 syntax: {e}")

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_no_banned_apis(self, script):
        """API-level floor: known post-3.8 attribute APIs are banned."""
        src = script.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(script))
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRIBUTES:
                hits.append(f"{script.name}:{node.lineno} .{node.attr} — "
                            f"{BANNED_ATTRIBUTES[node.attr]}")
        assert not hits, "post-3.8 APIs found:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# Verbatim-copy contract
# ---------------------------------------------------------------------------

def _function_source(script_name, func_name):
    """Return the exact source segment of a module-level function."""
    src = _read(script_name)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            return ast.get_source_segment(src, node)
    return None


class TestVerbatimCopies:

    @pytest.mark.parametrize("func_name", sorted(VERBATIM_FUNCTIONS),
                             ids=str)
    def test_copies_are_byte_identical(self, func_name):
        carriers = VERBATIM_FUNCTIONS[func_name]
        versions = {}
        for script_name in carriers:
            seg = _function_source(script_name, func_name)
            assert seg is not None, (
                f"{script_name} is expected to carry {func_name} "
                f"(verbatim-copy contract) but does not define it")
            versions[script_name] = seg
        unique = set(versions.values())
        assert len(unique) == 1, (
            f"{func_name} has drifted between "
            f"{', '.join(versions)} — the copies must be byte-identical "
            f"(repo convention: verbatim copies, no shared imports)")


# ---------------------------------------------------------------------------
# Explicit-encoding convention
# ---------------------------------------------------------------------------

def _mode_is_binary(call):
    """Best-effort: does this open()/fdopen() call use a binary mode?"""
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and "b" in mode


def _has_kw(call, name):
    return any(kw.arg == name for kw in call.keywords)


class TestExplicitEncoding:

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_text_io_passes_encoding(self, script):
        """Every text-mode open()/os.fdopen()/read_text()/write_text() must
        pass encoding= explicitly (Windows defaults to the locale codepage)."""
        src = script.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(script))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = None
            if isinstance(fn, ast.Name) and fn.id == "open":
                name = "open"
            elif isinstance(fn, ast.Attribute) and fn.attr in (
                    "fdopen", "read_text", "write_text"):
                name = fn.attr
            if name is None:
                continue
            if name in ("open", "fdopen") and _mode_is_binary(node):
                continue
            if not _has_kw(node, "encoding"):
                offenders.append(f"{script.name}:{node.lineno} {name}() "
                                 f"without encoding=")
        assert not offenders, (
            "text I/O without explicit encoding:\n" + "\n".join(offenders))

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_data_file_writes_pin_newline(self, script):
        """Text-mode *write* opens must pin newline='\\n' so Windows does not
        CRLF-translate notebooks/indexes (size/hash divergence)."""
        src = script.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(script))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_open = isinstance(fn, ast.Name) and fn.id == "open"
            is_fdopen = isinstance(fn, ast.Attribute) and fn.attr == "fdopen"
            if not (is_open or is_fdopen):
                continue
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if not (isinstance(mode, str) and "w" in mode and "b" not in mode):
                continue
            if not _has_kw(node, "newline"):
                offenders.append(f"{script.name}:{node.lineno}")
        assert not offenders, (
            "text-mode write without newline= (CRLF hazard on Windows):\n"
            + "\n".join(offenders))


# ---------------------------------------------------------------------------
# Test-suite conventions
# ---------------------------------------------------------------------------

class TestSuiteConventions:

    def test_no_python3_literal_in_test_runners(self):
        """Test helpers must spawn scripts with sys.executable, never a
        'python3' literal (absent on stock Windows; escapes the venv)."""
        tests_dir = Path(__file__).parent
        offenders = []
        # Only flag UPPER_CASE constant assignments (the run-helper pattern);
        # kernel_language="python" and similar fixture kwargs are legitimate.
        pattern = re.compile(r'^\s*[A-Z][A-Z_]*\s*=\s*["\']python3?["\']')
        for tf in sorted(tests_dir.glob("test_*.py")):
            for i, line in enumerate(
                    tf.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{tf.name}:{i}: {line.strip()}")
        assert not offenders, (
            "python literal instead of sys.executable:\n"
            + "\n".join(offenders))
