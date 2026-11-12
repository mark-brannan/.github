#!/usr/bin/env python3
# Tests for the fixer job's script. Run:
# python3 .github/actions/easy-fixes/fixer.test.py
#
# What matters: only this head's open, unanswered claude[bot] [easy] threads
# are taken; a suggestion fence replaces exactly its lines; any path, hooks and
# workflows included, is fixed like another; an edit no taken thread
# names never reaches the commit; and every thread gets one reply.
import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "fixer.py"
HEAD = "a" * 40
SIGN = "🤖 claude-fixer · run 1"


def node(i, path, line, body, start=None, login="claude", commit=HEAD, total=1,
         resolved=False, side="RIGHT", kind="Bot"):
    return {"id": f"T{i}", "isResolved": resolved, "path": path, "diffSide": side,
            "originalLine": line, "originalStartLine": start,
            "comments": {"totalCount": total, "nodes": [{
                "databaseId": 100 + i, "url": f"https://x/{i}", "author": {"login": login, "__typename": kind},
                "body": body, "originalCommit": {"oid": commit}}]}}


def body_fix(old, new):
    return f"🤖 [nit] [easy] the body is stale\n```body-old\n{old}\n```\n```body-new\n{new}\n```\n🤖 claude-sonnet-5-5 · medium · 1a2b3c4d"


def fence(text):
    return f"🤖 [nit] [easy] fix it\n```suggestion\n{text}```\n🤖 claude-sonnet-5-5 · medium · 1a2b3c4d"


class Repo:
    def __init__(self, files):
        self.root = tempfile.mkdtemp()
        for p, t in files.items():
            f = Path(self.root, p)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(t.encode() if isinstance(t, str) else t)
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                     "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", self.root, *cmd], check=True)

    def read(self, p):
        return Path(self.root, p).read_text()

    def write(self, p, t):
        Path(self.root, p).write_text(t)

    def run(self, *args, data=None, name="in.json"):
        if data is not None:
            Path(self.root, "..", name).resolve().write_text(json.dumps(data))
        out = Path(self.root, "..", "out.json").resolve()
        subprocess.run(["python3", str(SCRIPT), *args, "--out", str(out)], check=True)
        return json.loads(out.read_text())

    def tmp(self, name):
        return str(Path(self.root, "..", name).resolve())

    def plan(self, nodes, head_message=None, pr_body=None):
        Path(self.tmp("threads.json")).write_text(json.dumps(nodes))
        extra = []
        Path(self.tmp("body-out.md")).unlink(missing_ok=True)
        if pr_body is not None:
            Path(self.tmp("body.md")).write_bytes(pr_body.encode())
            extra = ["--pr-body", self.tmp("body.md"), "--body-out", self.tmp("body-out.md")]
        if head_message is not None:
            Path(self.tmp("msg.txt")).write_text(head_message)
            extra += ["--head-message", self.tmp("msg.txt")]
        res = self.run("plan", "--threads", self.tmp("threads.json"), "--head", HEAD, "--root", self.root,
                       *extra)
        Path(self.tmp("plan.json")).write_text(json.dumps(res))
        return res

    def new_body(self):
        f = Path(self.tmp("body-out.md"))
        return f.read_text() if f.exists() else None

    def gate(self, agent):
        Path(self.tmp("agent.json")).write_text(json.dumps(agent))
        res = self.run("gate", "--plan", self.tmp("plan.json"), "--agent", self.tmp("agent.json"),
                       "--root", self.root)
        Path(self.tmp("gate.json")).write_text(json.dumps(res))
        return res


class Plan(unittest.TestCase):
    def test_suggestion_replaces_its_lines(self):
        r = Repo({"README.md": "# Title\n## Where thing are\nbody\n"})
        res = r.plan([node(1, "README.md", 2, fence("## Where things are\n"))])
        self.assertEqual(r.read("README.md"), "# Title\n## Where things are\nbody\n")
        self.assertEqual(res[0]["status"], "fixed")

    def test_multi_line_range_and_two_suggestions_in_one_file(self):
        r = Repo({"a.txt": "1\n2\n3\n4\n5\n"})
        r.plan([node(1, "a.txt", 3, fence("two-three\n"), start=2),
                node(2, "a.txt", 5, fence("five\nsix\n"))])
        self.assertEqual(r.read("a.txt"), "1\ntwo-three\n4\nfive\nsix\n")

    def test_empty_fence_deletes_and_blank_fence_is_a_blank_line(self):
        r = Repo({"a.txt": "1\n2\n3\n"})
        r.plan([node(1, "a.txt", 1, fence("")), node(2, "a.txt", 3, fence("\n"))])
        self.assertEqual(r.read("a.txt"), "2\n\n")

    def test_crlf_and_no_final_newline_are_kept(self):
        r = Repo({"w.txt": "one\r\ntwo\r\n", "n.txt": "x\ny"})
        r.plan([node(1, "w.txt", 1, fence("ONE\n")), node(2, "n.txt", 2, fence("Y\n"))])
        self.assertEqual(Path(r.root, "w.txt").read_bytes(), b"ONE\r\ntwo\r\n")
        self.assertEqual(r.read("n.txt"), "x\nY")

    def test_a_hook_or_workflow_path_is_taken_like_any_other(self):
        r = Repo({".github/workflows/x.yml": "a\n", "hooks/h.py": "a\n"})
        res = r.plan([node(1, ".github/workflows/x.yml", 1, fence("b\n")),
                      node(2, "hooks/h.py", 1, "🤖 [nit] [easy] rename")])
        self.assertEqual(r.read(".github/workflows/x.yml"), "b\n")
        self.assertEqual([t["status"] for t in res], ["fixed", "agent"])

    def test_a_path_that_could_forge_a_tool_grant_is_not_taken(self):
        evil = "x),Bash(*"
        r = Repo({evil: "1\n", "a b.txt": "1\n", "ok.txt": "1\n"})
        res = r.plan([node(1, evil, 1, "🤖 [nit] [easy] x"),
                      node(2, "a b.txt", 1, fence("2\n")),
                      node(3, "../ok.txt", 1, "🤖 [nit] [easy] x")])
        self.assertEqual([t["status"] for t in res], ["not-taken"] * 3)
        self.assertTrue(all("characters" in t["why"] for t in res))
        self.assertEqual(r.read("a b.txt"), "1\n")

    def test_a_symlink_is_not_taken(self):
        r = Repo({"hooks/y.py": "1\n", "docs/real.md": "1\n"})
        os.symlink("../hooks/y.py", Path(r.root, "docs/x"))
        os.symlink("../hooks", Path(r.root, "docs/d"))
        subprocess.run(["git", "-C", r.root, "add", "docs/x", "docs/d"], check=True)
        res = r.plan([node(1, "docs/x", 1, fence("evil\n")),
                      node(2, "docs/d/y.py", 1, "🤖 [nit] [easy] x")])
        self.assertEqual([t["status"] for t in res], ["not-taken", "not-taken"])
        self.assertEqual(r.read("hooks/y.py"), "1\n")

    def test_only_this_heads_open_unanswered_easy_bot_threads(self):
        r = Repo({"a.txt": "1\n"})
        res = r.plan([
            node(1, "a.txt", 1, "🤖 [nit] fix it"),                      # not [easy]
            node(2, "a.txt", 1, fence("x\n"), login="someone"),          # not the bot
            node(7, "a.txt", 1, fence("x\n"), kind="User"),              # a User named claude
            node(3, "a.txt", 1, fence("x\n"), commit="b" * 40),          # another head
            node(4, "a.txt", 1, fence("x\n"), total=2),                  # answered
            node(5, "a.txt", 1, fence("x\n"), resolved=True),            # resolved
            node(6, "a.txt", 1, "🤖 [should] [easy] say it in words"),   # taken, by the agent
        ])
        self.assertEqual([(t["thread_id"], t["status"]) for t in res], [("T6", "agent")])
        self.assertEqual(r.read("a.txt"), "1\n")

    def test_a_user_named_claude_is_not_the_bot(self):
        r = Repo({"src/x.py": "ok\n"})
        res = r.plan([node(1, "src/x.py", 1, fence("evil\n"), kind="User"),
                      node(2, "src/x.py", 1, fence("evil\n"), login="claude[bot]", kind="User")])
        self.assertEqual(res, [])
        self.assertEqual(r.read("src/x.py"), "ok\n")

    def test_a_head_the_fixer_committed_is_not_fixed_again(self):
        r = Repo({"a.txt": "1\n"})
        res = r.plan([node(1, "a.txt", 1, fence("A\n"))],
                     head_message="fix: apply 1 easy review finding(s)\n\n- u\n\nClaude-Fixer-Run: 42\n")
        self.assertEqual(res, [])
        self.assertEqual(r.read("a.txt"), "1\n")
        res = r.plan([node(1, "a.txt", 1, fence("A\n"))], head_message="a human commit\n")
        self.assertEqual(res[0]["status"], "fixed")

    def test_agent_lines_move_past_a_suggestion_above_them(self):
        r = Repo({"a.txt": "".join(f"{i}\n" for i in range(1, 13))})
        res = r.plan([node(1, "a.txt", 2, fence("two\ntwo-b\ntwo-c\n")),   # 1 line -> 3
                      node(2, "a.txt", 10, "🤖 [nit] [easy] say ten", start=9),
                      node(3, "a.txt", 3, "🤖 [nit] [easy] above? no, below"),
                      node(4, "a.txt", 2, "🤖 [nit] [easy] same line as the fence")])
        st = {t["thread_id"]: t for t in res}
        self.assertEqual((st["T2"]["start"], st["T2"]["end"]), (11, 12))
        self.assertEqual(r.read("a.txt").splitlines()[11], "10")
        self.assertEqual((st["T3"]["start"], st["T3"]["end"]), (5, 5))
        self.assertEqual(st["T4"]["status"], "left")

    def test_a_file_the_merge_moved_is_not_taken(self):
        r = Repo({"a.txt": "1\n", "b.txt": "1\n"})
        Path(r.tmp("moved.txt")).write_text("a.txt\n")
        Path(r.tmp("threads.json")).write_text(json.dumps(
            [node(1, "a.txt", 1, fence("A\n")), node(2, "b.txt", 1, "🤖 [nit] [easy] x")]))
        res = r.run("plan", "--threads", r.tmp("threads.json"), "--head", HEAD, "--root", r.root,
                    "--moved", r.tmp("moved.txt"))
        self.assertEqual([t["status"] for t in res], ["not-taken", "agent"])
        self.assertEqual(r.read("a.txt"), "1\n")

    def test_overlap_out_of_range_and_left_side_are_not_taken(self):
        r = Repo({"a.txt": "1\n2\n3\n"})
        res = r.plan([node(1, "a.txt", 2, fence("x\n")), node(2, "a.txt", 3, fence("y\n"), start=2),
                      node(3, "a.txt", 9, fence("z\n")), node(4, "a.txt", 1, fence("w\n"), side="LEFT")])
        st = {t["thread_id"]: t["status"] for t in res}
        self.assertEqual(st, {"T1": "not-taken", "T2": "fixed", "T3": "not-taken", "T4": "not-taken"})
        self.assertEqual(r.read("a.txt"), "1\ny\n")


class Gate(unittest.TestCase):
    def test_keeps_taken_paths_and_reverts_the_rest(self):
        r = Repo({"a.txt": "1\n", "b.txt": "1\n", "c.txt": "1\n", ".github/x.yml": "1\n"})
        r.plan([node(1, "a.txt", 1, fence("A\n")),
                node(2, "b.txt", 1, "🤖 [nit] [easy] capitalise"),
                node(3, "c.txt", 1, "🤖 [nit] [easy] capitalise")])
        # The agent fixes b, declines c, and wanders into two files no thread names.
        r.write("b.txt", "B\n")
        r.write(".github/x.yml", "evil\n")
        r.write("c.txt", "C\n")
        res = r.gate([{"thread_id": "T2", "done": True},
                      {"thread_id": "T3", "done": False, "why": "ambiguous"}])
        self.assertEqual(sorted(a["path"] for a in res["additions"]), ["a.txt", "b.txt"])
        self.assertEqual(base64.b64decode(res["additions"][0]["contents"]).decode(), "A\n")
        self.assertEqual(r.read(".github/x.yml"), "1\n")
        self.assertEqual(r.read("c.txt"), "1\n")
        st = {t["thread_id"]: (t["status"], t.get("why")) for t in res["threads"]}
        self.assertEqual(st["T1"][0], "fixed")
        self.assertEqual(st["T2"][0], "fixed")
        self.assertEqual(st["T3"], ("left", "ambiguous"))

    def test_a_file_with_an_unfinished_agent_thread_is_reverted_whole(self):
        r = Repo({"a.py": "1\n2\n3\n", "b.py": "1\n2\n3\n"})
        r.plan([node(1, "a.py", 1, "🤖 [nit] [easy] t1"), node(2, "a.py", 3, "🤖 [nit] [easy] t2"),
                node(3, "b.py", 1, fence("ONE\n")), node(4, "b.py", 3, "🤖 [nit] [easy] t4")])
        # a.py: half of T1, all of T2; T1 declined. b.py: the agent declines
        # T4 without touching it, so the suggestion stays.
        r.write("a.py", "half\n2\nTHREE\n")
        res = r.gate([{"thread_id": "T1", "done": False, "why": "unclear"},
                      {"thread_id": "T2", "done": True},
                      {"thread_id": "T4", "done": False, "why": "unclear"}])
        self.assertEqual([a["path"] for a in res["additions"]], ["b.py"])
        self.assertEqual(r.read("a.py"), "1\n2\n3\n")
        st = {t["thread_id"]: t["status"] for t in res["threads"]}
        self.assertEqual(st, {"T1": "left", "T2": "left", "T3": "fixed", "T4": "left"})

    def test_an_undone_agent_edit_keeps_the_suggestion_on_the_same_file(self):
        r = Repo({"b.py": "1\n2\n3\n4\n"})
        r.plan([node(1, "b.py", 1, fence("ONE\n")), node(2, "b.py", 3, "🤖 [nit] [easy] t2"),
                node(3, "b.py", 4, "🤖 [nit] [easy] t3")])
        r.write("b.py", "ONE\n2\nhalf\nFOUR\n")
        res = r.gate([{"thread_id": "T3", "done": True}])  # T2 never answered
        self.assertEqual(r.read("b.py"), "ONE\n2\n3\n4\n")
        self.assertEqual([base64.b64decode(a["contents"]).decode() for a in res["additions"]],
                         ["ONE\n2\n3\n4\n"])
        self.assertEqual([t["status"] for t in res["threads"]], ["fixed", "left", "left"])

    def test_an_edit_through_a_symlink_is_reverted_at_commit(self):
        r = Repo({"docs/a.md": "1\n", "hooks/y.py": "1\n"})
        os.symlink("../hooks/y.py", Path(r.root, "docs/link"))
        subprocess.run(["git", "-C", r.root, "add", "docs/link"], check=True)
        subprocess.run(["git", "-C", r.root, "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "link"], check=True)
        r.plan([node(1, "docs/a.md", 1, "🤖 [nit] [easy] x")])
        # The agent writes through the link: the link's target is reverted.
        r.write("docs/link", "evil\n")
        res = r.gate([{"thread_id": "T1", "done": True}])
        self.assertEqual(res["additions"], [])
        self.assertEqual(r.read("hooks/y.py"), "1\n")

    def test_a_file_the_base_moved_is_reverted_not_committed(self):
        r = Repo({"a.txt": "1\n", "b.txt": "1\n"})
        r.plan([node(1, "a.txt", 1, fence("A\n")), node(2, "b.txt", 1, fence("B\n"))])
        Path(r.tmp("moved.txt")).write_text("a.txt\n")
        res = r.run("gate", "--plan", r.tmp("plan.json"), "--agent", r.tmp("nope.json"),
                    "--root", r.root, "--moved", r.tmp("moved.txt"))
        self.assertEqual([a["path"] for a in res["additions"]], ["b.txt"])
        self.assertEqual(r.read("a.txt"), "1\n")
        st = {t["thread_id"]: t["status"] for t in res["threads"]}
        self.assertEqual(st, {"T1": "not-taken", "T2": "fixed"})

    def test_missing_agent_file_leaves_agent_threads(self):
        r = Repo({"a.txt": "1\n"})
        r.plan([node(1, "a.txt", 1, "🤖 [nit] [easy] x")])
        res = r.run("gate", "--plan", r.tmp("plan.json"), "--agent", r.tmp("nope.json"), "--root", r.root)
        self.assertEqual(res["threads"][0]["status"], "left")
        self.assertEqual(res["additions"], [])


class Workflow(unittest.TestCase):
    def test_the_gate_runs_once_before_the_update(self):
        # gate() rewrites the tree it reads, so a second run reads a different
        # one: the update step runs it, the commit step only when that did not.
        wf = (HERE.parents[1] / "workflows" / "claude-review.yml").read_text()
        update = wf.index("Gate the fixes and update the branch")
        commit = wf.index("name: Commit the fixes")
        self.assertEqual(wf.count('"$SCRIPT" gate'), 2)
        self.assertLess(update, wf.index('"$SCRIPT" gate'))
        self.assertLess(wf.index('"$SCRIPT" gate'), wf.index("/update-branch"))
        self.assertIn('[ -f "$FIXER_DIR/gate.json" ] || python3 "$SCRIPT" gate', wf[commit:])


def g(root, *args):
    return subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                          capture_output=True, text=True)


def conflicted(base, ours, theirs):
    """A repo whose HEAD is `ours` with `theirs` (the base branch) merged in
    and not committed: files map path -> text, None for absent."""
    r = Repo({p: t for p, t in base.items() if t is not None})
    g(r.root, "branch", "-M", "head")
    g(r.root, "checkout", "-qb", "main")
    for side, branch in ((theirs, "main"), (ours, "head")):
        g(r.root, "checkout", "-q", branch)
        for p, t in side.items():
            f = Path(r.root, p)
            if t is None:
                f.unlink(missing_ok=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(t.encode() if isinstance(t, str) else t)
        g(r.root, "add", "-A")
        g(r.root, "commit", "-qm", branch)
    r.head = g(r.root, "rev-parse", "HEAD").stdout.strip()
    r.base = g(r.root, "rev-parse", "main").stdout.strip()
    g(r.root, "merge", "--no-ff", "--no-commit", "main")
    return r


class Conflicts(unittest.TestCase):
    def test_a_small_text_conflict_goes_to_the_model(self):
        r = conflicted({"a.txt": "1\n2\n"}, {"a.txt": "1\nours\n"}, {"a.txt": "1\ntheirs\n"})
        res = r.run("conflicts", "--root", r.root)
        self.assertEqual((res["status"], res["files"], res["hunks"]), ("resolve", ["a.txt"], {"a.txt": 1}))

    def test_a_clean_merge_is_clean(self):
        r = conflicted({"a.txt": "1\n", "b.txt": "1\n"}, {"a.txt": "2\n"}, {"b.txt": "2\n"})
        self.assertEqual(r.run("conflicts", "--root", r.root)["status"], "clean")

    def test_more_than_the_limit_escalates(self):
        n = 6
        r = conflicted({f"f{i}": "0\n" for i in range(n)}, {f"f{i}": "1\n" for i in range(n)},
                       {f"f{i}": "2\n" for i in range(n)})
        res = r.run("conflicts", "--root", r.root)
        self.assertEqual(res["status"], "escalate")
        self.assertIn("more than 5 conflicted files (6)", res["rule"])
        self.assertEqual(r.run("conflicts", "--root", r.root, "--max-files", "6")["status"], "resolve")

    def test_lockfile_generated_binary_and_delete_escalate(self):
        base = {"package-lock.json": "0\n", "go.sum": "0\n", "vendor/x.go": "0\n",
                "img.bin": b"\0a", "gone.txt": "0\n", "ok.txt": "0\n"}
        ours = {"package-lock.json": "1\n", "go.sum": "1\n", "vendor/x.go": "1\n",
                "img.bin": b"\0b", "gone.txt": None, "ok.txt": "1\n"}
        theirs = {"package-lock.json": "2\n", "go.sum": "2\n", "vendor/x.go": "2\n",
                  "img.bin": b"\0c", "gone.txt": "2\n", "ok.txt": "2\n"}
        r = conflicted(base, ours, theirs)
        res = r.run("conflicts", "--root", r.root, "--max-files", "9")
        self.assertEqual(res["status"], "escalate")
        p = res["problems"]
        self.assertIn("lockfile", p["package-lock.json"])
        self.assertIn("lockfile", p["go.sum"])
        self.assertIn("generated", p["vendor/x.go"])
        self.assertIn("binary", p["img.bin"])
        self.assertIn("deleted", p["gone.txt"])
        self.assertNotIn("ok.txt", p)

    def test_a_file_with_marker_lines_of_its_own_escalates(self):
        r = conflicted({"t.py": "x\n<<<<<<< sample\n"}, {"t.py": "ours\n<<<<<<< sample\n"},
                       {"t.py": "theirs\n<<<<<<< sample\n"})
        res = r.run("conflicts", "--root", r.root)
        self.assertIn("marker lines of its own", res["problems"]["t.py"])

    def test_a_path_the_action_resets_escalates(self):
        r = conflicted({"CLAUDE.md": "0\n", ".claude/s.json": "0\n"}, {"CLAUDE.md": "1\n", ".claude/s.json": "1\n"},
                       {"CLAUDE.md": "2\n", ".claude/s.json": "2\n"})
        p = r.run("conflicts", "--root", r.root)["problems"]
        self.assertEqual(set(p), {"CLAUDE.md", ".claude/s.json"})
        self.assertIn("claude-code-action resets", p["CLAUDE.md"])

    def test_a_linguist_generated_attribute_escalates(self):
        r = conflicted({"api.ts": "0\n", ".gitattributes": "api.ts linguist-generated\n"},
                       {"api.ts": "1\n"}, {"api.ts": "2\n"})
        self.assertIn("generated", r.run("conflicts", "--root", r.root)["problems"]["api.ts"])


class Resolved(unittest.TestCase):
    def setUp(self):
        self.r = conflicted({"a.txt": "1\n2\n", "b.txt": "1\n"}, {"a.txt": "1\nours\n"},
                            {"a.txt": "1\ntheirs\n"})
        Path(self.r.tmp("conflicts.json")).write_text(json.dumps(self.r.run("conflicts", "--root", self.r.root)))

    def check(self, answers="a.txt: kept both lines @someone | piped\n"):
        Path(self.r.tmp("answers.txt")).write_text(answers)
        return self.r.run("resolved", "--conflicts", self.r.tmp("conflicts.json"),
                          "--answers", self.r.tmp("answers.txt"), "--root", self.r.root)

    def test_a_clean_resolution_passes_with_its_note(self):
        self.r.write("a.txt", "1\nours\ntheirs\n")
        res = self.check()
        self.assertTrue(res["ok"])
        self.assertEqual(res["notes"]["a.txt"], "kept both lines @\u200bsomeone \\| piped")

    def test_markers_left_escalate(self):
        res = self.check()
        self.assertFalse(res["ok"])
        self.assertEqual(res["rule"], "the model left a conflict unresolved")

    def test_an_edit_outside_the_conflict_goes_back_to_the_merge(self):
        self.r.write("a.txt", "1\nours\ntheirs\n")
        self.r.write("b.txt", "edited\n")
        res = self.check()
        self.assertTrue(res["ok"])
        self.assertEqual(res["reverted"], ["b.txt"])
        self.assertEqual(self.r.read("b.txt"), "1\n")

    def test_a_deleted_conflicted_file_fails(self):
        os.unlink(Path(self.r.root, "a.txt"))
        self.assertEqual(self.check()["problems"], {"a.txt": "the model deleted it"})


class MergeTree(unittest.TestCase):
    def test_entries_rebuild_the_merged_tree_on_the_base(self):
        r = conflicted({"a.txt": "1\n2\n", "keep.txt": "0\n", "del.txt": "0\n"},
                       {"a.txt": "1\nours\n", "del.txt": None, "new.txt": "n\n"},
                       {"a.txt": "1\ntheirs\n", "keep.txt": "base\n"})
        r.write("a.txt", "1\nours\ntheirs\n")
        g(r.root, "add", "a.txt")
        g(r.root, "commit", "-qm", "merge")
        res = r.run("merge-tree", "--root", r.root, "--base", r.base, "--head", r.head)
        by = {e["path"]: e for e in res["entries"]}
        self.assertEqual(set(by), {"a.txt", "del.txt", "new.txt"})
        self.assertIsNone(by["del.txt"]["sha"])
        # Only the resolved blob is new to the server; new.txt is the head's.
        self.assertEqual(res["upload"], ["a.txt"])
        # Replaying the entries on the base's tree gives the merged tree.
        idx = r.tmp("idx")
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        subprocess.run(["git", "-C", r.root, "read-tree", res["base_tree"]], env=env, check=True)
        for e in res["entries"]:
            if e["sha"] is None:
                subprocess.run(["git", "-C", r.root, "update-index", "--force-remove", e["path"]], env=env, check=True)
            else:
                subprocess.run(["git", "-C", r.root, "update-index", "--add", "--cacheinfo",
                                f"{e['mode']},{e['sha']},{e['path']}"], env=env, check=True)
        tree = subprocess.run(["git", "-C", r.root, "write-tree"], env=env, capture_output=True,
                              text=True, check=True).stdout.strip()
        self.assertEqual(tree, res["tree"])


class MergeComment(unittest.TestCase):
    def comment(self, state, **kw):
        d = tempfile.mkdtemp()
        Path(d, "s.json").write_text(json.dumps(state))
        args = ["python3", str(SCRIPT), "merge-comment", "--state", f"{d}/s.json", "--base", "main",
                "--sign", SIGN, "--marker", "<!-- m -->", "--out", f"{d}/c.md"]
        for k, v in kw.items():
            args += [f"--{k}", v]
        subprocess.run(args, check=True)
        return Path(d, "c.md").read_text()

    def test_escalation_names_the_files_and_the_rule(self):
        body = self.comment({"status": "escalate", "files": ["yarn.lock", "a.txt"],
                             "rule": "a conflicted file the fixer leaves to the author",
                             "problems": {"yarn.lock": "a lockfile, regenerated rather than merged"}})
        self.assertIn("did not resolve", body)
        self.assertIn("| `yarn.lock` | a lockfile", body)
        self.assertIn("| `a.txt` | conflicted |", body)
        self.assertTrue(body.rstrip().endswith(SIGN))
        self.assertIn("<!-- m -->", body)

    def test_success_and_push_failure(self):
        state = {"ok": True, "files": ["a.txt"], "notes": {"a.txt": "kept both"}}
        self.assertIn("in abc1234 and resolved", self.comment(state, sha="abc1234"))
        body = self.comment(state, failure="the branch moved")
        self.assertIn("could not push the merge: the branch moved", body)
        self.assertIn("| `a.txt` | kept both |", body)


class ConflictWorkflow(unittest.TestCase):
    def test_nothing_is_pushed_before_both_agents_and_the_token(self):
        wf = (HERE.parents[1] / "workflows" / "claude-review.yml").read_text()
        order = [wf.index(x) for x in ("name: Merge the base in", "id: conflict-agent",
                                         "name: Check the resolution", "id: plan", "id: agent\n",
                                         "id: token", "name: Push the merge",
                                         "name: Gate the fixes and update", "name: Commit the fixes")]
        self.assertEqual(order, sorted(order))
        # The merge is never a rebase, and never forced over a moved branch.
        fixer = wf[wf.index("  fixer:\n"):]
        self.assertNotIn("rebase", fixer.replace("never a rebase", ""))
        self.assertIn("-F force=false", fixer)


class Body(unittest.TestCase):
    BODY = "Adds a thing.\r\n\r\nHead: 1111111\r\n\r\n## Pencil: none\r\n"

    def test_the_old_text_is_replaced_and_no_file_changes(self):
        r = Repo({"a.txt": "1\n"})
        plan = r.plan([node(1, "a.txt", 1, body_fix("Head: 1111111", "Head: 2222222")),
                       node(2, "a.txt", 1, body_fix("## Pencil: none", "## Pencil:\n- kept the old name"))],
                      pr_body=self.BODY)
        self.assertEqual([t["status"] for t in plan], ["body", "body"])
        self.assertEqual(r.new_body(), "Adds a thing.\n\nHead: 2222222\n\n## Pencil:\n- kept the old name\n")
        res = r.gate([])
        self.assertEqual(res["additions"], [])
        rep = r.run("report", "--gate", r.tmp("gate.json"), "--sha", "", "--failure", "nothing to commit", "--sign", SIGN)
        self.assertEqual(rep["counts"], {"fixed": 2, "not-taken": 0, "left": 0})
        self.assertIn("pull request's body", rep["replies"][0]["body"])

    def test_text_absent_repeated_or_unreadable_is_left_and_the_body_stays(self):
        r = Repo({"a.txt": "1\n"})
        plan = r.plan([node(1, "a.txt", 1, body_fix("Head: 9999999", "Head: 2222222")),
                       node(2, "a.txt", 1, body_fix(" ", "x")),
                       node(3, "a.txt", 1, body_fix("x", "b")),
                       node(4, "a.txt", 1, body_fix("Head: 111", "Head: 222")),
                       node(5, "a.txt", 1, body_fix("## Pencil:", "## Pencil:\n- x"))],
                      pr_body=self.BODY + "x\nx\n")
        self.assertEqual([t["status"] for t in plan], ["left", "not-taken", "left", "left", "left"])
        self.assertIn("times, not once", plan[2]["why"])
        self.assertIn("whole lines", plan[3]["why"])
        self.assertIsNone(r.new_body())
        plan = r.plan([node(1, "a.txt", 1, body_fix("Head: 1111111", "Head: 2222222"))])
        self.assertEqual(plan[0]["status"], "left")

    def test_half_a_pair_or_a_suggestion_beside_it_is_not_taken(self):
        r = Repo({"a.txt": "1\n"})
        half = "🤖 [nit] [easy] x\n```body-new\nHead: 2\n```\n"
        both = body_fix("Head: 1111111", "Head: 2") + "\n```suggestion\nA\n```\n"
        plan = r.plan([node(1, "a.txt", 1, half), node(2, "a.txt", 1, both)], pr_body=self.BODY)
        self.assertEqual([t["status"] for t in plan], ["not-taken", "not-taken"])
        self.assertEqual(r.read("a.txt"), "1\n")
        self.assertIsNone(r.new_body())

    def test_a_body_that_was_not_saved_is_left(self):
        r = Repo({"a.txt": "1\n"})
        r.plan([node(1, "a.txt", 1, body_fix("Head: 1111111", "Head: 2222222"))], pr_body=self.BODY)
        r.gate([])
        rep = r.run("report", "--gate", r.tmp("gate.json"), "--body-failure", "GitHub refused the edit", "--sign", SIGN)
        self.assertEqual(rep["counts"], {"fixed": 0, "not-taken": 0, "left": 1})
        self.assertIn("GitHub refused the edit", rep["replies"][0]["body"])
        self.assertFalse(rep["replies"][0]["resolve"])


class Report(unittest.TestCase):
    def test_every_thread_gets_one_reply_and_no_commit_means_left(self):
        r = Repo({"a.txt": "1\n", ".github/x.yml": "1\n"})
        r.plan([node(1, "a.txt", 1, fence("A\n")), node(2, "a.txt", 1, fence("B\n"), side="LEFT")])
        r.gate([])
        res = r.run("report", "--gate", r.tmp("gate.json"), "--sha", "", "--failure",
                    "the App token cannot write contents", "--sign", SIGN)
        self.assertEqual(res["counts"], {"fixed": 0, "not-taken": 1, "left": 1})
        self.assertEqual([x["comment_id"] for x in res["replies"]], [101, 102])
        self.assertTrue(all(x["body"].endswith(SIGN) for x in res["replies"]))
        res = r.run("report", "--gate", r.tmp("gate.json"), "--sha", "abc1234", "--sign", SIGN)
        self.assertEqual(res["counts"]["fixed"], 1)
        self.assertIn("abc1234", res["replies"][0]["body"])
        # Only a fixed thread is resolved; a declined one stays open.
        self.assertEqual([(x["thread_id"], x["resolve"]) for x in res["replies"]], [("T1", True), ("T2", False)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
