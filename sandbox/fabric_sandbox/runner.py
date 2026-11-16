"""Child bootstrap: runs ONE execution of user code. Started as ``python -I -S runner.py CFG``.

Executed as a script in a fresh interpreter for every execution, never imported by the
service. The order matters:

1. mark the process non-dumpable, so other code running under the same uid (a concurrent
   execution of another run) cannot read this process's memory, environment or working
   directory through ``/proc`` or attach with ptrace;
2. apply resource limits (hard == soft, so user code cannot raise them);
3. only then add the scientific site directories (``-S`` skipped ``site`` at startup, so no
   ``sitecustomize``/``usercustomize`` hook ever runs) and read the code from stdin;
4. run the code as ``__main__``; afterwards save any open matplotlib figures as PNGs when
   the code did not write one itself.

Stdin is replaced by /dev/null before user code runs, so ``input()`` sees end-of-file.
"""

import ctypes
import json
import os
import resource
import sys
import traceback

_PR_SET_DUMPABLE = 4


def _non_dumpable() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):  # not Linux: nothing to do
        pass


def _limit(name: str, value: int, hard: int | None = None) -> None:
    kind = getattr(resource, name, None)
    if kind is None or value <= 0:
        return
    try:
        resource.setrlimit(kind, (value, hard if hard is not None else value))
    except (ValueError, OSError):
        # Never raise above an inherited hard limit; keep the stricter one.
        current = resource.getrlimit(kind)[1]
        if current != resource.RLIM_INFINITY:
            resource.setrlimit(kind, (min(value, current), current))


def _pngs() -> set[str]:
    try:
        return {entry for entry in os.listdir(".") if entry.lower().endswith(".png")}
    except OSError:
        return set()


def _save_figures(before: set[str]) -> None:
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None or _pngs() - before:
        return
    try:
        for number in plt.get_fignums():
            plt.figure(number).savefig(f"figure_{number}.png", dpi=100)
        plt.close("all")
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never fatal
        print(f"[sandbox] could not save figures: {exc}", file=sys.stderr)


def main() -> int:
    config = json.loads(sys.argv[1])
    _non_dumpable()
    os.umask(0o077)
    _limit("RLIMIT_CORE", 0)
    _limit("RLIMIT_CPU", int(config["cpu_seconds"]), int(config["cpu_seconds"]) + 1)
    _limit("RLIMIT_AS", int(config["memory_bytes"]))
    _limit("RLIMIT_FSIZE", int(config["file_size_bytes"]))
    _limit("RLIMIT_NOFILE", int(config["open_files"]))
    _limit("RLIMIT_NPROC", int(config["max_processes"]))

    code = sys.stdin.read()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdin = open(os.devnull)  # noqa: SIM115 - lives for the process

    import site  # noqa: PLC0415 - deliberately after the limits

    for directory in config.get("site_dirs") or []:
        if os.path.isdir(directory):
            site.addsitedir(directory)
    sys.argv = ["<code>"]
    sys.path.insert(0, os.getcwd())

    before = _pngs()
    status = 0
    try:
        compiled = compile(code, "<code>", "exec")
        exec(compiled, {"__name__": "__main__", "__builtins__": __builtins__})  # noqa: S102
    except SystemExit as exc:
        code_ = exc.code
        status = code_ if isinstance(code_, int) else (0 if code_ is None else 1)
        if code_ is not None and not isinstance(code_, int):
            print(code_, file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001 - user code may raise anything
        # Drop this file's frame so the traceback starts at the user's code.
        tb = exc.__traceback__.tb_next if exc.__traceback__ else None
        traceback.print_exception(type(exc), exc, tb)
        status = 1
    _save_figures(before)
    sys.stdout.flush()
    sys.stderr.flush()
    return status


if __name__ == "__main__":
    sys.exit(main())
