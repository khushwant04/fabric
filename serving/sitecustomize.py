"""Applies the Fabric kernel substitution in every interpreter that starts.

Python imports this automatically at startup when it is importable, which is the only
placement that reaches the process running the model: vLLM runs its engine in a child
process, so a patch applied by the process that starts the server would not be present
where decoding actually happens.

It does nothing unless FABRIC_KERNEL is set, so the same image serves both an unmodified
vLLM and a substituted one, and a comparison between them differs in that variable alone.
"""

from __future__ import annotations

import os
import sys

if os.environ.get("FABRIC_KERNEL", "").strip().lower() in {"1", "true", "yes", "on"}:
    try:
        from fabric_serving.register import install

        result = install()
        # Printed rather than logged: at interpreter startup no logging is configured yet,
        # and a substitution that silently did not happen is the failure worth catching.
        print(
            f"[fabric] kernel substitution armed pid={os.getpid()} "
            f"tile={result['tile']} already_imported={result['patched_immediately']}",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        # Refusing to start would be worse than serving vLLM's own kernel, but a silent
        # fallback would make a comparison meaningless, so it is loud.
        print(f"[fabric] KERNEL SUBSTITUTION FAILED: {exc!r}", file=sys.stderr, flush=True)
