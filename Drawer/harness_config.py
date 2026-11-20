"""Hub config: reads harness.toml (and the local, git-ignored harness.local.toml) and finds the hub root.

The hub root is the first folder, from the cwd up and then from this file up, that holds
harness.toml and the marker file that harness.toml names. <env_prefix>_HUB is not read: the
prefix lives in the file this module is looking for.

  <python> Drawer/harness_config.py --print        every value, flat, plus the derived ones
  <python> Drawer/harness_config.py --get mem.db one value

harness.local.toml is git-ignored, so a linked task worktree has none: load reads the main checkout's copy
there, so a state tool in the worktree uses this box's roots, not the defaults. A worktree's own copy wins.

HARNESS_NO_LOCAL_TOML=1 skips harness.local.toml: the test runners (Drawer/smoke.py, the hub-root conftest.py)
set it, so a local binding ([product] path, the roots) never reaches a hub test.
"""
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oscompat  # noqa: E402

CONFIG = "harness.toml"
LOCAL = "harness.local.toml"
NO_LOCAL_VAR = "HARNESS_NO_LOCAL_TOML"
REAL_HOME_VAR = "HARNESS_TEST_REAL_HOME"  # the test runners save the user home here before they move HOME


def _is_root(folder):
    toml = folder / CONFIG
    if not toml.is_file():
        return False
    marker = tomllib.loads(toml.read_text(encoding="utf-8")).get("project", {}).get("marker_file")
    return bool(marker) and (folder / marker).is_file()


def hub_root(start=None):
    starts = [Path(start or os.getcwd()).resolve(), Path(__file__).resolve().parent]
    for base in starts:
        for folder in (base, *base.parents):
            if _is_root(folder):
                return folder
    raise SystemExit(f"no hub root: no {CONFIG} with its marker file above {starts[0]}")


def _merge(base, over):
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def server_name(cfg):
    """The hub memory's MCP server name ([mem] server_name)."""
    return (cfg.get("mem") or {}).get("server_name") or "mem"


def local_toml(root):
    """root's harness.local.toml; in a linked worktree (its .git is a file) with none, the main checkout's."""
    local = root / LOCAL
    if not local.is_file() and (root / ".git").is_file():
        main = main_checkout(root) / LOCAL
        if main.is_file():
            return main
    return local


def load(root=None):
    root = Path(root) if root else hub_root()
    cfg = tomllib.loads((root / CONFIG).read_text(encoding="utf-8"))
    use_local = os.environ.get(NO_LOCAL_VAR) != "1"
    local = local_toml(root) if use_local else root / LOCAL
    use_local = use_local and local.is_file()
    if use_local:
        cfg = _merge(cfg, tomllib.loads(local.read_text(encoding="utf-8-sig")))  # a hand-made file may carry a BOM
    py = oscompat.venv_python(root)
    cfg["derived"] = {
        "root": str(root),
        "local_override": str(local) if use_local else "",
        "python": str(py),
        "tool_prefix": f"mcp__{server_name(cfg)}__",
    }
    return cfg


# git rev-parse --local-env-vars: Git exports some of these to the hooks it runs (GIT_DIR on a push
# from a linked worktree), and they outrank -C. A git call that names its repo drops them.
GIT_LOCAL_ENV = frozenset((
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR"))


def git_env():
    """os.environ without Git's repo-local variables, for a git -C <repo> call made inside a hook."""
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV}


def main_checkout(path):
    """The checkout a git worktree was added from, or path itself. Checkout-wide state lives there:
    the commit gate's pass file and logs."""
    try:
        common = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True, env=git_env()).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return Path(path)
    return Path(common).resolve().parent


def gate_argv(command):
    """A [product] gate_command as an argv: split like a shell line (write paths in it with forward slashes), {python}
    as the hub interpreter. On Windows a leading sh or bash is Git for Windows' own (oscompat.git_shell), as a script
    path or the WSL launcher in System32 would not run the gate. ValueError: it does not parse; OSError: no Git sh."""
    argv = [sys.executable if word == "{python}" else word for word in shlex.split(command)]
    if oscompat.WINDOWS and argv and argv[0] in ("sh", "bash"):
        argv[0] = oscompat.git_shell(argv[0])
    return argv


def env_name(suffix, cfg=None):
    """A session env variable of this hub: <env_prefix>_<suffix>, for example <env_prefix>_GIT_FRESHNESS_ACTIVE."""
    cfg = cfg or load()
    return f"{cfg['project']['env_prefix']}_{suffix}"


def product_path(cfg=None):
    """(source, value): this session's product folder. The <env_prefix>_PRODUCT_PATH override of a
    task worktree outranks [product] path; value is "" when neither is set."""
    cfg = cfg or load()
    var = env_name("PRODUCT_PATH", cfg)
    if os.environ.get(var, "").strip():
        return var, os.environ[var].strip()
    return "[product] path", str((cfg.get("product") or {}).get("path") or "").strip()


def path(value):
    """A config path with ~ expanded."""
    return Path(os.path.expanduser(value))


def real_home():
    """The user home before a test runner moved HOME (REAL_HOME_VAR), else the account's home, else HOME."""
    saved = os.environ.get(REAL_HOME_VAR)
    if saved:
        return Path(saved)
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):
        return Path.home()


def real_roots(cfg):
    """The real [project] config_root and state_root and [mem] db: their ~ is the real home, not HOME."""
    values = [cfg["project"]["config_root"], cfg["project"]["state_root"], cfg.get("mem", {}).get("db") or
              cfg["project"]["state_root"].rstrip("/") + "/hub.db"]
    home = str(real_home())
    return [Path(home + v[1:]) if v.startswith("~") else Path(v) for v in values]


def test_home(env, home):
    """env with a test run's temp HOME: the ~-rooted roots resolve under home, and the real home is saved.
    The folder of the interpreter that runs the tests leads PATH: a fixture repo has no .venv, so its git hooks take
    python3 from PATH, and a Mac with a uv-managed .venv only has the stock 3.9 there (incident e14ea0c2)."""
    env = dict(env)
    env.setdefault(REAL_HOME_VAR, str(real_home()))
    env.update(HOME=str(home), USERPROFILE=str(home))
    own = os.path.dirname(sys.executable)
    rest = [part for part in env.get("PATH", "").split(os.pathsep) if part and part != own]
    env["PATH"] = os.pathsep.join([own, *rest])
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "CLAUDE_CONFIG_DIR"):
        env.pop(name, None)
    return env


def isolate_test_env():
    """Isolate this process from the box's local roots, as a hub test needs (the one home of the isolation: the
    hub-root conftest.py calls it, and a direct unittest or test_*.py run gets it at import, below).
    NO_LOCAL_VAR=1 skips harness.local.toml; the Flight session variables go (a card-run leg inherits them); and a
    temp HOME (test_home) takes the ~-rooted roots, unless a runner already set REAL_HOME_VAR (a nested run keeps its
    runner's HOME). test_home also leads PATH with the test interpreter, so a direct run finds python3 as pytest does."""
    import atexit
    import shutil
    import tempfile
    os.environ[NO_LOCAL_VAR] = "1"
    cfg = load(hub_root(Path(__file__).resolve().parent))
    for name in ("FLIGHT", "FLIGHT_ONESHOT"):
        os.environ.pop(env_name(name, cfg), None)
    if REAL_HOME_VAR not in os.environ:
        home = tempfile.mkdtemp(prefix="hub-test-home-")
        atexit.register(shutil.rmtree, home, True)
        os.environ.update(test_home(os.environ, home))


def _direct_test_main():
    """True in `python -m unittest` and `python test_x.py`: a direct test run no runner isolated. No ps call: every
    hook imports this module."""
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if getattr(spec, "name", "") == "unittest.__main__":
        return True
    name = os.path.basename(sys.argv[0]) if sys.argv else ""
    return name.startswith("test_") and name.endswith(".py")


if os.environ.get(NO_LOCAL_VAR) != "1" and _direct_test_main():
    isolate_test_env()


def get(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def flat(node, prefix=""):
    for key, value in node.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from flat(value, name + ".")
        else:
            yield name, value


def main(argv):
    cfg = load()
    if argv[:1] == ["--print"]:
        for name, value in flat(cfg):
            print(f"{name} = {json.dumps(value)}")
        return 0
    if argv[:1] == ["--get"] and len(argv) == 2:
        value = get(cfg, argv[1])
        # newline="\n": a $(...) capture in Git Bash keeps a CR that Windows text mode adds.
        sys.stdout.reconfigure(newline="\n")
        print(value if isinstance(value, str) else json.dumps(value))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
