"""pushretry.py -- retry a git push that the remote side faulted.

  <python> Drawer/pushretry.py <repo> [<git push args>...]
      Run `git push <args>` in <repo>. A remote-side fault (remote unpack failed, an RPC failure, an HTTP 5xx, a
      connection reset or timed out, the remote end hung up) retries up to three times after a backoff (BACKOFF_S);
      any other refusal (a non-fast-forward, a hook refusal, a bad ref) returns at once. Prints the last push's
      output; exits with its code.

Module API: run(push, hold=None) calls push() (a CompletedProcess with stderr) and retries it the same way. mem
nightly pushes its export through run(). Each try runs the push hooks again. hold(), when given, runs after each
backoff, before the retry: a true value stops the retries and run returns None (product-push re-runs its steer check
there).
Why: a GitHub-side failure after a green gate is not the code, and a plain retry lands it.
"""
import re
import subprocess
import sys
import time

BACKOFF_S = (5, 15, 45)
# Git's own lines only, anchored at the line start: a hook's or a test's output never matches.
REMOTE_FAULT = re.compile(
    r"^(remote: error: .*unpack|error: remote unpack failed"
    r"|error: RPC failed"
    r"|error: .*returned error: 5\d\d"
    r"|fatal: .*(connection (reset|timed out)|timed out|the remote end hung up|returned error: 5\d\d)"
    r"| ! \[remote rejected\] .*(unpack|missing necessary objects))",
    re.IGNORECASE | re.MULTILINE)


def remote_fault(stderr):
    """True when git's stderr names a remote-side fault, never a non-fast-forward or a hook refusal."""
    return bool(REMOTE_FAULT.search(stderr or ""))


def run(push, sleep=time.sleep, hold=None):
    """Call push(); retry it after each remote-side fault. Returns the last result, or None when hold() stops it."""
    result = push()
    for delay in BACKOFF_S:
        if result.returncode == 0 or not remote_fault(result.stderr):
            break
        sleep(delay)
        if hold is not None and hold():
            return None
        result = push()
    return result


def main(argv=None, sleep=time.sleep):
    args = sys.argv[1:] if argv is None else argv
    if args in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    if not args:
        sys.stderr.write("usage: <python> Drawer/pushretry.py <repo> [<git push args>...]\n")
        return 2
    result = run(lambda: subprocess.run(["git", "push", *args[1:]], cwd=args[0], capture_output=True, text=True),
                 sleep)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
