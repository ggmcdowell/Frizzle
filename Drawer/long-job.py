"""long-job.py -- a long run (a gate, an export, a smoke) as its own detached named job with one result file.
A dead worker's replacement reads the result and does not start the run over.

  <python> Drawer/long-job.py start --name <name> [--cwd <dir>] -- <argv...>
      Start argv as a detached job (its own session, setsid) and exit. The job outlives the worker's session and
      process tree. The default cwd is the main clone of the current checkout (harness_config.main_checkout), so a
      removed task worktree does not take the job's folder. The name is the identity: a name that already holds a
      record (running, done or lost) starts nothing; start prints that job's status line and says so on stderr.
      A deliberate new run takes a new name.
  <python> Drawer/long-job.py status --name <name>
      Print one JSON line: state (absent, starting, running, done, lost), exit_code, started, finished, runner_pid,
      log. Starting means the record exists and its runner has not yet written its pid; running always holds a pid.
  <python> Drawer/long-job.py wait --name <name> [--timeout <seconds>]
      Block until the job is done, lost or absent, or the timeout, print the status line, and exit with the job's exit code.
      The timeout is capped at WAIT_S (270 s, under the 5-minute prompt cache life and a Code Mode call), so
      wait always returns in time; call it again for a longer job. Timeout exits 124, lost 125, absent 2.

State lives under harness.toml [project] state_root/jobs/: <name>.json, the result record (start time, argv,
cwd, the runner pid, then at the end finish time and exit code; every write is atomic), and <name>.log, the
job's stdout and stderr. Lost means the runner died before it wrote a finish: its pid is dead
(gatelib.pid_alive), or it never wrote its pid within RUNNER_GRACE_S of the start. pid_alive has no pid-reuse
guard: a dead runner whose pid another process took reads running, so wait times out and nothing reruns (fail
closed). A name is letters, digits, dot, dash and underscore. No queue, no retry, no cleanup: trash a record by
hand to free its name.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gatelib  # noqa: E402
import harness_config  # noqa: E402

STATE = harness_config.path(harness_config.load(harness_config.hub_root(Path(__file__).resolve().parent))["project"]["state_root"]) / "jobs"
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
WAIT_S = 270
RUNNER_GRACE_S = 60  # a record with no runner pid this long after its start is lost
EXIT_ABSENT, EXIT_TIMEOUT, EXIT_LOST = 2, 124, 125
RUN_MODE = "_run"  # the hidden runner mode


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def paths(name):
    return STATE / f"{name}.json", STATE / f"{name}.log"


def read(record):
    """The record as a dict, or None when absent."""
    try:
        with open(record, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def write(record, data):
    """Replace the record atomically."""
    tmp = record.with_name(f"{record.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, record)


def claim(record, data):
    """Create the record only if the name is free: link a complete temp file into place. True if claimed."""
    tmp = record.with_name(f"{record.name}.{os.getpid()}.claim")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    try:
        os.link(tmp, record)
        return True
    except FileExistsError:
        return False
    finally:
        os.remove(tmp)


def status(name):
    record, log = paths(name)
    rec = read(record)
    if rec is None:
        return {"name": name, "state": "absent", "log": str(log)}
    pid = rec.get("runner_pid")
    if "exit_code" in rec:
        state = "done"
    elif pid is None:
        started = datetime.datetime.fromisoformat(rec["started"])
        late = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() > RUNNER_GRACE_S
        state = "lost" if late else "starting"
    else:
        state = "running" if gatelib.pid_alive(pid) else "lost"
    return {"name": name, "state": state, "exit_code": rec.get("exit_code"), "started": rec.get("started"),
            "finished": rec.get("finished"), "runner_pid": pid, "log": str(log), "cwd": rec.get("cwd"),
            "argv": rec.get("argv")}


def start(name, cwd, argv):
    record, log = paths(name)
    STATE.mkdir(parents=True, exist_ok=True)
    if not claim(record, {"name": name, "started": now(), "argv": argv, "cwd": cwd, "log": str(log)}):
        line = status(name)
        print(json.dumps(line))
        print(f"long-job: {name} already holds a {line['state']} job; nothing started. "
              "A deliberate new run takes a new name.", file=sys.stderr)
        return 0
    runner = [sys.executable, "-B", str(Path(__file__).resolve()), RUN_MODE, name]
    try:
        proc = subprocess.Popen(runner, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=STATE, start_new_session=True)
    except OSError as exc:
        os.remove(record)
        print(f"long-job: cannot start the runner for {name} ({exc}); the name is free again", file=sys.stderr)
        return 1
    for _ in range(100):  # the runner writes its own pid first; only it writes the record from here on
        rec = read(record)
        if rec.get("runner_pid") is not None:
            break
        if proc.poll() is not None:  # the runner exited: re-read, since it may have run and finished meanwhile
            rec = read(record)
            if rec.get("runner_pid") is None:  # it died before its first write: record its pid so status says lost
                rec["runner_pid"] = proc.pid
                write(record, rec)
            break
        time.sleep(0.1)
    print(json.dumps(status(name)))
    return 0


def run(name):
    """The runner: run the record's argv, its output to the log, then write the finish."""
    record, log = paths(name)
    rec = read(record)
    rec["runner_pid"] = os.getpid()
    write(record, rec)
    with open(log, "ab") as out:
        try:
            child = subprocess.Popen(rec["argv"], cwd=rec["cwd"], stdin=subprocess.DEVNULL, stdout=out,
                                     stderr=subprocess.STDOUT)
            code = child.wait()
        except OSError as exc:
            out.write(f"long-job: cannot start {rec['argv']!r}: {exc}\n".encode("utf-8"))
            code = 127
    rec["finished"] = now()
    rec["exit_code"] = code
    write(record, rec)
    return 0


def wait(name, timeout):
    deadline = time.monotonic() + min(timeout, WAIT_S)
    while True:
        line = status(name)
        if line["state"] not in ("starting", "running") or time.monotonic() >= deadline:
            break
        time.sleep(min(2, max(0.1, deadline - time.monotonic())))
    print(json.dumps(line))
    if line["state"] == "done":
        code = line["exit_code"]
        return 128 - code if code < 0 else code  # a POSIX signal death exits 128 + signal
    return {"absent": EXIT_ABSENT, "lost": EXIT_LOST}.get(line["state"], EXIT_TIMEOUT)


def job_name(text):
    if not NAME.fullmatch(text):
        raise argparse.ArgumentTypeError("a name is 1 to 100 letters, digits, dots, dashes and underscores")
    return text


def main(argv):
    if len(argv) == 2 and argv[0] == RUN_MODE and NAME.fullmatch(argv[1]):
        return run(argv[1])
    job = []
    if "--" in argv:
        cut = argv.index("--")
        argv, job = argv[:cut], argv[cut + 1:]
    parser = argparse.ArgumentParser(prog="long-job.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "status", "wait"):
        p = sub.add_parser(command)
        p.add_argument("--name", required=True, type=job_name)
        if command == "start":
            p.add_argument("--cwd", default=str(harness_config.main_checkout(os.getcwd())))
        if command == "wait":
            p.add_argument("--timeout", type=float, default=WAIT_S)
    args = parser.parse_args(argv)
    if args.command == "start":
        cwd = os.path.abspath(args.cwd)
        if not job:
            parser.error("start takes the job's argv after --")
        if not os.path.isdir(cwd):
            parser.error(f"--cwd is not a folder: {cwd}")
        return start(args.name, cwd, job)
    if job:
        parser.error(f"{args.command} takes no argv after --")
    if args.command == "status":
        print(json.dumps(status(args.name)))
        return 0
    if args.timeout < 0:
        parser.error("--timeout is seconds, 0 or more")
    return wait(args.name, args.timeout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
