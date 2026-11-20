"""The saved runtime profile: environment loading, setup detection, machine facts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

DRAWER = Path(__file__).resolve().parents[1]
if str(DRAWER) not in sys.path:
    sys.path.insert(0, str(DRAWER))
from runtime import profile  # noqa: E402

E = profile.env_name


class ProfileFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime profile ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.hub = self.home / "a hub" / "checkout"
        self.hub.mkdir(parents=True)
        self.profile = dict(schema_version=1, hub_root=str(self.hub), member_name="member",
            agent="claude", box_name="test-box")
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.path = profile.default_profile_path(self.hub)
        profile.save_profile(self.path, self.profile)

    def seat_env(self):
        """Every variable this fixture's saved profile hands a child, written out."""
        return {
            E("HUB"): str(self.hub), E("BOX"): "test-box", E("ROLE"): "operator",
        }


class RuntimeEnvTests(ProfileFixture):
    def test_profile_lives_under_the_configured_config_root(self):
        self.assertEqual(self.home, Path.home())
        root = str(profile._config()["project"]["config_root"]).replace("~/", "", 1)
        self.assertEqual(self.home / root / "seats", self.path.parent.parent)
        self.assertEqual("config.local.json", self.path.name)

    def test_empty_parent_environment_loads_complete_seat(self):
        self.assertEqual(self.seat_env(), profile.runtime_env(self.hub, {}))

    def test_cut_fields_and_foreign_agents_are_refused(self):
        # Fields the base cut (a workspace root, the viewer page, a second owner, an installer stamp).
        for field, value in (("engine_root", str(self.home / "engine")), ("viewer_url", "https://v.test/graph/"),
                             ("owner_name", "member"), ("installer_profile_version", 1)):
            with self.subTest(field=field), self.assertRaisesRegex(
                    profile.ProfileError, f"^unknown profile fields: {field}$"):
                profile.validate_profile(dict(self.profile, **{field: value}))
        with self.assertRaisesRegex(profile.ProfileError, r"^agent must be 'claude'$"):
            profile.validate_profile(dict(self.profile, agent="other"))
        for role in ("library", "member"):
            with self.subTest(role=role), self.assertRaisesRegex(profile.ProfileError, r"^role must be operator$"):
                profile.validate_profile(dict(self.profile, role=role))
        self.assertEqual(dict(self.profile, role="operator"),
                         profile.validate_profile(dict(self.profile, role="operator")))

    def test_a_saved_profile_without_a_role_loads_as_operator(self):
        self.path.write_text(json.dumps(self.profile), encoding="utf-8")
        self.assertEqual(dict(self.profile, role="operator"), profile.load_runtime_profile(self.hub))
        self.assertEqual(self.seat_env(), profile.runtime_env(self.hub, {}))

    def test_saved_profile_replaces_stale_scope_preserves_other_environment(self):
        parent = {E("HUB"): "wrong", E("BOX"): "wrong", "UNRELATED": "keep"}
        untouched = dict(parent)
        result = profile.runtime_env(self.hub, parent)
        self.assertEqual(dict(self.seat_env(), UNRELATED="keep"), result)
        self.assertEqual(untouched, parent)

    def test_broken_saved_profile_never_uses_inherited_identity(self):
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(profile.ProfileError,
                                    r"^profile is unavailable or invalid \(JSONDecodeError\)$"):
            profile.runtime_env(self.hub, {E("BOX"): "inherited"})

    def test_wrong_checkout_is_rejected(self):
        profile.save_profile(self.path, dict(self.profile, hub_root=str(self.home)))
        with self.assertRaisesRegex(profile.ProfileError,
                                    r"^Saved runtime profile belongs to another checkout$"):
            profile.load_runtime_profile(self.hub)

    def test_no_profile_preserves_legacy_explicit_environment(self):
        self.path.unlink()
        self.assertEqual({"LEGACY": "yes"}, profile.runtime_env(self.hub, {"LEGACY": "yes"}))

    def test_independent_python_process_loads_profile_without_a_parent(self):
        prefix = E("")
        env = {k: v for k, v in os.environ.items() if not k.startswith(prefix)}
        env["HOME"] = str(self.home)
        code = ("import json,sys;sys.path.insert(0,sys.argv[1]);from runtime import profile as p;"
                "e=p.runtime_env(sys.argv[2],{});"
                "print(json.dumps({k:e.get(p.env_name(k)) for k in ('HUB','BOX')}))")
        completed = subprocess.run([sys.executable, "-B", "-c", code, str(DRAWER), str(self.hub)],
                                   env=env, cwd=self.home, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"HUB": str(self.hub), "BOX": "test-box"},
                         json.loads(completed.stdout))


class SetupRequiredAndMachineTests(ProfileFixture):
    def test_setup_required_only_without_profile(self):
        self.assertIs(False, profile.setup_required(self.hub))
        self.path.unlink()
        self.assertIs(True, profile.setup_required(self.hub))

    def test_a_present_but_broken_profile_is_repair_not_setup(self):
        self.path.write_text("not json", encoding="utf-8")
        self.assertIs(False, profile.setup_required(self.hub))
        with self.assertRaisesRegex(profile.ProfileError,
                                    r"^profile is unavailable or invalid \(JSONDecodeError\)$"):
            profile.machine(self.hub, {})

    def test_machine_from_a_saved_profile(self):
        self.assertEqual(profile.Machine("posix", "claude"), profile.machine(self.hub, {}))

    def test_a_seat_without_a_profile_names_no_provider(self):
        self.path.unlink()
        self.assertEqual(profile.Machine("posix", None), profile.machine(self.hub, {}))


if __name__ == "__main__":
    unittest.main()
