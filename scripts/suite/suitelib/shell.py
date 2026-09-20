"""Subprocess helpers. Every call has a timeout."""
import subprocess

DEFAULT_TIMEOUT = 120


def run(cmd, timeout=DEFAULT_TIMEOUT, input=None, env=None, cwd=None):
    """Run a command (list or shell string); never raises on a non-zero exit or a timeout.
    Returns a CompletedProcess with text stdout/stderr (returncode 124 on timeout)."""
    shell = isinstance(cmd, str)
    try:
        return subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout,
                              input=input, env=env, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, out, err + f"\n[timeout after {timeout}s]")


def out(cmd, timeout=DEFAULT_TIMEOUT, default="", **kw):
    """stdout of a command, stripped; default when it fails."""
    r = run(cmd, timeout=timeout, **kw)
    return r.stdout.strip() if r.returncode == 0 else default


def ok(cmd, timeout=DEFAULT_TIMEOUT, **kw):
    return run(cmd, timeout=timeout, **kw).returncode == 0


def detached(cmd):
    """Start a shell command fully detached (own session, stdio closed) and return the Popen."""
    return subprocess.Popen(["sh", "-c", cmd], start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
