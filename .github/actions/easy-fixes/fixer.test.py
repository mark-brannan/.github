#!/usr/bin/env python3
# Tests for the fixer job's script. Run:
# python3 .github/actions/easy-fixes/fixer.test.py
#
# What matters: only this head's open, unanswered claude[bot] [easy] threads
# are taken; a suggestion fence replaces exactly its lines; a risky path is
# refused before any edit and reverted at the gate; an edit no taken thread
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
RISKY = ".github/* action.yml */action.yml hooks/*"
SIGN = "🤖 claude-fixer · run 1"


def node(i, path, line, body, start=None, login="claude", commit=HEAD, total=1,
         resolved=False, side="RIGHT"):
    return {"id": f"T{i}", "isResolved": resolved, "path": path, "diffSide": side,
            "originalLine": line, "originalStartLine": start,
            "comments": {"totalCount": total, "nodes": [{
                "databaseId": 100 + i, "url": f"https://x/{i}", "author": {"login": login},
                "body": body, "originalCommit": {"oid": commit}}]}}


def fence(text):
    return f"🤖 [nit] [easy] fix it\n```suggestion\n{text}```\n🤖 claude-sonnet-5-5 · medium · 1a2b3c4d"


class Repo:
    def __init__(self, files):
        self.root = tempfile.mkdtemp()
        for p, t in files.items():
            f = Path(self.root, p)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(t.encode())
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
        subprocess.run(["python3", str(SCRIPT), *args, "--out", str(out)], check=True,
                       env=dict(os.environ, RISKY=RISKY))
        return json.loads(out.read_text())

    def tmp(self, name):
        return str(Path(self.root, "..", name).resolve())

    def plan(self, nodes):
        Path(self.tmp("threads.json")).write_text(json.dumps(nodes))
        res = self.run("plan", "--threads", self.tmp("threads.json"), "--head", HEAD, "--root", self.root)
        Path(self.tmp("plan.json")).write_text(json.dumps(res))
        return res

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

    def test_risky_path_is_refused_before_any_edit(self):
        r = Repo({".github/workflows/x.yml": "a\n", "hooks/h.py": "a\n"})
        res = r.plan([node(1, ".github/workflows/x.yml", 1, fence("b\n")),
                      node(2, "hooks/h.py", 1, "🤖 [nit] [easy] rename")])
        self.assertEqual(r.read(".github/workflows/x.yml"), "a\n")
        self.assertEqual([t["status"] for t in res], ["not-taken", "not-taken"])

    def test_only_this_heads_open_unanswered_easy_bot_threads(self):
        r = Repo({"a.txt": "1\n"})
        res = r.plan([
            node(1, "a.txt", 1, "🤖 [nit] fix it"),                      # not [easy]
            node(2, "a.txt", 1, fence("x\n"), login="someone"),          # not the bot
            node(3, "a.txt", 1, fence("x\n"), commit="b" * 40),          # another head
            node(4, "a.txt", 1, fence("x\n"), total=2),                  # answered
            node(5, "a.txt", 1, fence("x\n"), resolved=True),            # resolved
            node(6, "a.txt", 1, "🤖 [should] [easy] say it in words"),   # taken, by the agent
        ])
        self.assertEqual([(t["thread_id"], t["status"]) for t in res], [("T6", "agent")])
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
        # The agent fixes b, declines c, and wanders into a risky path and an untaken one.
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

    def test_missing_agent_file_leaves_agent_threads(self):
        r = Repo({"a.txt": "1\n"})
        r.plan([node(1, "a.txt", 1, "🤖 [nit] [easy] x")])
        res = r.run("gate", "--plan", r.tmp("plan.json"), "--agent", r.tmp("nope.json"), "--root", r.root)
        self.assertEqual(res["threads"][0]["status"], "left")
        self.assertEqual(res["additions"], [])


class Report(unittest.TestCase):
    def test_every_thread_gets_one_reply_and_no_commit_means_left(self):
        r = Repo({"a.txt": "1\n", ".github/x.yml": "1\n"})
        r.plan([node(1, "a.txt", 1, fence("A\n")), node(2, ".github/x.yml", 1, fence("B\n"))])
        r.gate([])
        res = r.run("report", "--gate", r.tmp("gate.json"), "--sha", "", "--failure",
                    "the App token cannot write contents", "--sign", SIGN)
        self.assertEqual(res["counts"], {"fixed": 0, "not-taken": 1, "left": 1})
        self.assertEqual([x["comment_id"] for x in res["replies"]], [101, 102])
        self.assertTrue(all(x["body"].endswith(SIGN) for x in res["replies"]))
        res = r.run("report", "--gate", r.tmp("gate.json"), "--sha", "abc1234", "--sign", SIGN)
        self.assertEqual(res["counts"]["fixed"], 1)
        self.assertIn("abc1234", res["replies"][0]["body"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
