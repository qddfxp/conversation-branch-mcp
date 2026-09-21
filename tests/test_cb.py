#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standard-library regression tests for Conversation Branch.

覆盖两类内容：
1. cb.py 的状态迁移不变量（归档、提升、回滚、体检、导出、结论回流）。
2. cb_mcp.py 的 stdio 协议契约：换行分隔 JSON（不是 Content-Length 帧）+ ping + 版本协商。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CB = HERE / "scripts" / "cb.py"
MCP = HERE / "scripts" / "cb_mcp.py"


class ConversationBranchTests(unittest.TestCase):
    def run_cb(self, root, *args, check=True):
        completed = subprocess.run(
            [sys.executable, str(CB), args[0], str(root), *args[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if check and completed.returncode:
            self.fail(f"cb failed: {completed.stdout}\n{completed.stderr}")
        return completed

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cb-test-")
        self.root = Path(self.tmp.name) / "task"
        self.run_cb(self.root, "init")
        self.store = self.root / ".branches"
        (self.store / "main" / "inputs" / "sample.txt").write_text("same input\n", encoding="utf-8")
        (self.store / "main" / "PROMPT.md").write_text("v1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return json.loads((self.store / "state.json").read_text(encoding="utf-8"))

    def branch(self, name, prompt=None):
        """建分支；prompt 给定时同时改写该分支的 PROMPT.md（模拟实验产出）。"""
        self.run_cb(self.root, "branch", name)
        if prompt is not None:
            (self.store / "branches" / name / "PROMPT.md").write_text(prompt, encoding="utf-8")

    def verdict_file(self, conclusion="promote", extra=""):
        path = Path(self.tmp.name) / "verdict.txt"
        path.write_text("conclusion: {0}\n{1}".format(conclusion, extra), encoding="utf-8")
        return path

    def archives(self):
        return sorted(p.name for p in (self.store / "archive").iterdir())

    # ---------------------------------------------------------------- 状态迁移

    def test_branch_copies_inputs_by_default_and_records_base_version(self):
        self.run_cb(self.root, "branch", "exp", "--purpose", "test")
        state = self.state()
        self.assertEqual(state["branches"]["exp"]["base_version"], 1)
        self.assertTrue((self.store / "branches" / "exp" / "inputs" / "sample.txt").exists())

    def test_stale_branch_cannot_promote_over_new_main(self):
        self.run_cb(self.root, "branch", "old")
        self.run_cb(self.root, "checkout", "main")
        self.run_cb(self.root, "branch", "new")
        (self.store / "branches" / "new" / "PROMPT.md").write_text("v2\n", encoding="utf-8")
        self.run_cb(self.root, "promote", "new", "--note", "test")
        stale = self.run_cb(self.root, "promote", "old", check=False)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("旧主线", stale.stderr)

    def test_rollback_increments_version_and_restores_archived_content(self):
        self.run_cb(self.root, "branch", "new")
        (self.store / "branches" / "new" / "PROMPT.md").write_text("v2\n", encoding="utf-8")
        self.run_cb(self.root, "promote", "new", "--note", "test")
        self.run_cb(self.root, "rollback", "1", "--note", "test rollback")
        state = self.state()
        self.assertEqual(state["main_version"], 3)
        self.assertEqual((self.store / "main" / "PROMPT.md").read_text(encoding="utf-8"), "v1\n")
        self.assertTrue(any(p.startswith("main-v2-") and "before-rollback" in p for p in self.archives()))

    def test_rollback_accepts_archive_directory_name_and_prefers_before_rollback_snapshot(self):
        self.branch("new", "v2\n")
        self.run_cb(self.root, "promote", "new", "--note", "test")
        target = next(p for p in self.archives() if p.startswith("main-v1-"))
        self.run_cb(self.root, "rollback", target, "--note", "回滚到 v1")
        self.assertEqual((self.store / "main" / "PROMPT.md").read_text(encoding="utf-8"), "v1\n")
        state = self.state()
        self.assertEqual(state["main_version"], 3)
        # 回滚前的主线快照必须留档，否则无处可退
        self.assertTrue(any("before-rollback" in p for p in self.archives()))

    def test_promote_requires_note_when_prompt_unchanged(self):
        self.run_cb(self.root, "branch", "same")
        refused = self.run_cb(self.root, "promote", "same", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--note", refused.stderr)
        # 拒绝提升时必须没有任何状态写入：分支还在 testing，主线版本不动
        self.assertEqual(self.state()["branches"]["same"]["status"], "testing")
        self.assertEqual(self.state()["main_version"], 1)

    def test_promote_consumes_branch_and_advances_main(self):
        self.branch("winner", "v2\n")
        self.run_cb(self.root, "promote", "winner", "--note", "更好")
        state = self.state()
        self.assertNotIn("winner", state["branches"])
        self.assertEqual(state["main_version"], 2)
        self.assertEqual((self.store / "main" / "PROMPT.md").read_text(encoding="utf-8"), "v2\n")

    def test_discard_archives_branch_and_moves_head_back_to_main(self):
        self.run_cb(self.root, "branch", "a")
        self.run_cb(self.root, "branch", "b")
        self.run_cb(self.root, "checkout", "b")
        self.run_cb(self.root, "discard", "a", "b")
        state = self.state()
        self.assertEqual(state["head"], "main")
        for name in ("a", "b"):
            self.assertEqual(state["branches"][name]["status"], "archived")
            self.assertFalse((self.store / "branches" / name).exists())
            self.assertTrue(any(p.startswith("branch-{0}-".format(name)) for p in self.archives()))

    def test_discard_keep_flag_keeps_only_the_winner(self):
        for name in ("a", "b", "c"):
            self.run_cb(self.root, "branch", name)
        self.run_cb(self.root, "discard", "--keep", "b")
        state = self.state()
        self.assertEqual(state["branches"]["b"]["status"], "testing")
        self.assertEqual(state["branches"]["a"]["status"], "archived")
        self.assertEqual(state["branches"]["c"]["status"], "archived")
        self.assertTrue((self.store / "branches" / "b").exists())

    def test_discard_purge_removes_directory_without_archiving(self):
        self.run_cb(self.root, "branch", "junk")
        self.run_cb(self.root, "discard", "junk", "--purge")
        self.assertFalse((self.store / "branches" / "junk").exists())
        self.assertFalse(any("junk" in p for p in self.archives()))

    def test_discard_argument_errors_are_rejected(self):
        for name in ("a", "b"):
            self.run_cb(self.root, "branch", name)
        both = self.run_cb(self.root, "discard", "a", "--keep", "b", check=False)
        self.assertNotEqual(both.returncode, 0)
        nothing = self.run_cb(self.root, "discard", check=False)
        self.assertNotEqual(nothing.returncode, 0)
        missing = self.run_cb(self.root, "discard", "nosuch", check=False)
        self.assertNotEqual(missing.returncode, 0)
        # 三次失败都不该改动状态
        state = self.state()
        self.assertEqual(state["branches"]["a"]["status"], "testing")
        self.assertEqual(state["branches"]["b"]["status"], "testing")

    def test_illegal_branch_names_and_duplicates_are_rejected(self):
        self.run_cb(self.root, "branch", "exp")
        for bad in ("Main", "a/b", "A", "", "-lead", "x" * 41):
            failed = self.run_cb(self.root, "branch", bad, check=False)
            self.assertNotEqual(failed.returncode, 0, bad)
        duplicate = self.run_cb(self.root, "branch", "exp", check=False)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("exp", duplicate.stderr)
        self.assertNotIn("exp/", self.state()["branches"])

    # ------------------------------------------------------------------- 结论

    def test_verdict_requires_conclusion_line_and_writes_verdict_file(self):
        self.run_cb(self.root, "branch", "a")
        missing = self.run_cb(self.root, "verdict", "a", "--from", str(self.verdict_file(conclusion="maybe")), check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse((self.store / "branches" / "a" / "VERDICT.md").exists())
        self.assertNotIn("verdict", self.state()["branches"]["a"])

        self.run_cb(self.root, "verdict", "a", "--from", str(self.verdict_file(extra="dimensions: 速度")) )
        verdict = (self.store / "branches" / "a" / "VERDICT.md").read_text(encoding="utf-8")
        self.assertIn("promote", verdict)
        self.assertEqual(self.state()["branches"]["a"]["verdict"]["conclusion"], "promote")
        # 只回流结论，不得替用户执行 promote/discard
        self.assertEqual(self.state()["branches"]["a"]["status"], "testing")
        self.assertEqual(self.state()["main_version"], 1)

    def test_verdict_rejects_missing_file(self):
        self.run_cb(self.root, "branch", "a")
        failed = self.run_cb(self.root, "verdict", "a", "--from", str(Path(self.tmp.name) / "nope.txt"), check=False)
        self.assertNotEqual(failed.returncode, 0)

    # ------------------------------------------------------------------- 体检

    def test_check_is_read_only_and_reports_structural_problems(self):
        self.run_cb(self.root, "branch", "a")
        before = (self.store / "state.json").read_bytes()
        clean = self.run_cb(self.root, "check", check=False)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(before, (self.store / "state.json").read_bytes())

        # 孤儿目录：branches/ 下有目录但 state.json 里没有记录
        (self.store / "branches" / "orphan").mkdir()
        broken = self.run_cb(self.root, "check", check=False)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("orphan", broken.stdout + broken.stderr)
        self.assertEqual(before, (self.store / "state.json").read_bytes())

        # 缺失 PROMPT.md 也必须被点名
        (self.store / "branches" / "a" / "PROMPT.md").unlink()
        again = self.run_cb(self.root, "check", check=False)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(before, (self.store / "state.json").read_bytes())

    # --------------------------------------------------------------- 导出/日志

    def test_export_requires_existing_target_and_writes_handoff_package(self):
        self.run_cb(self.root, "branch", "a")
        self.run_cb(self.root, "verdict", "a", "--from", str(self.verdict_file()))
        missing_target = self.run_cb(self.root, "export", "a", "--to", str(Path(self.tmp.name) / "pkg"), check=False)
        self.assertNotEqual(missing_target.returncode, 0)

        target = Path(self.tmp.name) / "pkg"
        target.mkdir()
        self.run_cb(self.root, "export", "a", "--to", str(target))
        package = next(p for p in target.iterdir() if p.is_dir())
        for name in ("HANDOFF.md", "PROMPT.md", "VERDICT.md", "DIFF.md", "NOTES.md"):
            self.assertTrue((package / name).exists(), name)
        self.assertTrue((package / "inputs").is_dir())
        handoff = (package / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("a", handoff)
        # 导出是只读操作，不该动主线版本
        self.assertEqual(self.state()["main_version"], 1)

    def test_log_limit_and_diff_output(self):
        self.branch("a", "v2\n")
        log = self.run_cb(self.root, "log", "--limit", "1")
        self.assertTrue(log.stdout.strip())
        self.assertEqual(self.run_cb(self.root, "log", "--limit", "1").returncode, 0)

        diff = self.run_cb(self.root, "diff", "a")
        self.assertIn("-v1", diff.stdout)
        self.assertIn("+v2", diff.stdout)
        missing = self.run_cb(self.root, "diff", "nosuch", check=False)
        self.assertNotEqual(missing.returncode, 0)

    # ------------------------------------------------------- 并发与健壮性

    def test_state_json_with_bom_is_loaded_and_rewritten_clean(self):
        state_path = self.store / "state.json"
        state_path.write_bytes(b"\xef\xbb\xbf" + state_path.read_bytes())
        self.run_cb(self.root, "branch", "bomcheck")
        self.assertFalse(state_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn("bomcheck", self.state()["branches"])

    def test_stale_lock_is_broken_but_fresh_lock_blocks(self):
        lock = self.store / "state.lock"
        lock.write_text("", encoding="utf-8")
        stale = time.time() - 60
        os.utime(lock, (stale, stale))
        # 陈旧锁必须被强拆，命令正常完成，锁用完即释
        self.run_cb(self.root, "status")
        self.assertFalse(lock.exists())

        # 新鲜锁代表真实并发：默认等 10 秒，这里用环境变量压到 0 秒验证拒绝路径
        lock.write_text("", encoding="utf-8")
        env = dict(os.environ, CB_LOCK_WAIT_SECONDS="0")
        blocked = subprocess.run(
            [sys.executable, str(CB), "status", str(self.root)],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("等待工作区锁", blocked.stderr)
        lock.unlink()

    def test_mcp_check_reports_findings_as_success_not_error(self):
        self.run_cb(self.root, "branch", "a")
        (self.store / "branches" / "orphan").mkdir()
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cb_check", "arguments": {"root": str(self.root)}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cb_check", "arguments": {"root": str(Path(self.tmp.name) / "nope")}}},
        )
        # 体检发现结构问题（exit 1）是有效结果，不能被当成工具失败
        self.assertFalse(replies[1]["result"]["isError"], replies[1])
        self.assertIn("orphan", replies[1]["result"]["content"][0]["text"])
        # 但真的打不开工作区仍然要 isError
        self.assertTrue(replies[2]["result"]["isError"], replies[2])

    def test_concurrent_writers_do_not_lose_updates(self):
        """5 个进程同时建分支：没有 state.lock 时会丢更新，加锁后必须 5 条全在。"""
        names = ["c{0}".format(i) for i in range(5)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        procs = [
            subprocess.Popen(
                [sys.executable, str(CB), "branch", str(self.root), name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            for name in names
        ]
        for proc in procs:
            _, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        recorded = self.state()["branches"]
        for name in names:
            self.assertIn(name, recorded)
            # 不光看数量：每条记录必须是完整的 testing 分支，且目录真的建出来了
            self.assertEqual(recorded[name]["status"], "testing")
            self.assertTrue((self.store / "branches" / name / "PROMPT.md").is_file(), name)
        self.assertEqual(len(names), len([n for n in recorded if n.startswith("c")]))

    def test_write_refuses_to_downgrade_newer_state_format(self):
        state_path = self.store / "state.json"
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 99
        state_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        before = state_path.read_bytes()

        refused = self.run_cb(self.root, "branch", "downgrade", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("高于本脚本", refused.stderr)
        # 拒绝写入必须是彻底的：版本号、分支表、字节内容都不能变
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(self.state()["schema_version"], 99)
        self.assertNotIn("downgrade", self.state()["branches"])

    def test_check_detects_state_md_drift(self):
        self.run_cb(self.root, "branch", "a")
        check = self.run_cb(self.root, "check", check=False)
        self.assertNotIn("不一致", check.stdout)
        self.assertIn("完全一致", check.stdout)

        state_md = self.store / "STATE.md"
        state_md.write_text(state_md.read_text(encoding="utf-8") + "\n手改的一行\n", encoding="utf-8")
        drifted = self.run_cb(self.root, "check", check=False)
        self.assertEqual(drifted.returncode, 0, "漂移是警告，不该把整个体检判为失败")
        self.assertIn("不一致", drifted.stdout)

        # 任何写命令都会重建视图，漂移随之消失
        self.run_cb(self.root, "note", "a", "刷新视图")
        self.assertNotIn("不一致", self.run_cb(self.root, "check", check=False).stdout)

    def test_check_reports_state_schema_version(self):
        self.run_cb(self.root, "branch", "a")
        self.assertEqual(self.state()["schema_version"], 1)
        self.assertIn("结构版本 v1", self.run_cb(self.root, "check", check=False).stdout)

        state_path = self.store / "state.json"
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        del raw["schema_version"]
        state_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        legacy = self.run_cb(self.root, "check", check=False)
        self.assertIn("schema_version", legacy.stdout)

        raw["schema_version"] = 99
        state_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        future = self.run_cb(self.root, "check", check=False)
        self.assertNotEqual(future.returncode, 0)
        self.assertIn("高于本脚本", future.stdout)

    def test_rename_moves_dir_state_and_head(self):
        self.run_cb(self.root, "branch", "a")
        self.run_cb(self.root, "checkout", "a")
        self.run_cb(self.root, "rename", "a", "b")
        state = self.state()
        self.assertEqual(state["head"], "b", "HEAD 必须跟着改名走")
        self.assertIn("b", state["branches"])
        self.assertNotIn("a", state["branches"])
        self.assertTrue((self.store / "branches" / "b" / "PROMPT.md").is_file())
        self.assertFalse((self.store / "branches" / "a").exists())
        self.assertIn("rename", self.run_cb(self.root, "log").stdout)

    def test_rename_rejects_taken_invalid_and_archived_names(self):
        self.branch("a", prompt="v1-a\n")
        self.run_cb(self.root, "branch", "b")

        taken = self.run_cb(self.root, "rename", "a", "b", check=False)
        self.assertNotEqual(taken.returncode, 0)
        self.assertIn("已被占用", taken.stderr)
        invalid = self.run_cb(self.root, "rename", "a", "Bad", check=False)
        self.assertNotEqual(invalid.returncode, 0)
        # 以上两次失败都不得动到目录或状态
        self.assertTrue((self.store / "branches" / "a" / "PROMPT.md").is_file())
        self.assertEqual(self.state()["branches"]["a"]["status"], "testing")

        self.run_cb(self.root, "discard", "--keep", "b")
        self.assertEqual(self.state()["branches"]["a"]["status"], "archived")
        archived = self.run_cb(self.root, "rename", "a", "c", check=False)
        self.assertNotEqual(archived.returncode, 0)
        self.assertIn("不能重命名", archived.stderr)

    def test_compare_needs_a_testing_branch_then_writes_report(self):
        empty = self.run_cb(self.root, "compare", check=False)
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("没有 testing 分支", empty.stderr)
        self.assertFalse((self.store / "COMPARE.md").exists())

        self.branch("a", prompt="v2-a\n")
        self.run_cb(self.root, "compare")
        report = (self.store / "COMPARE.md").read_text(encoding="utf-8")
        for needle in ("多分支横向对比", "| a |", "同步", "a vs 当前 main"):
            self.assertIn(needle, report)

    def test_demo_workspace_is_healthy_and_never_overwritten(self):
        target = Path(self.tmp.name) / "demo-ws"
        out = self.run_cb(str(target), "demo")
        self.assertIn("示例工作区已生成", out.stdout)
        store = target / ".branches"
        state = json.loads((store / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["head"], "main")
        self.assertEqual(sorted(state["branches"]), ["style-detailed", "style-minimal"])
        self.assertTrue((store / "COMPARE.md").is_file())
        for name in state["branches"]:
            self.assertTrue((store / "branches" / name / "DIFF.md").is_file(), name)
        healthy = self.run_cb(str(target), "check")
        self.assertIn("工作区结构完整", healthy.stdout)

        again = self.run_cb(str(target), "demo", check=False)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("已存在分支工作区", again.stderr)
        self.assertEqual(
            json.loads((store / "state.json").read_text(encoding="utf-8"))["branches"].keys(),
            state["branches"].keys(),
        )

    def test_status_json_is_pure_json_and_matches_state(self):
        self.branch("a", prompt="v2-a\n")
        out = self.run_cb(self.root, "status", "--json")
        payload = json.loads(out.stdout)  # 纯 JSON：多一行人类文本都会解析失败
        state = self.state()
        self.assertEqual(payload["head"], state["head"])
        self.assertEqual(payload["main_version"], state["main_version"])
        self.assertEqual(payload["schema_version"], state["schema_version"])
        self.assertEqual(list(payload["branches"]), ["a"])
        self.assertEqual(payload["branches"]["a"]["status"], "testing")
        self.assertTrue(payload["branches"]["a"]["prompt_changed"], "改写过的分支必须标为已改")
        self.assertIn("a", self.run_cb(self.root, "status").stdout)  # 人类可读输出仍在

    def test_check_json_keeps_exit_code_and_lists(self):
        healthy = json.loads(self.run_cb(self.root, "check", "--json").stdout)
        self.assertTrue(healthy["ok"])
        self.assertEqual(healthy["problems"], [])
        self.assertTrue(healthy["info"])

        ghost = self.store / "branches" / "ghost"
        ghost.mkdir(parents=True)
        (ghost / "PROMPT.md").write_text("x\n", encoding="utf-8")
        broken = self.run_cb(self.root, "check", "--json", check=False)
        self.assertEqual(broken.returncode, 1, "--json 不得改变退出码语义")
        payload = json.loads(broken.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["problems"])

    # ---------------------------------------------------------------- MCP 协议

    def mcp_session(self, *messages, extra_raw=b""):
        """按 MCP stdio 规范发消息：每条一行 JSON。返回解析后的回复列表。"""
        payload = b"".join(
            json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n" for message in messages
        ) + extra_raw
        completed = subprocess.run([sys.executable, str(MCP)], input=payload, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        # 回归保护：绝不允许退回 LSP 的 Content-Length 帧
        self.assertNotIn(b"Content-Length", completed.stdout)
        lines = [line for line in completed.stdout.decode("utf-8").split("\n") if line.strip()]
        return [json.loads(line) for line in lines]

    def test_mcp_advertises_annotations_and_instructions(self):
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        init = replies[0]["result"]
        self.assertIn("main/PROMPT.md", init["instructions"])
        self.assertIn("promote", init["instructions"])
        self.assertIn("tools", init["capabilities"])
        by_name = {tool["name"]: tool for tool in replies[1]["result"]["tools"]}
        # cb_status 走 cb.py status，会 refresh 重建视图，所以它不是只读；真正只读的是 check 与 log
        self.assertTrue(by_name["cb_check"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["cb_log"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["cb_status"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["cb_branch"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["cb_promote"]["annotations"]["destructiveHint"])
        self.assertTrue(by_name["cb_discard"]["annotations"]["destructiveHint"])
        self.assertFalse(by_name["cb_note"]["annotations"]["destructiveHint"])
        for tool in by_name.values():
            self.assertFalse(tool["annotations"]["openWorldHint"], tool["name"])
            self.assertIn("title", tool["annotations"])
        self.assertIn("note", by_name["cb_promote"]["inputSchema"]["required"])
        # 幂等标注：报告重生成/备注/切 HEAD 重复调用无副作用，可安全重试
        self.assertTrue(by_name["cb_diff"]["annotations"]["idempotentHint"])
        self.assertTrue(by_name["cb_checkout"]["annotations"]["idempotentHint"])
        # 时间戳命名/复制型写入不幂等
        self.assertFalse(by_name["cb_export"]["annotations"]["idempotentHint"])
        self.assertFalse(by_name["cb_branch"]["annotations"]["idempotentHint"])

    def test_readonly_tools_really_do_not_touch_disk(self):
        """readOnlyHint=true 必须是真话：调完 state.json 与 STATE.md 一个字节都不能变。"""
        self.run_cb(self.root, "branch", "a")
        before = {name: (self.store / name).read_bytes() for name in ("state.json", "STATE.md")}
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cb_check", "arguments": {"root": str(self.root)}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cb_log", "arguments": {"root": str(self.root)}}},
        )
        for reply in replies[1:]:
            self.assertFalse(reply["result"]["isError"], reply)
        for name, blob in before.items():
            self.assertEqual((self.store / name).read_bytes(), blob, f"{name} 被改动，注解与实现对不上")

    def test_mcp_returns_structured_content_for_status_and_check(self):
        self.run_cb(self.root, "branch", "a")
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cb_status", "arguments": {"root": str(self.root)}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "cb_check", "arguments": {"root": str(self.root)}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "cb_log", "arguments": {"root": str(self.root)}}},
        )
        by_name = {tool["name"]: tool for tool in replies[1]["result"]["tools"]}
        self.assertIn("outputSchema", by_name["cb_status"])
        self.assertIn("outputSchema", by_name["cb_check"])
        self.assertNotIn("outputSchema", by_name["cb_log"], "没实现结构化输出的工具不要声明 outputSchema")

        status = replies[2]["result"]
        self.assertFalse(status["isError"], status)
        self.assertEqual(status["structuredContent"]["head"], self.state()["head"])
        self.assertEqual(status["structuredContent"]["head"], "a", "branch 会把 HEAD 切到新分支")
        self.assertEqual(status["structuredContent"]["main_version"], 1)
        self.assertEqual(list(status["structuredContent"]["branches"]), ["a"])
        self.assertEqual(status["structuredContent"]["main_version"], self.state()["main_version"])
        # 规范：返回 structuredContent 时 text 块要是同一份序列化 JSON（向后兼容老客户端）
        self.assertEqual(len(status["content"]), 1, "不要重复塞两份 JSON")
        self.assertEqual(json.loads(status["content"][0]["text"]), status["structuredContent"])

        check = replies[3]["result"]
        self.assertFalse(check["isError"], check)
        self.assertTrue(check["structuredContent"]["ok"])
        self.assertEqual(check["structuredContent"]["problems"], [])
        self.assertTrue(check["structuredContent"]["info"])
        self.assertTrue(json.loads(check["content"][0]["text"])["ok"])

        plain = replies[4]["result"]
        self.assertFalse(plain["isError"], plain)
        self.assertNotIn("structuredContent", plain, "未声明 outputSchema 的工具不返回 structuredContent")
        self.assertIn("[branch]", plain["content"][0]["text"], "未走 --json 的工具仍是人类可读文本")

    def test_mcp_promote_without_note_is_refused_and_changes_nothing(self):
        # 两次会话分开：同一会话里的消息全部执行完才能回看状态
        first = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cb_branch", "arguments": {"root": str(self.root), "name": "winner"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cb_promote", "arguments": {"root": str(self.root), "name": "winner"}}},
        )
        self.assertFalse(first[1]["result"]["isError"], first[1])
        refused = first[2]["result"]
        self.assertTrue(refused["isError"], refused)
        self.assertIn("note", refused["content"][0]["text"])
        # 被拒绝时主线与分支状态都不能变
        self.assertEqual(self.state()["main_version"], 1)
        self.assertEqual(self.state()["branches"]["winner"]["status"], "testing")

        second = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cb_promote", "arguments": {"root": str(self.root), "name": "winner", "note": "理由充分"}}},
        )
        self.assertFalse(second[1]["result"]["isError"], second[1])
        self.assertEqual(self.state()["main_version"], 2)

    def test_mcp_stdio_uses_newline_delimited_json(self):
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        # 通知不能有回复
        self.assertEqual([item["id"] for item in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(replies[1]["result"], {})
        names = [tool["name"] for tool in replies[2]["result"]["tools"]]
        self.assertEqual(len(names), 15)
        for expected in ("cb_status", "cb_branch", "cb_verdict", "cb_export"):
            self.assertIn(expected, names)

    def test_mcp_negotiates_protocol_version_and_supports_ping(self):
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertNotEqual(replies[0]["result"]["protocolVersion"], "1999-01-01")
        self.assertEqual(replies[1]["result"], {})
        self.assertEqual(replies[1].get("error"), None)

    def test_mcp_reports_unknown_method_and_survives_bad_json(self):
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "bogus/method"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            extra_raw=b"{not json\n",
        )
        self.assertEqual(replies[0]["error"]["code"], -32601)
        self.assertEqual(replies[1]["result"], {})
        self.assertEqual(replies[-1]["error"]["code"], -32700)

    def test_mcp_tools_call_round_trip_and_error_mapping(self):
        root = Path(self.tmp.name) / "mcp-task"
        replies = self.mcp_session(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cb_init", "arguments": {"root": str(root)}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cb_branch", "arguments": {"root": str(root), "name": "exp"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "cb_status", "arguments": {"root": str(root)}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "cb_branch", "arguments": {"root": str(root), "name": "Main"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "cb_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "cb_nosuch", "arguments": {"root": str(root)}}},
        )
        self.assertEqual([item["id"] for item in replies], [1, 2, 3, 4, 5, 6, 7])
        for index in (1, 2, 3):
            self.assertFalse(replies[index]["result"]["isError"], replies[index])
        self.assertIn("exp", replies[3]["result"]["content"][0]["text"])
        # 非法参数与非法工具名必须走 isError，而不是崩掉或返回成功
        for index in (4, 5, 6):
            self.assertTrue(replies[index]["result"]["isError"], replies[index])
        self.assertIn("Main", replies[4]["result"]["content"][0]["text"])
        self.assertTrue((root / ".branches" / "branches" / "exp").exists())


if __name__ == "__main__":
    unittest.main()
