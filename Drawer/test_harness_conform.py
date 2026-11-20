import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).with_name("harness-conform.py")
SPEC = importlib.util.spec_from_file_location("harness_conform", PATH)
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)

# The contract, written out here so a constant changed by mistake cannot move the test with it:
# the hub root this file sits under, the offline subset a commit may be held on, and the check
# names --list prints.
HUB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB / "Drawer"))
import testgit  # noqa: E402

testgit.arm()  # no git template in any fixture repo
OFFLINE = ["settings", "skills", "hooks", "pointers", "agents", "hiding", "blobs", "pins", "cites",
           "config"]
CHECK_NAMES = ["settings", "seat", "hooks", "pointers", "skills", "agents", "hiding", "blobs", "pins",
               "cites", "config", "mcp-live"]
# The prepared interpreter each printed remedy names on this host: a new seat has no py launcher.
PY = ".venv/bin/python -B"
GENERATORS = [["settings-sync.py", "--apply"]]


def recording_checks(ran):
    """A full check table: every check records that it ran and passes."""
    def one(name):
        def run():
            ran.append(name)
            return HARNESS.Result(name, "PASS", "")
        return run
    return {name: one(name) for name in CHECK_NAMES}


def write_hub(hub, files):
    """A fixture hub: each hub path holds its text."""
    for name, text in files.items():
        (hub / name).parent.mkdir(parents=True, exist_ok=True)
        (hub / name).write_text(text, encoding="utf-8")


def mem_fixture(path, rows):
    """A fixture mem database: each (id, superseded_by) row."""
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cards(id TEXT, superseded_by TEXT)")
    con.executemany("INSERT INTO cards VALUES (?, ?)", rows)
    con.commit()
    con.close()


class HarnessConformTests(unittest.TestCase):
    def test_a_standing_line_citing_a_superseded_card_or_a_removed_file_fails_the_cites_check(self):
        # Pre-pivot sweep: a doc, skill or State line that cites a superseded mem card, or (live
        # lines) a removed hub file, as current fails. A line that names the change, or names the
        # successor, is history and passes. Below the State section is not judged; a commit judges
        # only its staged documents.
        old, new, kept = "aaaa1111-0000", "bbbb2222-0000", "cccc3333-0000"
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            mem_fixture(str(hub / "hub.db"), [(old, new), (new, None), (kept, None)])
            write_hub(hub, {
                "AGENTS.md": "Build to aaaa1111.\nKeep cccc3333.\naaaa1111 is superseded.\nSee aaaa1111, now bbbb2222.\n",
                ".claude/skills/x/SKILL.md": "Clean line cccc3333.\n",
                "Flight/SCRATCHPAD.md": "# pad\n## State\nNext: aaaa1111\nRead `Drawer/gone.py`\n## Backlog\naaaa1111 old\n",
            })
            with patch.object(HARNESS, "HUB", hub), \
                    patch.object(HARNESS, "CITE_GLOBS", ["AGENTS.md", ".claude/skills/*/SKILL.md"]), \
                    patch.object(HARNESS, "_mem_db", lambda: hub / "hub.db"), \
                    patch.object(HARNESS.harness_config, "main_checkout", lambda path: hub):
                bare = HARNESS.check_cites()
                staged = HARNESS.check_cites(staged=[".claude/skills/x/SKILL.md"])
                staged_agents = HARNESS.check_cites(staged=["AGENTS.md"])
        self.assertEqual(bare.state, "FAIL")
        self.assertEqual(sorted(bare.findings), sorted([
            "AGENTS.md:1 cites aaaa1111 (superseded by bbbb2222)",
            "Flight/SCRATCHPAD.md:3 cites aaaa1111 (superseded by bbbb2222)",
            "Flight/SCRATCHPAD.md:4 cites removed file Drawer/gone.py",
        ]))
        self.assertEqual(staged.state, "PASS")
        self.assertEqual(staged_agents.findings, ["AGENTS.md:1 cites aaaa1111 (superseded by bbbb2222)"])

    def test_the_cites_check_passes_on_a_box_with_no_local_mem_database(self):
        with patch.object(HARNESS, "_mem_db", lambda: None):
            result = HARNESS.check_cites(staged=[])
        self.assertEqual(result.state, "PASS")
        self.assertIn("no local mem database", result.summary)

    def test_a_declared_box_local_output_directory_may_be_absent(self):
        # Gitignored per-seat files (the Flight pad, the hub pad) are absent from a fresh task
        # worktree and PASS; an undeclared missing sibling still WARNs by its line.
        declared = ["Flight/SCRATCHPAD.md", "SCRATCHPAD.md"]
        undeclared = ["Flight/NOTES.md", "Flight/SCRATCHPAD.txt"]
        seen = {}
        for pointer in declared + undeclared:
            with tempfile.TemporaryDirectory() as temporary:
                hub = Path(temporary)
                (hub / "CLAUDE.md").write_text(f"Path: `{pointer}`\n", encoding="utf-8")
                with patch.object(HARNESS, "HUB", hub), patch.object(HARNESS, "DOC_GLOBS", ["CLAUDE.md"]):
                    result = HARNESS.check_pointers()
                seen[pointer] = (result.state, result.findings)
        self.assertEqual({**{pointer: ("PASS", []) for pointer in declared},
                          **{pointer: ("WARN", [f"CLAUDE.md:1 -> {pointer}"]) for pointer in undeclared}},
                         seen)

    def test_a_doc_glob_that_matches_nothing_warns_in_the_bare_pointer_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / "CLAUDE.md").write_text("no pointers\n", encoding="utf-8")
            with patch.object(HARNESS, "HUB", hub), \
                    patch.object(HARNESS, "DOC_GLOBS", ["CLAUDE.md", "retired/*.md"]):
                result = HARNESS.check_pointers()
                self.assertEqual("WARN", result.state)
                self.assertEqual(["retired/*.md"], result.findings)
                self.assertEqual("PASS", HARNESS.check_pointers(staged=["CLAUDE.md"]).state)

    def test_the_tracked_documents_pass_the_bare_pointer_check(self):
        # Drawer/*.md left DOC_GLOBS when those files became cards; a retired glob fails
        # every bare run. Rebuild the tracked documents, pointer-free, in a scratch hub.
        tracked = subprocess.run(["git", "ls-files"], cwd=HUB, capture_output=True, text=True,
                                 check=True).stdout.splitlines()

        def tracked_for(glob):
            return {path for path in tracked
                    if Path(path).match(glob) and len(Path(path).parts) == len(Path(glob).parts)}

        # Until each glob has a tracked document, skip.
        waiting = [g for g in HARNESS.DOC_GLOBS if not tracked_for(g)]
        if waiting:
            self.skipTest(f"no tracked document yet for {waiting} ")
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            for path in set().union(*(tracked_for(g) for g in HARNESS.DOC_GLOBS)):
                (hub / path).parent.mkdir(parents=True, exist_ok=True)
                (hub / path).write_text("no pointers\n", encoding="utf-8")
            with patch.object(HARNESS, "HUB", hub):
                result = HARNESS.check_pointers()
            self.assertEqual(("PASS", []), (result.state, result.findings))

    def test_blobs_warn_on_evidence_images_and_anything_over_one_megabyte(self):
        sizes = {"Drawer/x.json": 2 * 1024 * 1024, "LevelDesign/reports/a.png": 10,
                 "UI/cursors/a.png": 10, "Drawer/ok.json": 10}

        def index(paths=None):
            # the real _index_sizes keeps only the given paths; no path is the whole index
            return {path: size for path, size in sizes.items() if not paths or path in paths}

        with patch.object(HARNESS, "_index_sizes", side_effect=index):
            result = HARNESS.check_blobs(staged=list(sizes))
            self.assertEqual("WARN", result.state)
            self.assertEqual(["Drawer/x.json (2048 KB, over 1 MB)",
                              "LevelDesign/reports/a.png (image under an evidence home)"],
                             result.findings)
            # blobs judge the staged paths: an oversize blob nobody staged is not this commit's
            scoped_pass = HARNESS.check_blobs(staged=["Drawer/ok.json"])
            self.assertEqual("PASS", scoped_pass.state)
            self.assertEqual([], scoped_pass.findings)
            scoped_fail = HARNESS.check_blobs(staged=["Drawer/x.json"])
            self.assertEqual("WARN", scoped_fail.state)
            self.assertEqual(["Drawer/x.json (2048 KB, over 1 MB)"], scoped_fail.findings)
        with patch.object(HARNESS, "_index_sizes", return_value={k: sizes[k] for k in list(sizes)[2:]}):
            self.assertEqual("PASS", HARNESS.check_blobs().state)

    def test_index_sizes_keeps_only_staged_paths(self):
        """Staged paths past the OS argument limit still size; an unstaged index entry is not judged."""
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            write_hub(hub, {"a.py": "a" * 10, "b.py": "b" * 20})
            subprocess.run(["git", "init", "-q"], cwd=hub, check=True)
            subprocess.run(["git", "add", "a.py", "b.py"], cwd=hub, check=True)
            staged = ["a.py"] + [f"Drawer/staged-but-deleted-{i:06d}.py" for i in range(100000)]
            with patch.object(HARNESS, "HUB", hub):
                self.assertEqual({"a.py": 10}, HARNESS._index_sizes(staged))
                self.assertEqual({"a.py": 10, "b.py": 20}, HARNESS._index_sizes())

    def test_commit_subset_is_offline_and_judges_staged_paths(self):
        order, seen = [], {}

        def record(name, state, findings=()):
            def check(**kwargs):
                order.append(name)
                seen[name] = kwargs
                return HARNESS.Result(name, state, "", list(findings))
            return check

        # Every check is available; only the offline subset and the live MCP probe may run in a
        # commit hook: the seat stays out of it.
        fake = {name: record(name, "PASS") for name in CHECK_NAMES}

        class Policy:
            PolicyError = RuntimeError

            @staticmethod
            def _rebase_branch(repo):
                return ""

            @staticmethod
            def _staged_paths(repo):
                return ["CLAUDE.md", "Drawer/x.bak"]

        subset = ["settings", "skills", "hooks", "pointers", "agents", "hiding", "blobs", "pins", "cites", "config",
                  "mcp-live"]

        # The exported tree runs tree_checks in a child; here it runs in process on the fixture.
        with patch.object(HARNESS, "CHECKS", fake), patch.object(HARNESS, "_git_policy", lambda: Policy), \
                patch.object(HARNESS, "_judge_the_tree", lambda staged, stage=True: HARNESS.tree_checks(staged)), \
                patch.object(HARNESS, "check_settings", record("settings", "PASS")), \
                patch.object(HARNESS, "check_pointers", record("pointers", "PASS")), \
                patch.object(HARNESS, "check_hiding", record("hiding", "FAIL", ["Drawer/x.bak"])), \
                patch.object(HARNESS, "check_blobs", record("blobs", "PASS")), \
                patch.object(HARNESS, "check_cites", record("cites", "PASS")), \
                patch.object(HARNESS, "check_config", record("config", "PASS")), \
                patch.object(HARNESS, "check_mcp_live", record("mcp-live", "PASS")), \
                patch.dict("os.environ", {"HARNESS_CONFORM": "dry"}):
            with tempfile.TemporaryDirectory() as temporary:
                hub = Path(temporary)
                (hub / "Drawer").mkdir()
                shutil.copy2(HUB / "Drawer/gatelib.py", hub / "Drawer/gatelib.py")
                (hub / "CLAUDE.md").write_text("", encoding="utf-8")
                (hub / "Drawer/x.bak").write_text("", encoding="utf-8")
                with patch.object(HARNESS, "HUB", hub):
                    with patch("sys.stdout", new=io.StringIO()) as out:
                        self.assertEqual(1, HARNESS.commit_gate())
                    lines = out.getvalue().splitlines()
                    self.assertEqual("harness-conform: commit held; each failed check above names its fix",
                                     lines[-1])
                    printed = [line[7:].split()[0] for line in lines if line.startswith("[")]
                    first = list(order)
                    self.assertEqual({"staged": ["CLAUDE.md", "Drawer/x.bak"]}, seen["pointers"])
                    self.assertEqual({"staged": ["CLAUDE.md", "Drawer/x.bak"]}, seen["cites"])
                    # a staged path absent on disk is a deletion: every document is judged
                    (hub / "Drawer/x.bak").unlink()
                    order.clear()
                    self.assertEqual(1, HARNESS.commit_gate())
        # each check of the subset runs once, and the report lists them in subset order
        self.assertEqual(subset, printed)
        self.assertEqual(sorted(subset), sorted(first))
        self.assertEqual(sorted(subset), sorted(order))
        # the exported tree has no box-local .mcp.json: settings judge the tracked files only
        # ... and a drift there is rendered, for the gate to stage
        self.assertEqual({"tracked": True, "fix": True}, seen["settings"])
        self.assertEqual({"staged": None}, seen["pointers"])
        self.assertEqual({"staged": ["CLAUDE.md", "Drawer/x.bak"]}, seen["hiding"])
        self.assertEqual({"staged": ["CLAUDE.md", "Drawer/x.bak"]}, seen["blobs"])
        self.assertEqual({"staged": ["CLAUDE.md", "Drawer/x.bak"]}, seen["config"])
        self.assertEqual({"staged": ["CLAUDE.md", "Drawer/x.bak"]}, seen["mcp-live"])

    def test_each_failed_check_prints_its_own_fix(self):
        results = [HARNESS.Result("settings", "FAIL", "drift", ["a.json"]),
                   HARNESS.Result("hiding", "FAIL", "1 hiding place", ["x.bak"]),
                   HARNESS.Result("pointers", "PASS", "clean"),
                   HARNESS.Result("tree", "FAIL", "the commit's tree could not be exported", ["boom"])]
        with patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(1, HARNESS.report(results, verbose=False))
        recheck = f"{PY} Drawer/harness-conform.py --commit"
        self.assertEqual([
            "[FAIL] settings           drift",
            "         a.json",
            f"         fix: {PY} Drawer/harness-conform.py --apply, then git add the regenerated files",
            "[FAIL] hiding             1 hiding place",
            "         x.bak",
            "         fix: delete or rename each named hiding-place path",
            "[PASS] pointers           clean",
            "[FAIL] tree               the commit's tree could not be exported",
            "         boom",
            f"         fix: repair the error named above, then recheck: {recheck}",
            "",
            "RESULT: FAIL - 3 of 4 checks failed: settings, hiding, tree",
        ], out.getvalue().splitlines())

    def test_a_peers_unstaged_draft_does_not_hold_a_clean_path_commit(self):
        """The commit is judged on the index exported to its own tree, by that tree's copy of
        harness-conform, never on the shared working tree a peer is editing."""
        # The tree's copy is the real file; only its tree_checks is replaced, by one that
        # reports what the exported Drawer/tool.py holds.
        wrapper = (
            "import importlib.util, sys\nfrom pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location('hc', Path(__file__).with_name('harness_conform_real.py'))\n"
            "hc = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(hc)\n"
            "def judge(staged):\n"
            "    text = (hc.HUB / 'Drawer/tool.py').read_text(encoding='utf-8').strip()\n"
            "    return [hc.Result('hiding', 'FAIL' if 'DRAFT' in text else 'PASS', text, staged)]\n"
            "hc.tree_checks = judge\nsys.exit(hc.main(sys.argv[1:]))\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, env, clear=True):
            hub = Path(temporary)
            (hub / "Drawer").mkdir()
            (hub / "Drawer/harness-conform.py").write_text(wrapper, encoding="utf-8")
            shutil.copy2(PATH, hub / "Drawer/harness_conform_real.py")
            # the real file reads the hub config from the tree's own copy
            shutil.copy2(HUB / "Drawer/harness_config.py", hub / "Drawer/harness_config.py")
            shutil.copy2(HUB / "harness.toml", hub / "harness.toml")
            tool = hub / "Drawer/tool.py"
            tool.write_text("clean\n", encoding="utf-8")

            def git(*args):
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                               cwd=hub, capture_output=True, check=True)

            git("init", "-b", "main")
            git("add", "-A")
            git("commit", "-m", "fixture")
            tool.write_text("staged\n", encoding="utf-8")
            git("add", "Drawer/tool.py")
            tool.write_text("DRAFT by a peer\n", encoding="utf-8")  # unstaged
            with patch.object(HARNESS, "HUB", hub):
                clean = HARNESS._judge_the_tree(["Drawer/tool.py"])
                git("add", "Drawer/tool.py")  # positive control: the draft staged is judged
                held = HARNESS._judge_the_tree(["Drawer/tool.py"])
            self.assertEqual("DRAFT by a peer\n", tool.read_text(encoding="utf-8"))
        self.assertEqual([("hiding", "PASS", "staged", ["Drawer/tool.py"])],
                         [(r.name, r.state, r.summary, r.findings) for r in clean])
        self.assertEqual([("hiding", "FAIL", "DRAFT by a peer", ["Drawer/tool.py"])],
                         [(r.name, r.state, r.summary, r.findings) for r in held])

    def test_the_exported_tree_link_is_removed_before_the_tree(self):
        """The exported tree links the real interpreter. Cleanup removes the link and never
        follows it: the interpreter survives, and the whole temporary folder goes."""
        # The tree's own harness-conform writes a marker through its .venv: only a link, not a
        # copy, puts that marker in the real interpreter folder.
        stub = ("import json, sys\nfrom pathlib import Path\n"
                "venv = Path(__file__).parents[1] / '.venv'\n"
                "seen = (venv / 'python.exe').is_file()\n"
                "(venv / 'marker').write_text('through the link', encoding='utf-8')\n"
                "rows = [{'name': 'hiding', 'state': 'PASS', 'summary': f'linked={seen}', 'findings': []}]\n"
                "Path(sys.argv[2], 'results.json').write_text(json.dumps(rows), encoding='utf-8')\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, env, clear=True):
            hub = Path(temporary, "hub")
            (hub / "Drawer").mkdir(parents=True)
            (hub / "Drawer/harness-conform.py").write_text(stub, encoding="utf-8")
            real = hub / ".venv"  # untracked, as on a seat
            real.mkdir()
            (real / "python.exe").write_text("interpreter", encoding="utf-8")

            def git(*args):
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                               cwd=hub, capture_output=True, check=True)

            git("init", "-b", "main")
            git("add", "Drawer")
            roots = []
            real_mkdtemp = tempfile.mkdtemp

            def mkdtemp(*args, **kwargs):
                roots.append(Path(real_mkdtemp(*args, **{**kwargs, "dir": temporary})))
                return str(roots[-1])

            with patch.object(HARNESS, "HUB", hub), \
                    patch.object(HARNESS.tempfile, "mkdtemp", side_effect=mkdtemp):
                results = HARNESS._judge_the_tree(["Drawer/harness-conform.py"])
            self.assertEqual([("hiding", "PASS", "linked=True")],
                             [(r.name, r.state, r.summary) for r in results])
            self.assertFalse(os.path.lexists(roots[0]))
            # a missing file reads "" so a followed link or a copy fails the assert, not errors
            def text(path):
                return path.read_text(encoding="utf-8") if path.is_file() else ""
            self.assertEqual(("interpreter", "through the link"),
                             (text(real / "python.exe"), text(real / "marker")))

    def test_a_timed_out_run_dies_with_its_grandchild_holding_the_pipe(self):
        """A hung child whose grandchild inherits stdout: the timeout kills the whole tree,
        so the drain ends at the bound, not when the grandchild would have exited."""
        shard = ("import subprocess, sys, time\n"
                 "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                 "time.sleep(60)\n")
        started = time.monotonic()
        result = HARNESS._run([sys.executable, "-c", shard], timeout=3)
        elapsed = time.monotonic() - started
        self.assertEqual((None, "timed out after 3s"), result)
        self.assertLess(elapsed, 30)

    def test_commit_subset_skips_during_a_rebase(self):
        """A rebase's intermediate trees disagree with the manifest: no staged path is read."""
        read, ran = [], []

        class Policy:
            PolicyError = RuntimeError

            @staticmethod
            def _rebase_branch(repo):
                return "main"

            @staticmethod
            def _staged_paths(repo):
                read.append(str(repo))
                return ["Drawer/tool.py"]

        def record(name):
            def check(**kwargs):
                ran.append(name)
                return HARNESS.Result(name, "PASS", "")
            return check

        # Every check records itself instead of running: a subset that does not skip reads the
        # staged paths and runs checks, and both lists say so.
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / "Drawer").mkdir()
            shutil.copy2(HUB / "Drawer/gatelib.py", hub / "Drawer/gatelib.py")
            with patch.object(HARNESS, "HUB", hub), \
                    patch.object(HARNESS, "_git_policy", lambda: Policy), \
                    patch.object(HARNESS, "CHECKS", recording_checks(ran)), \
                    patch.object(HARNESS, "check_settings", record("settings")), \
                    patch.object(HARNESS, "check_pointers", record("pointers")), \
                    patch.object(HARNESS, "check_hiding", record("hiding")), \
                    patch.object(HARNESS, "check_blobs", record("blobs")), \
                    patch.object(HARNESS, "check_mcp_live", record("mcp-live")), \
                    patch.dict("os.environ", {"HARNESS_CONFORM": "dry"}):
                self.assertEqual(0, HARNESS.commit_gate())
        self.assertEqual([], read)
        self.assertEqual([], ran)

    def _precheck_fixture(self, temporary, tree_state):
        """A real repo with a.py changed and peer.py untracked; tree and live MCP record.

        Staged paths come from the index git sees, the private one included. Returns a git runner,
        the staged list of each tree-check run, and the names of the other checks that ran.
        """
        hub = Path(temporary)
        (hub / "Drawer").mkdir()
        shutil.copy2(HUB / "Drawer/gatelib.py", hub / "Drawer/gatelib.py")
        (hub / "a.py").write_text("a = 1\n", encoding="utf-8")

        def git(*args):
            return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                                  cwd=hub, capture_output=True, text=True, check=True).stdout

        git("init", "-b", "main")
        git("add", "a.py")
        git("commit", "-m", "fixture")
        (hub / "a.py").write_text("a = 2\n", encoding="utf-8")
        (hub / "peer.py").write_text("p = 1\n", encoding="utf-8")
        trees, others = [], []

        class Policy:
            PolicyError = RuntimeError

            @staticmethod
            def _rebase_branch(repo):
                return ""

            @staticmethod
            def _staged_paths(repo):
                return git("diff", "--cached", "--name-only").split()

        self.stage_args = []

        def judge(staged, stage=True):
            trees.append(staged)
            self.stage_args.append(stage)
            return [HARNESS.Result("settings", tree_state, ""), HARNESS.Result("hiding", "PASS", "")]

        def record(name):
            def check(**kwargs):
                others.append(name)
                return HARNESS.Result(name, "PASS", "")
            return check

        for name, value in (("HUB", hub), ("_git_policy", lambda: Policy), ("_judge_the_tree", judge),
                            ("check_mcp_live", record("mcp-live"))):
            patcher = patch.object(HARNESS, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", {"HARNESS_CONFORM": "dry"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GIT_INDEX_FILE", None)
        return git, trees, others

    def test_commit_precheck_records_a_clean_tree_and_the_hook_skips_its_tree_checks(self):
        """--commit -- <paths> judges HEAD plus paths in a private index; the hook skips that tree only."""
        with tempfile.TemporaryDirectory() as temporary:
            git, trees, others = self._precheck_fixture(temporary, "PASS")
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.main(["--commit", "--", "a.py"]))
            # the precheck judged exactly HEAD plus a.py, never the peer's file, and stages nothing
            self.assertEqual([["a.py"]], trees)
            self.assertEqual([False], self.stage_args)
            # the real index is untouched and no private index leaks into the session
            self.assertEqual("", git("diff", "--cached", "--name-only"))
            self.assertNotIn("GIT_INDEX_FILE", os.environ)
            # the hook on the same tree skips the tree checks but runs the live MCP check
            git("add", "a.py")
            others.clear()
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.commit_gate())
            self.assertEqual([["a.py"]], trees)
            self.assertEqual(["mcp-live"], others)
            # any other tree runs the tree checks in the hook
            git("add", "peer.py")
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.commit_gate())
            self.assertEqual([["a.py"], ["a.py", "peer.py"]], trees)
            self.assertEqual([False, False], self.stage_args)  # dry judges and stages nothing

    def test_commit_precheck_does_not_run_the_live_mcp_check_and_the_hook_does(self):
        """The precheck's private index has no skip-worktree bits; only the hook runs the live MCP check."""
        with tempfile.TemporaryDirectory() as temporary:
            git, _, others = self._precheck_fixture(temporary, "PASS")
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.main(["--commit", "--", "a.py"]))
            self.assertEqual([], others)
            git("add", "a.py")
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.commit_gate())
            self.assertEqual(["mcp-live"], others)

    def test_commit_precheck_records_nothing_when_a_tree_check_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            git, trees, _ = self._precheck_fixture(temporary, "FAIL")
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(1, HARNESS.main(["--commit", "--", "a.py"]))
            self.assertEqual("", git("diff", "--cached", "--name-only"))
            git("add", "a.py")
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(1, HARNESS.commit_gate())
            # the hook judged the same tree again: the failed precheck recorded no pass
            self.assertEqual([["a.py"], ["a.py"]], trees)

    def _gate_log_fixture(self, temporary):
        """The precheck fixture with a.py staged, the gate log live, and the tree results settable.
        Returns a git runner, the settable results list and the gate log path."""
        git, _, _ = self._precheck_fixture(temporary, "PASS")
        git("add", "a.py")
        tree = []
        judge = patch.object(HARNESS, "_judge_the_tree", lambda staged, stage=True: list(tree))
        judge.start()
        self.addCleanup(judge.stop)
        live = patch.dict("os.environ", {"HARNESS_CONFORM": ""})
        live.start()
        self.addCleanup(live.stop)
        return git, tree, Path(temporary) / "Drawer/commit-gate.log"

    def test_a_deny_logs_each_failed_check_and_its_failing_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            git, tree, log = self._gate_log_fixture(temporary)
            tree[:] = [HARNESS.Result("settings", "PASS", "clean"),
                       HARNESS.Result("hiding", "FAIL", "1 hiding-place path(s) on disk",
                                      ["Drawer/x.bak\n  second line"])]
            # dry judges and logs nothing; off skips the subset
            for mode, code in (("dry", 1), ("off", 0)):
                with patch.dict("os.environ", {"HARNESS_CONFORM": mode}), patch("sys.stdout", new=io.StringIO()):
                    self.assertEqual(code, HARNESS.commit_gate())
            self.assertFalse(log.exists())
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(1, HARNESS.commit_gate())
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            self.assertIn(" deny ", lines[0])
            self.assertIn(f" tree {git('write-tree').strip()[:12]}: ", lines[0])
            causes = lines[0].split(": ", 1)[1]
            for part in ("hiding", "1 hiding-place path(s) on disk", "Drawer/x.bak", "second line"):
                self.assertIn(part, causes)
            self.assertNotIn("settings", causes)

    def test_staged_hiding_and_pointers_judge_only_the_staged_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            (hub / "CLAUDE.md").write_text("see `Drawer/missing.py`\n", encoding="utf-8")
            (hub / "notes.bak").write_text("", encoding="utf-8")
            with patch.object(HARNESS, "HUB", hub), patch.object(HARNESS, "DOC_GLOBS", ["CLAUDE.md"]):
                self.assertEqual("FAIL", HARNESS.check_hiding().state)
                self.assertEqual("PASS", HARNESS.check_hiding(staged=["CLAUDE.md"]).state)
                self.assertEqual("FAIL", HARNESS.check_hiding(staged=["notes.bak"]).state)
                self.assertEqual("WARN", HARNESS.check_pointers().state)
                self.assertEqual("PASS", HARNESS.check_pointers(staged=["notes.bak"]).state)
                self.assertEqual("WARN", HARNESS.check_pointers(staged=["CLAUDE.md"]).state)

    def test_a_dependency_tree_is_not_walked_for_hiding_places(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            sandbox = hub / "UI" / "node_modules" / "package-1"
            sandbox.mkdir(parents=True)
            (sandbox / "notes.bak").write_text("", encoding="utf-8")
            with patch.object(HARNESS, "HUB", hub):
                self.assertEqual("PASS", HARNESS.check_hiding().state)
                (hub / "notes.bak").write_text("", encoding="utf-8")
                self.assertEqual(["notes.bak"], HARNESS.check_hiding().findings)

    def test_the_copy_exclude_list_is_not_a_hiding_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            write_hub(hub, {"Drawer/never-copy.txt": ""})
            with patch.object(HARNESS, "HUB", hub):
                clean = HARNESS.check_hiding()
                write_hub(hub, {"Drawer/notes-copy.txt": ""})
                held = HARNESS.check_hiding()
        self.assertEqual(("PASS", []), (clean.state, clean.findings))
        self.assertEqual(["Drawer/notes-copy.txt"], held.findings)

    def test_apply_runs_every_generator_then_the_offline_checks(self):
        """--no-check must run no check, and a bare --apply must run the offline subset only."""
        seen, ran = [], []

        def runner(args, timeout=600):
            seen.append([Path(args[2]).name, *args[3:]])
            return 0, "wrote"

        with patch.object(HARNESS, "_run", side_effect=runner), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch.object(HARNESS, "CHECKS", recording_checks(ran)):
            self.assertEqual(0, HARNESS.main(["--apply", "--no-check"]))
            self.assertEqual(GENERATORS, seen)
            self.assertEqual([], ran)
            self.assertEqual(0, HARNESS.main(["--apply"]))
        self.assertEqual(GENERATORS * 2, seen)
        self.assertEqual(OFFLINE, ran)

    def test_a_failing_generator_stops_apply_before_the_later_generators_and_the_checks(self):
        """A generator that fails leaves the tree half-written: nothing after it may run."""
        seen, ran = [], []

        def runner(args, timeout=600):
            seen.append([Path(args[2]).name, *args[3:]])
            return (1, "settings-sync: drift") if len(seen) == 1 else (0, "wrote")

        generators = [*HARNESS.GENERATORS, ["Drawer/later-generator.py"]]
        with patch.object(HARNESS, "_run", side_effect=runner), \
                patch.object(HARNESS, "GENERATORS", generators), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch.object(HARNESS, "CHECKS", recording_checks(ran)):
            self.assertEqual(1, HARNESS.main(["--apply"]))
        self.assertEqual(GENERATORS[:1], seen)
        self.assertEqual([], ran)
        self.assertEqual("[ERR ] settings-sync.py       settings-sync: drift",
                         out.getvalue().splitlines()[-1])

    def test_hook_resolves_from_the_hub_root_project_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            settings = hub / ".claude/settings.json"
            gate = hub / "Drawer/example-gate.py"
            settings.parent.mkdir(parents=True)
            gate.parent.mkdir(parents=True)
            gate.write_text("", encoding="utf-8")
            settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{
                "type": "command",
                "command": 'py "$CLAUDE_PROJECT_DIR/Drawer/example-gate.py"',
            }]}]}}), encoding="utf-8")
            with patch.object(HARNESS, "HUB", hub):
                result = HARNESS.check_hooks()
            self.assertEqual("PASS", result.state)

            gate.unlink()
            with patch.object(HARNESS, "HUB", hub):
                result = HARNESS.check_hooks()
            self.assertEqual("FAIL", result.state)

    def test_form_checks_warn_and_the_report_passes_with_their_fix(self):
        """A form finding (pointers, skills, agents, blobs, pins) warns;
        the report exits 0 and still prints each finding and its fix."""
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary)
            write_hub(hub, {"CLAUDE.md": "Load the /nosuchskill skill.\nsubagent_type: nosuchagent\n"})
            with patch.object(HARNESS, "HUB", hub), patch.object(HARNESS, "DOC_GLOBS", ["CLAUDE.md"]):
                skills, agents = HARNESS.check_skills(), HARNESS.check_agents()
        self.assertEqual(("WARN", ["CLAUDE.md:1 -> /nosuchskill"]), (skills.state, skills.findings))
        self.assertEqual(("WARN", ["CLAUDE.md:2 -> subagent_type=nosuchagent"]), (agents.state, agents.findings))
        results = [HARNESS.Result(name, "WARN", "form", [f"{name} case"])
                   for name in ("pointers", "skills", "agents", "blobs", "pins")]
        results.append(HARNESS.Result("hiding", "PASS", "clean"))
        with patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(0, HARNESS.report(results, verbose=False))
        lines = out.getvalue().splitlines()
        self.assertIn("[WARN] pointers           form", lines)
        self.assertIn("         pointers case", lines)
        self.assertIn("         fix: correct or remove each named dead path", lines)
        self.assertEqual("RESULT: PASS - 6 checks, 5 warning(s): pointers, skills, agents, blobs, pins", lines[-1])
        # a hard check beside the warnings still fails the run
        results.append(HARNESS.Result("hiding", "FAIL", "1 hiding place", ["x.bak"]))
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(1, HARNESS.report(results, verbose=False))

    def test_check_settings_fix_renders_a_drift_and_names_the_files(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args[3:])
            if "--apply" in args:
                return 0, "  wrote: .claude/settings.json\nwrote: 1 of 2 generated files\n"
            return 1, "  DRIFT: .claude/settings.json\nDRIFT: 1 of 2 generated files\n"

        with patch.object(HARNESS, "_run", side_effect=run):
            held = HARNESS.check_settings(tracked=True)
            fixed = HARNESS.check_settings(tracked=True, fix=True)
        self.assertEqual(("FAIL", []), (held.state, held.fixed))
        self.assertEqual(("PASS", [".claude/settings.json"]), (fixed.state, fixed.fixed))
        self.assertEqual([["--tracked"], ["--tracked"], ["--apply", "--tracked"]], calls)
        # a manifest settings-sync cannot render (exit 2) is never fixed: it holds
        with patch.object(HARNESS, "_run", return_value=(2, "ERROR: bad manifest")):
            broken = HARNESS.check_settings(tracked=True, fix=True)
        self.assertEqual(("FAIL", "ERROR: bad manifest", []), (broken.state, broken.summary, broken.fixed))

    def test_a_tree_the_gate_rendered_records_no_precheck_pass(self):
        """A rendered drift changes the tree the commit delivers: its hash is no clean pass."""
        with tempfile.TemporaryDirectory() as temporary:
            git, tree, _ = self._gate_log_fixture(temporary)
            tree[:] = [HARNESS.Result("settings", "PASS", "fixed", [".claude/settings.json"],
                                      fixed=[".claude/settings.json"]),
                       HARNESS.Result("pointers", "WARN", "1 dead path reference(s)", ["x"])]
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.commit_gate())
            self.assertFalse(HARNESS._prechecked(git("write-tree").strip()))
            tree[:] = [HARNESS.Result("pointers", "WARN", "1 dead path reference(s)", ["x"])]
            with patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.commit_gate())
            self.assertTrue(HARNESS._prechecked(git("write-tree").strip()))

    def test_the_live_mcp_check_judges_a_staged_render(self):
        """Review finding: a manifest edit renders settings.json; the live MCP check must see that path."""
        with tempfile.TemporaryDirectory() as temporary:
            git, tree, _ = self._gate_log_fixture(temporary)
            tree[:] = [HARNESS.Result("settings", "PASS", "fixed", [".claude/settings.json"],
                                      fixed=[".claude/settings.json"])]
            seen = []
            with patch.object(HARNESS, "check_mcp_live",
                              lambda staged: seen.append(staged) or HARNESS.Result("mcp-live", "PASS", "")), \
                    patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, HARNESS.commit_gate())
        self.assertEqual([["a.py", ".claude/settings.json"]], seen)

    def test_a_render_note_turns_the_settings_pass_into_a_warning(self):
        """A render step that did not run (a peer's working-tree edit kept) shows as WARN, never a silent PASS."""
        with tempfile.TemporaryDirectory() as temporary:
            git, _, _ = self._precheck_fixture(temporary, "PASS")
            hub = Path(temporary)
            stub = ("import json, sys\nfrom pathlib import Path\n"
                    "io = Path(sys.argv[2]); tree = io / 'tree'\n"
                    "(tree / 'a.py').write_text('a = 9\\n')\n"
                    "(io / 'results.json').write_text(json.dumps([{'name': 'settings', 'state': 'PASS',"
                    " 'summary': 'fixed', 'findings': ['a.py'], 'fixed': ['a.py']}]))\n")
            (hub / "Drawer/harness-conform.py").write_text(stub, encoding="utf-8")
            git("add", "Drawer/harness-conform.py")
            git("add", "a.py")
            (hub / "a.py").write_text("a = 'peer draft'\n", encoding="utf-8")  # unstaged peer edit
            patch.stopall()
            with patch.object(HARNESS, "HUB", hub):
                results = HARNESS._judge_the_tree(["a.py"])
            self.assertEqual("WARN", results[0].state)
            self.assertIn("a.py: the working tree copy is someone's edit; left as is", results[0].findings)
            self.assertEqual("a = 'peer draft'\n", (hub / "a.py").read_text(encoding="utf-8"))
            self.assertIn("a = 9", git("show", ":a.py"))

    def test_seat_check_uses_the_doctor_and_ignores_user_settings(self):
        seen = []

        def runner(args, timeout=600):
            seen.append([Path(args[2]).name, *args[3:]])
            return 0, "in sync"

        with patch.object(HARNESS, "_run", side_effect=runner):
            self.assertEqual("PASS", HARNESS.check_seat().state)
        self.assertEqual([["seat-doctor.py"]], seen)

        with patch.object(HARNESS, "_run", return_value=(1, "RESULT: FAIL - box")):
            result = HARNESS.check_seat()
        self.assertEqual("FAIL", result.state)
        self.assertEqual(["RESULT: FAIL - box"], result.findings)

    def test_seat_is_a_registered_check_with_a_real_doctor(self):
        """A check that leaves the table stops running and --list stops naming it."""
        seen = []

        def runner(args, timeout=600):
            seen.append([Path(args[2]).name, *args[3:]])
            return 0, "in sync"

        with patch("sys.stdout", new_callable=io.StringIO) as listing:
            self.assertEqual(0, HARNESS.main(["--list"]))
        self.assertEqual(CHECK_NAMES, listing.getvalue().splitlines())
        # named on the command line, "seat" runs check_seat, which runs the doctor on disk
        with patch.object(HARNESS, "_run", side_effect=runner), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, HARNESS.main(["seat"]))
        self.assertEqual([["seat-doctor.py"]], seen)
        self.assertTrue((HUB / "Drawer/seat-doctor.py").is_file())

    def test_unavailable_aggregate_is_incomplete_exit_two(self):
        unavailable = lambda: HARNESS.Result("optional", "UNAVAILABLE", "peer absent")
        with patch.object(HARNESS, "CHECKS", {"optional": unavailable}):
            self.assertEqual(2, HARNESS.main([]))

    def test_unknown_flag_refused_before_any_check(self):
        for argv in (["--appply"], ["law", "-x"]):
            with patch.object(HARNESS, "CHECKS", {}), patch.object(HARNESS, "apply_generators") as gen,                     patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(2, HARNESS.main(argv))
            gen.assert_not_called()
            self.assertIn(f"unknown flag(s): {argv[-1]}", err.getvalue())

    def test_help_and_h_print_the_docstring_and_run_no_check(self):
        for flag in ("--help", "-h"):
            with patch.object(HARNESS, "CHECKS", {}), patch.object(HARNESS, "apply_generators") as gen, \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(0, HARNESS.main([flag]))
            gen.assert_not_called()
            self.assertEqual(HARNESS.__doc__.strip(), out.getvalue().strip())


class HelpFlagTests(unittest.TestCase):
    # Defect: -h and --help were refused as unknown flags, unlike every other Drawer tool.
    def test_help_prints_the_module_docstring_and_runs_no_check(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag), patch.object(HARNESS, "CHECKS", {}), \
                    patch.object(HARNESS, "apply_generators") as gen, \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(0, HARNESS.main([flag]))
            gen.assert_not_called()
            self.assertEqual(HARNESS.__doc__.strip(), out.getvalue().strip())


class ConfigCheckTests(unittest.TestCase):
    """WRK-2: a model id, port or hub path that harness.toml names is not hard-coded in Drawer code."""

    TOML = ('[project]\nstate_root = "~/.local/share/zqhub"\n'
            '[routing]\ndefault = { model = "opus", effort = "medium" }\n'
            '[product]\npath = ""\nmcp_url = "http://127.0.0.1:6010/mcp"\n')
    BAD = ('MODEL = "opus"\nURL = "http://127.0.0.1:6010/mcp"\nPORT = 6010\n'
           'STATE = "~/.local/share/zqhub/x.db"\n')
    GOOD = ('"""Names opus, the default model."""\n'
            'import re\n'
            'ALIAS = re.compile("opus|sonnet")  # the editor listens on 6010\n'
            'TIMEOUT = 6010\n'
            'FLAG = True\n'
            'def model():\n'
            '    """Return opus from harness.toml."""\n'
            '    return harness_config.load()["routing"]["default"]["model"]\n')
    FINDINGS = ["Drawer/bad.py:1 model 'opus'", "Drawer/bad.py:2 port 6010",
                "Drawer/bad.py:3 port 6010", "Drawer/bad.py:4 path '~/.local/share/zqhub'"]

    def run_check(self, files, staged=None):
        with tempfile.TemporaryDirectory(prefix="conform config ") as temporary:
            hub = Path(temporary)
            write_hub(hub, files)
            with patch.object(HARNESS, "HUB", hub):
                return HARNESS.check_config(staged=staged)

    def fixture(self, **more):
        return {"harness.toml": self.TOML, "Drawer/bad.py": self.BAD, "Drawer/good.py": self.GOOD,
                "Drawer/test_fixture.py": 'MODEL = "opus"\nPORT = 6010\n', **more}

    def test_the_config_check_flags_a_hard_coded_model_id_port_and_hub_path(self):
        result = self.run_check(self.fixture())
        self.assertEqual(("WARN", self.FINDINGS), (result.state, result.findings))
        self.assertEqual("4 hard-coded harness.toml value(s) in Drawer code", result.summary)

    def test_code_that_reads_harness_toml_passes(self):
        result = self.run_check({"harness.toml": self.TOML, "Drawer/good.py": self.GOOD,
                                 "Drawer/broken.py": "if (\n"})
        self.assertEqual(("PASS", []), (result.state, result.findings))

    def test_only_the_staged_drawer_files_are_judged_unless_harness_toml_is_staged(self):
        self.assertEqual("PASS", self.run_check(self.fixture(), staged=["Drawer/good.py", "CLAUDE.md"]).state)
        self.assertEqual(self.FINDINGS, self.run_check(self.fixture(), staged=["Drawer/bad.py"]).findings)
        self.assertEqual(self.FINDINGS, self.run_check(self.fixture(), staged=["harness.toml"]).findings)

    def test_a_hub_with_no_harness_toml_passes(self):
        result = self.run_check({"Drawer/bad.py": self.BAD})
        self.assertEqual(("PASS", []), (result.state, result.findings))


class PinsCheckTests(unittest.TestCase):
    """A per-box install outside the seat pin stamp warns, by name; an unreadable stamp holds the commit."""

    def run_check(self, files, exempt):
        with tempfile.TemporaryDirectory(prefix="conform pins ") as temporary:
            hub = Path(temporary)
            subprocess.run(["git", "init", "-q", str(hub)], check=True)
            for path, text in files.items():
                (hub / path).parent.mkdir(parents=True, exist_ok=True)
                (hub / path).write_bytes(text.encode("utf-8"))
            subprocess.run(["git", "-C", str(hub), "add", "--", *files], check=True)
            stamp = ({"uv tool install good==1"}, {"server/uv.lock"})
            with patch.object(HARNESS, "HUB", hub), patch.object(HARNESS, "_stamp_rows", return_value=stamp), \
                    patch.object(HARNESS, "UNSTAMPED_LOCKS", exempt):
                return HARNESS.check_pins()

    def test_a_stamped_or_exempt_install_passes(self):
        result = self.run_check({"tools/mcp.py": 'PIN = "uv tool install good==1"\n',
                                 "tools/scan.py": 'PREFIX = "uv tool install "  # names no tool\n',
                                 "server/uv.lock": "version = 1\n",
                                 "viewer/package-lock.json": "{}\n",
                                 "tools/tests/test_mcp.py": 'FAKE = "uv tool install fake==0"\n'},
                                {"viewer/package-lock.json": "smoke only"})
        self.assertEqual(("PASS", []), (result.state, result.findings))

    def test_each_install_outside_the_stamp_is_named(self):
        result = self.run_check({"tools/mcp.py": 'PIN = ("uv tool install "\n       "rogue==2")\n',
                                 "tools/broken.py": 'PIN = "uv tool install good==1"\nif (\n',
                                 "server/uv.lock": "version = 1\n",
                                 "lane/requirements.lock": "pillow==1\n"},
                                {"gone/package-lock.json": "deleted"})
        self.assertEqual("WARN", result.state)
        self.assertEqual(sorted([
            "lane/requirements.lock: a lock file outside the seat pin stamp; add it to LOCKS in "
            "Drawer/runtime/seat.py or to UNSTAMPED_LOCKS in Drawer/harness-conform.py with the reason",
            "gone/package-lock.json: UNSTAMPED_LOCKS names a lock Git does not track",
            "tools/mcp.py:1: 'uv tool install rogue==2' is not a uv tool pin of the seat stamp "
            "(Drawer/runtime/seat.py)",
            "tools/broken.py: unreadable (SyntaxError)",
        ]), sorted(result.findings))

    def test_the_base_stamp_holds_no_uv_tool_and_the_root_lock(self):
        """The base seat stamps no uv tool (seat.uv_tools is gone) and stamps the root uv.lock."""
        self.assertEqual((set(), {"uv.lock"}), HARNESS._stamp_rows())

    def test_a_stamp_or_index_that_cannot_be_read_holds_the_commit(self):
        unreadable = "the seat pin stamp or the Git index could not be read"
        with patch.object(HARNESS, "_stamp_rows", side_effect=ImportError("no runtime package")):
            result = HARNESS.check_pins()
        self.assertEqual(("FAIL", unreadable, ["ImportError: no runtime package"]),
                         (result.state, result.summary, result.findings))
        git_failed = subprocess.CompletedProcess(["git"], 128, "", "fatal: not a git repository\n")
        with patch.object(HARNESS, "_stamp_rows", return_value=(set(), set())), \
                patch.object(HARNESS.subprocess, "run", return_value=git_failed):
            result = HARNESS.check_pins()
        self.assertEqual(("FAIL", unreadable, ["OSError: fatal: not a git repository"]),
                         (result.state, result.summary, result.findings))


if __name__ == "__main__":
    unittest.main()
