"""The checkout-local runtime profile: validation, storage, scoped environment.

One saved profile supplies MCP subprocesses, hooks and
diagnostics; no shell profile or persistent environment is changed. A checkout without a
saved profile keeps its explicit environment. Standard library only (plus the hub's
Drawer/harness_config.py): hooks run under any Python 3.11+ interpreter before the hub
virtual environment exists.

Names come from harness.toml: the session variables are <env_prefix>_<suffix>
(harness_config.env_name), and the profile lives under [project] config_root.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Mapping
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

_DRAWER = Path(__file__).resolve().parents[1]
if str(_DRAWER) not in sys.path:
    sys.path.insert(0, str(_DRAWER))
import harness_config  # noqa: E402
import oscompat  # noqa: E402

SCHEMA_VERSION = 1
AGENTS = frozenset({"claude"})
ROLES = frozenset({"operator"})
CORE_FIELDS = (
    "schema_version",
    "hub_root",
    "member_name",
    "agent",
    "box_name",
)
ALLOWED_FIELDS = frozenset((*CORE_FIELDS, "role"))
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
# Session variable suffixes this profile owns: scoped_env clears and sets each <env_prefix>_<suffix>.
SESSION_SUFFIXES = ("HUB", "BOX", "ROLE")

Machine = namedtuple("Machine", "os provider")


class ProfileError(ValueError):
    """A profile is unsafe or internally inconsistent."""


@functools.lru_cache(maxsize=1)
def _config() -> dict:
    """This hub's harness.toml: the hub that holds this file, never the caller's cwd."""
    return harness_config.load(_DRAWER.parent)


def env_name(suffix: str) -> str:
    """This hub's session variable for suffix (HUB -> <env_prefix>_HUB)."""
    return harness_config.env_name(suffix, _config())


def _config_root() -> Path:
    # Path.home(), not expanduser: a test that moves home moves the profile with it.
    raw = str(_config()["project"]["config_root"])
    if raw == "~" or raw.startswith("~/"):
        return Path.home() / raw[2:]
    return Path(os.path.expanduser(raw))


def _absolute_path(value: str) -> bool:
    return Path(value).is_absolute()


def _profile_copy(profile: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(profile, Mapping):
        raise ProfileError("profile must be a mapping")
    return dict(profile)


def validate_profile(profile: Mapping[str, object], *, complete: bool = True) -> dict[str, object]:
    """Validate and normalize a non-secret profile.

    Partial validation permits missing answers, while validating every supplied answer.
    Complete validation requires only the machine-independent core.
    """
    result = _profile_copy(profile)
    unknown = sorted(set(result) - ALLOWED_FIELDS)
    if unknown:
        raise ProfileError(f"unknown profile fields: {', '.join(unknown)}")

    result.setdefault("schema_version", SCHEMA_VERSION)
    result.setdefault("role", "operator")

    if complete:
        missing = [name for name in CORE_FIELDS if result.get(name) in (None, "")]
        if missing:
            raise ProfileError(f"missing required profile fields: {', '.join(missing)}")
    if result.get("schema_version") != SCHEMA_VERSION:
        raise ProfileError(f"schema_version must be {SCHEMA_VERSION}")

    for field in (*CORE_FIELDS, "role"):
        if field in result and field != "schema_version":
            value = result[field]
            if not isinstance(value, str):
                raise ProfileError(f"{field} must be a string")
            result[field] = value.strip()
            if any(ord(character) < 32 or ord(character) == 127 for character in result[field]):
                raise ProfileError(f"{field} contains a control character")

    if result.get("agent") and result["agent"] not in AGENTS:
        raise ProfileError("agent must be 'claude'")
    if result.get("role") not in ROLES:
        raise ProfileError("role must be operator")
    for field in ("member_name", "box_name"):
        value = result.get(field)
        if value and not IDENTIFIER.fullmatch(str(value)):
            raise ProfileError(f"{field} contains unsupported characters")
    if result.get("hub_root") and not _absolute_path(str(result["hub_root"])):
        raise ProfileError("hub_root must be an absolute path")
    return result


def default_profile_path(hub_root: str | os.PathLike[str]) -> Path:
    """Return the per-checkout non-secret profile path."""
    normalized = os.path.normcase(os.path.abspath(os.fspath(hub_root))).encode("utf-8")
    checkout_id = hashlib.sha256(normalized).hexdigest()[:16]
    return _config_root() / "seats" / checkout_id / "config.local.json"


def load_profile(path: str | os.PathLike[str]) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"profile is unavailable or invalid ({type(exc).__name__})") from exc
    return validate_profile(value, complete=False)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        oscompat.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def save_profile(
    path: str | os.PathLike[str], profile: Mapping[str, object], *, complete: bool = True,
    validate=validate_profile,
) -> Path:
    normalized = validate(profile, complete=complete)
    target = Path(path)
    payload = (json.dumps(normalized, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(target, payload, 0o600)
    return target


def scoped_env(profile: Mapping[str, object], base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child-process environment; os.environ is never modified."""
    value = validate_profile(profile)
    env = dict(os.environ if base_env is None else base_env)
    # Login probes run in the invoking terminal, while the agent starts in the
    # hub. Preserve the meaning of an existing relative client-config path.
    if env.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = str(Path(env["CLAUDE_CONFIG_DIR"]).expanduser().resolve())
    for suffix in SESSION_SUFFIXES:
        env.pop(env_name(suffix), None)
    env.update({env_name(suffix): text for suffix, text in (
        ("HUB", str(Path(str(value["hub_root"])))),
        ("BOX", str(value["box_name"])),
        ("ROLE", str(value["role"])),
    )})
    return env


def load_runtime_profile(hub: str | Path) -> dict:
    root = Path(hub).resolve()
    value = validate_profile(load_profile(default_profile_path(root)))
    if Path(str(value["hub_root"])).resolve() != root:
        raise ProfileError("Saved runtime profile belongs to another checkout")
    return value


def runtime_env(hub: str | Path, base_env=None) -> dict[str, str]:
    root = Path(hub).resolve()
    inherited = dict(os.environ if base_env is None else base_env)
    if not default_profile_path(root).exists():
        return inherited
    return scoped_env(load_runtime_profile(root), inherited)


def activate(hub: str | Path) -> None:
    """Load into this hook/diagnostic process only, before reading configuration."""
    env = runtime_env(hub)
    os.environ.clear()
    os.environ.update(env)


def setup_required(hub: str | Path) -> bool:
    """True when this checkout has no saved profile.

    Needs no activation: a boot hook asks this before it touches Git. A profile that exists
    but fails to load is repair, never setup.
    """
    return not default_profile_path(Path(hub).resolve()).exists()


def machine(hub: str | Path, env: Mapping[str, str] | None = None) -> Machine:
    """The small machine profile MCP applicability derives from: the host and, with a saved
    profile, its agent. A seat with no profile (setup required) names no provider.
    """
    root = Path(hub).resolve()
    host = "nt" if oscompat.WINDOWS else "posix"
    if default_profile_path(root).exists():
        return Machine(host, load_runtime_profile(root).get("agent") or None)
    return Machine(host, None)
