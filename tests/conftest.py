import os
import subprocess as _subprocess

# Ensure child Python processes use UTF-8 for stdin/stdout/stderr on all platforms.
os.environ.setdefault("PYTHONUTF8", "1")

# Patch subprocess.run so the parent test process also decodes subprocess output
# as UTF-8 when text=True is used (Windows otherwise defaults to cp1252).
_orig_run = _subprocess.run


def _run_utf8(*args, **kwargs):
    if kwargs.get("text") and "encoding" not in kwargs:
        kwargs["encoding"] = "utf-8"
    return _orig_run(*args, **kwargs)


_subprocess.run = _run_utf8
