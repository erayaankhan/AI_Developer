"""Test support for item 21 (`--yes` needs `--confirm <hash>`): do what the workflow does, preview first, then apply with
the hash the preview returned. Not used by the scripts themselves."""
import contextlib
import io
import json


def preview_hash(run_preview):
    """Run `run_preview()` (a command call without --yes) and return the `confirm` hash of its RESULT, else None
    (the command refused or has no confirm step)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        run_preview()
    lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")]
    return json.loads(lines[-1][len("RESULT: "):]).get("confirm") if lines else None
