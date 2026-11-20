#!/usr/bin/env python
"""Stamp a new product hub from this template hub.

    <python> Drawer/new-hub.py --dest <folder> --name <slug> --display "<Display Name>" [--origin <url>]
        [--state-root <folder>] [--config-root <folder>] [--copy-secrets]

It clones the template's committed head (history kept) into --dest, renames the clone's origin to
'template' (the template's own origin URL when it has one), and sets --origin as origin when given. It
rewrites harness.toml [project] (name, display_name, env_prefix, marker_file, config_root, state_root),
[mem] (db, env_file, export_dir) and [names] denylist, the pyproject and uv.lock name
('<slug>-hub'), renames the marker file, re-renders the generated settings, and commits the stamp.
--state-root and --config-root set the two roots (default ~/.local/share/<slug>-hub and ~/.config/<slug>-hub);
[mem] db and export_dir follow the state root, [mem] env_file and [names] denylist the config root.
By default it writes only under --dest. With --copy-secrets it also copies the template's name denylist and mem
env_file to the new paths at mode 600; it never prints them.

It refuses, with no write: a slug that is not [a-z][a-z0-9]{1,15} or is the template slug, a dest inside
the template, a dest that exists, and a new state_root or config_root that exists. Run it from the
template hub root (the setup skill, step 0). The stamp is a local commit: nothing is pushed.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_config  # noqa: E402

SLUG_RE = re.compile(r"[a-z][a-z0-9]{1,15}")
SLUG_KEYS = {"project": ("marker_file", "config_root", "state_root"),
             "mem": ("db", "env_file", "export_dir"),
             "names": ("denylist",)}
SECRET_COPIES = (("names", "denylist"), ("mem", "env_file"))


class Refused(Exception):
    pass


def git(cwd, *args, check=True):
    run = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                         env=harness_config.git_env())
    if check and run.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {run.stderr.strip() or run.stdout.strip()}")
    return run.stdout.strip()


def set_key(text, section, key, value):
    """Set key = "value" in [section] of a TOML text, keeping the line's comment; exactly one match."""
    out, current, hits = [], None, 0
    for line in text.splitlines(keepends=True):
        head = re.match(r"\s*\[([^\]]+)\]\s*(#.*)?$", line)
        if head:
            current = head.group(1).strip()
        elif current == section:
            m = re.match(r'(\s*' + re.escape(key) + r'\s*=\s*)"[^"]*"(.*)$', line, re.S)
            if m:
                line, hits = f'{m.group(1)}"{value}"{m.group(2)}', hits + 1
        out.append(line)
    if hits != 1:
        raise RuntimeError(f"harness.toml [{section}] {key}: {hits} matches, want 1")
    return "".join(out)


ROOT_OF = {("mem", "db"): "state_root", ("mem", "export_dir"): "state_root",
           ("mem", "env_file"): "config_root", ("names", "denylist"): "config_root"}  # the root a value sits under


def plan(template, dest, slug, display, state_root=None, config_root=None):
    """The new values, checked; raises Refused before any write."""
    cfg = harness_config.load(template)  # this box's paths, harness.local.toml included: the copy sources
    base = _tracked_cfg(template)
    old = base["project"]["name"]
    if not SLUG_RE.fullmatch(slug):
        raise Refused(f"slug {slug!r}: want [a-z][a-z0-9]{{1,15}}")
    if slug == old:
        raise Refused(f"slug {slug!r} is the template slug")
    dest = Path(os.path.abspath(os.path.expanduser(dest)))
    real = Path(os.path.realpath(dest))
    troot = Path(os.path.realpath(template))
    if real == troot or troot in real.parents:
        raise Refused(f"dest {dest} is inside the template {troot}")
    if dest.exists() or dest.is_symlink():
        raise Refused(f"dest {dest} exists")
    values = {"project": {"name": slug, "display_name": display, "env_prefix": slug.upper()},
              "mem": {}, "names": {}}
    given = {"state_root": state_root, "config_root": config_root}
    for section, keys in SLUG_KEYS.items():
        for key in keys:
            value = base[section][key]
            root = ROOT_OF.get((section, key))
            if section == "project" and given.get(key):
                values[section][key] = given[key].rstrip("/") or "/"
            elif root and given[root]:  # the template value's tail after its own root, under the given root
                head = base["project"][root]
                if not value.startswith(head + "/"):
                    raise RuntimeError(f"template [{section}] {key} = {value!r} is not under its {root} {head!r}")
                values[section][key] = values["project"][root] + value[len(head):]
            elif old not in value:
                raise RuntimeError(f"template [{section}] {key} = {value!r} does not name the slug {old!r}")
            else:
                values[section][key] = value.replace(old, slug)
    for key in ("state_root", "config_root"):
        if harness_config.path(values["project"][key]).exists():
            raise Refused(f"{key} {values['project'][key]} exists: another hub uses it")
    return cfg, base, old, dest, values


def _tracked_cfg(template):
    """harness.toml at the template head (no harness.local.toml: the stamp clones the head)."""
    import tomllib
    return tomllib.loads(git(template, "show", "HEAD:harness.toml"))


def copy_secret(src, dst):
    """Copy a private file at mode 600 when the template box has it; never read it into output."""
    src, dst = harness_config.path(src), harness_config.path(dst)
    if not src.is_file():
        return f"skip {dst.name}: the template has none"
    if dst.exists():
        return f"keep {dst}: exists"
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out)
    os.chmod(dst, 0o600)
    return f"copied {dst} mode 600"


def stamp(template, dest, slug, display, origin=None, python=sys.executable, state_root=None, config_root=None,
          copy_secrets=False):
    cfg, base, old, dest, values = plan(template, dest, slug, display, state_root, config_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(dest.parent, "clone", "-q", "--no-hardlinks", str(template), str(dest))
    template_url = git(template, "remote", "get-url", "origin", check=False) or str(template)
    git(dest, "remote", "rename", "origin", "template")
    git(dest, "remote", "set-url", "template", template_url)
    if origin:
        git(dest, "remote", "add", "origin", origin)

    toml = dest / "harness.toml"
    text = toml.read_text(encoding="utf-8")
    for section, keys in values.items():
        for key, value in keys.items():
            text = set_key(text, section, key, value)
    old_prefix = base["project"]["env_prefix"]
    text = text.replace(f"{old_prefix}_", f"{values['project']['env_prefix']}_")
    toml.write_text(text, encoding="utf-8")

    hub_name = f"{slug}-hub"
    for name in ("pyproject.toml", "uv.lock"):
        f = dest / name
        body = f.read_text(encoding="utf-8")
        new, n = re.subn(r'^name = "' + re.escape(f"{old}-hub") + '"$', f'name = "{hub_name}"', body,
                         count=1, flags=re.M)
        if n != 1:
            raise RuntimeError(f"{name}: no name = \"{old}-hub\" line")
        f.write_text(new, encoding="utf-8")

    git(dest, "mv", base["project"]["marker_file"], values["project"]["marker_file"])
    sync = subprocess.run([python, "-B", str(dest / "Drawer" / "settings-sync.py"), "--apply"], cwd=dest,
                          capture_output=True, text=True, env=harness_config.git_env())
    if sync.returncode:
        raise RuntimeError(f"settings-sync --apply: {sync.stdout}{sync.stderr}")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m",
        f"Stamp {display} ({slug}) from template {git(template, 'rev-parse', '--short', 'HEAD')} (new-hub.py).")
    if copy_secrets:
        return dest, [copy_secret(cfg[s][k], values[s][k]) for s, k in SECRET_COPIES]
    return dest, [f"skip {harness_config.path(values[s][k])}: pass --copy-secrets to copy it at mode 600"
                  for s, k in SECRET_COPIES]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--name", required=True, help="the new slug")
    ap.add_argument("--display", required=True, help="the display name")
    ap.add_argument("--origin", help="the new hub's origin URL (not pushed)")
    ap.add_argument("--state-root", help="the new hub's state root (default ~/.local/share/<slug>-hub)")
    ap.add_argument("--config-root", help="the new hub's config root (default ~/.config/<slug>-hub)")
    ap.add_argument("--copy-secrets", action="store_true",
                    help="copy the template's name denylist and mem env_file to the new paths at mode 600")
    a = ap.parse_args(argv)
    template = harness_config.hub_root(HERE)
    try:
        dest, notes = stamp(template, a.dest, a.name, a.display, a.origin, state_root=a.state_root,
                           config_root=a.config_root, copy_secrets=a.copy_secrets)
    except Refused as e:
        print(f"REFUSED {e}")
        return 2
    except RuntimeError as e:
        print(f"ERROR {e}")
        return 1
    print(f"STAMPED {dest} {git(dest, 'rev-parse', 'HEAD')}")
    for note in notes:
        print(note)
    print(f"next: cd {dest} && uv sync, then run the setup skill there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
