"""Adapter for the PipeWire tools (pw-dump, pw-link).

Links are created and removed by the numeric port ids from pw-dump — "node:port"
names are not unique (e.g. several discord_capture nodes).

pw-link -L (linger) is the default: the link outlives pw-link and disappears by
itself when either port goes away. Our links are marked with the MANAGED_KEY
property (pw-link -p) so they can be found later. Recreated originals (from the
snapshot) get RESTORED_KEY instead — restore does not remove them, but we know
we created them.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
from typing import Any

from .graph import MANAGED_KEY, RESTORED_KEY
from .i18n import _

MONITOR_ARGV = ["pw-dump", "-m", "-N"]
PR_SET_PDEATHSIG = 1


def _load_prctl():
    """libc prctl(), resolved once in the parent (dlopen/dlsym are not safe in a
    forked child of a multi-threaded process). None if unavailable."""
    try:
        fn = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    return fn


_prctl = _load_prctl()


def die_with_parent_preexec():
    """preexec_fn for a helper child: it gets SIGTERM when the thread that started
    it dies — also after SIGKILL or a crash of the tool, when our own cleanup never
    runs. Without it `pw-dump -m -N` would outlive us (re-parented to systemd --user).

    PR_SET_PDEATHSIG fires when the *thread* that forked the child ends; the
    Watcher thread owns the monitor for its whole life and stops it itself, so
    that is the same moment. Returns None (no preexec_fn) where prctl is missing."""
    if _prctl is None:
        return None
    prctl = _prctl
    parent = os.getpid()
    sig = int(signal.SIGTERM)

    def preexec() -> None:
        # Only async-signal-safe-ish work here: one pre-resolved libc call.
        prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0)
        if os.getppid() != parent:   # the parent died before prctl took effect
            os.kill(os.getpid(), sig)

    return preexec


class PwError(Exception):
    """Error from the PipeWire tools. The message is user-facing (translated)."""


def _short(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


class PwCli:
    def __init__(self, timeout: float = 8.0, owner_value: str = "true") -> None:
        self.timeout = timeout
        self.owner_value = owner_value

    def _run(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args, capture_output=True, text=True,
                timeout=timeout or self.timeout, check=False,
            )
        except FileNotFoundError as e:
            raise PwError(_("Program {program} not found — are the PipeWire tools (pipewire-utils) installed?")
                          .format(program=args[0])) from e
        except subprocess.TimeoutExpired as e:
            raise PwError(_("{program} did not answer within {seconds:.0f} s — is PipeWire running?")
                          .format(program=args[0], seconds=timeout or self.timeout)) from e
        except OSError as e:
            raise PwError(_("Can't run {program}: {error}").format(program=args[0], error=e)) from e

    # ------------------------------------------------------------------ reading
    def dump(self) -> list[dict]:
        r = self._run(["pw-dump", "-N"])
        if r.returncode != 0:
            raise PwError(_("Can't read the PipeWire state (pw-dump, exit code {code}): {details}")
                          .format(code=r.returncode, details=_short(r.stderr)))
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise PwError(_("pw-dump returned invalid data — is PipeWire running?")) from e
        if not isinstance(data, list):
            raise PwError(_("pw-dump returned data in an unexpected format."))
        return data

    # ------------------------------------------------------------------ changes
    def link(self, out_port: int, in_port: int, managed: bool = True) -> str:
        """Create a link. Returns 'created' or 'exists'.

        managed=True: our extra link (removed on restore);
        managed=False: a recreated original (kept after restore)."""
        args = ["pw-link", "-L"]
        key = MANAGED_KEY if managed else RESTORED_KEY
        args += ["-p", json.dumps({key: self.owner_value})]
        args += [str(int(out_port)), str(int(in_port))]
        r = self._run(args)
        if r.returncode == 0:
            return "created"
        if "File exists" in (r.stderr or "") or "File exists" in (r.stdout or ""):
            return "exists"
        raise PwError(_("pw-link failed to link ports {src} -> {dst}: {details}")
                      .format(src=out_port, dst=in_port, details=_short(r.stderr or r.stdout)))

    def unlink(self, out_port: int, in_port: int) -> str:
        """Remove the link between two ports. Returns 'deleted' or 'missing'."""
        r = self._run(["pw-link", "-d", str(int(out_port)), str(int(in_port))])
        if r.returncode == 0:
            return "deleted"
        err = (r.stderr or "") + (r.stdout or "")
        if "No such file" in err or "not found" in err.lower():
            return "missing"
        raise PwError(_("pw-link failed to unlink ports {src} -> {dst}: {details}")
                      .format(src=out_port, dst=in_port, details=_short(err)))

    # ---------------------------------------------------------------- monitor
    def start_monitor(self) -> Any:
        """Start `pw-dump -m -N` (a JSON stream of graph changes).

        The monitor dies with the thread that started it (die_with_parent_preexec),
        so a SIGKILLed tool does not leave it running."""
        try:
            return subprocess.Popen(
                list(MONITOR_ARGV),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                bufsize=0, preexec_fn=die_with_parent_preexec(),
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as e:
            raise PwError(_("Can't run pw-dump -m: {error}").format(error=e)) from e
