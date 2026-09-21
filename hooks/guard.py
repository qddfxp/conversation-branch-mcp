#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""conversation-branch 的 PreToolUse 工具层守卫（可选强制层）。

SKILL.md 第 4 节的隔离铁律是提示词约束；本脚本把其中两条在工具层变成硬拦截：

  规则一  .branches/state.json 与 .branches/STATE.md 只能由 cb.py 读写，
          任何 HEAD 状态下经 Write/Edit/MultiEdit 的写入一律 deny。
  规则二  HEAD 处于某分支（state.json 顶层 "head" != "main"）时，该工作区
          main/ 目录内的一切写入一律 deny；branches/、archive/ 及工作区外
          不受限，HEAD=main 时 main/ 正常可写（主线工作不受影响）。

协议（PreToolUse 钩子，ZCode / Claude Code 风格）：
  输入   stdin 上的一个 JSON 对象，含 "tool_name" 与 "tool_input"
         （Write/Edit/MultiEdit 的 tool_input.file_path；MultiEdit 同样用
         file_path）。多余字段忽略。
  输出   拦截时向 stdout 输出一行 JSON 并 exit 0：
           {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": "<中文原因>"}}
         放行时不输出任何内容、exit 0。
  绝不使用 exit 2 —— 那会被钩子运行器当作脚本错误而非权限决定。

两个刻意的设计选择：
  * 放行方式选了「exit 0 且无输出」而非显式 allow JSON：显式 allow 会被钩子
    运行器当作一条权限决定，可能把本应询问用户的写入自动放行，等于守卫顺手
    扩大了写权限；静默放行完全不介入正常权限流程。因此本守卫只在拦截时说话。
  * 容错铁律：任何异常（stdin 不可读、JSON 解析失败、字段缺失、state.json
    损坏或缺 "head"、路径解析失败……）一律静默放行并 exit 0。宁可漏拦，
    绝不把用户工作搞瘫。

tmp 说明：cb.py 的 write_text 先写 <目标>.tmp 再 replace，因此判定前会把
文件名末尾的 .tmp 剥离 —— main/PROMPT.md.tmp 视同 main/PROMPT.md，
state.json.tmp / STATE.md.tmp 视同 state.json / STATE.md（cb.py 自己经 Bash
运行、不经过本钩子，其内部 tmp 写法不受影响）。

工作区发现：从被写文件路径向上逐级查找 .branches/state.json（与 cb.py 的
git 式向上发现一致），因此钩子配置里无需指定任何任务路径；嵌套工作区取
最近的一个。

Windows 兼容：所有路径先 Path.resolve() 再 os.path.normcase 后比较
（normcase 统一大小写与路径分隔符）；.branches 向上查找基于
Path(file_path).resolve().parents。

用法：
  钩子调用：由 ZCode 通过 stdin 喂 JSON（配置见同目录 README.md）
  自检：    python guard.py --selftest   # 构造临时工作区跑全部场景断言

环境：仅 Python 3.8+ 标准库；Windows / macOS / Linux 通用。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# —— 与 scripts/cb.py 保持一致的常量 ——
STORE_DIRNAME = ".branches"
STATE_FILE = "state.json"
STATE_MD = "STATE.md"
MAIN = "main"
BRANCHES = "branches"
ARCHIVE = "archive"

TMP_SUFFIX = ".tmp"           # cb.py write_text 的中间文件后缀
HOOK_EVENT = "PreToolUse"     # 本守卫只认这一种事件（配置里也只该挂这一种）
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# STATE.md 含大写字母；而比较用的路径 parts 已过 normcase（Windows 下会转小写），
# 因此比较值也要过一遍 normcase（POSIX 上 normcase 是恒等变换，行为不变）。
STATE_MD_CMP = os.path.normcase(STATE_MD)

ALLOW = "allow"
DENY = "deny"


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def hook_json(reason: str) -> str:
    """deny 决定的确切输出格式（hookSpecificOutput 三个键，勿增删）。"""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT,
                "permissionDecision": DENY,
                "permissionDecisionReason": reason,
            }
        },
        ensure_ascii=False,
    )


def deny_main_reason(head: str, store: Path) -> str:
    return (
        f"conversation-branch 守卫：当前 HEAD 在分支「{head}」"
        f"（工作区 {store}，见 state.json）。分支实验期间 main/ 主线只读"
        f"（SKILL.md 铁律 2：promote/rollback 只能由 cb.py 完成，不要手工搬改主线）。"
        f"请把本次改动写入 branches/{head}/ 下的对应文件；若确需修改主线，"
        f"先请用户拍板，再用 python cb.py checkout <root> main 切回主线后写。"
        f"本次写入已被拦截。"
    )


def deny_state_reason(head: str, rel_display: str) -> str:
    return (
        f"conversation-branch 守卫：{rel_display} 只能由 cb.py 读写"
        f"（state.json 是权威状态、STATE.md 是每次操作自动重写的状态视图，"
        f"手改会被覆盖；SKILL.md 铁律 8：不绕过脚本）。当前 HEAD 是"
        f"「{head}」也一样。查状态用 python cb.py status <root>；"
        f"切焦点用 python cb.py checkout <root> main|<分支名>。本次写入已被拦截。"
    )


# --------------------------------------------------------------------------- #
# 工作区发现与判定
# --------------------------------------------------------------------------- #
def find_store(target: Path):
    """从 target 向上逐级找最近的 .branches/state.json（与 cb.py 一致）。

    返回 .branches 目录；找不到返回 None。
    """
    for parent in (target, *target.parents):
        try:
            if (parent / STORE_DIRNAME / STATE_FILE).is_file():
                return parent / STORE_DIRNAME
        except OSError:
            continue  # 单级探测失败不致命，继续向上
    return None


def read_head(store: Path):
    """读 state.json 顶层 "head"；任何异常/缺失返回 None（视为读不到 → 放行）。"""
    try:
        with (store / STATE_FILE).open("r", encoding="utf-8-sig") as f:
            state = json.load(f)
        head = state.get("head") if isinstance(state, dict) else None
        if isinstance(head, str) and head.strip():
            return head.strip()
    except Exception:
        pass
    return None


def locate_parts(target: Path, root: Path):
    """返回 target 相对工作区根 root 的路径 parts（末级 .tmp 已剥离）。

    target 不在 root 之下时返回 None（工作区外）。
    """
    try:
        rel = Path(os.path.normcase(str(target))).relative_to(
            os.path.normcase(str(root))
        )
    except ValueError:
        return None
    parts = list(rel.parts)
    if parts and parts[-1].endswith(TMP_SUFFIX):
        parts[-1] = parts[-1][: -len(TMP_SUFFIX)]  # xxx.tmp 视同 xxx
    return parts


def _evaluate(tool_name, file_path):
    """核心判定。返回 (decision, reason)；decision 为 "deny" 或 "allow"。

    所有容错分支都归为 allow —— 守卫绝不能把用户工作搞瘫（宁漏拦不误伤）。
    """
    # 非 Write 家族工具不归本守卫管（Bash 里的文件操作本来就拦不住，见 README）
    if not isinstance(tool_name, str) or tool_name not in WRITE_TOOLS:
        return ALLOW, ""
    if not isinstance(file_path, str) or not file_path.strip():
        return ALLOW, ""
    try:
        raw_target = Path(os.path.expanduser(file_path.strip())).absolute()
        target = raw_target.resolve()
    except Exception:
        return ALLOW, ""

    # 先按词法路径发现工作区，避免受保护目录中的符号链接把真实路径解析到工作区外后绕过守卫。
    lexical_store = find_store(raw_target)
    store = lexical_store or find_store(target)
    if store is None:
        return ALLOW, ""  # 工作区外，与 conversation-branch 无关

    parts = locate_parts(raw_target if lexical_store else target, store.parent)
    if parts is None:
        return ALLOW, ""  # normcase 后仍不同盘/不同根：放行

    head = read_head(store)
    if head is None:
        # state.json 损坏或缺 "head"：守卫让路，绝不因状态文件读不出而误伤
        return ALLOW, ""

    # 受保护目录中的符号链接默认拒绝，避免词法路径与真实写入位置不一致。
    if lexical_store is not None and target != raw_target:
        if len(parts) >= 2 and parts[0] == STORE_DIRNAME:
            return DENY, (
                "conversation-branch 守卫：拒绝写入解析后路径不同的符号链接/重解析路径；"
                "请写入真实工作区内的普通文件。"
            )

    # 规则一：状态文件任何 HEAD 状态都拦（含 .tmp 中间文件；比较值经 normcase，
    # 因为 Windows 下 parts 已被 normcase 转小写）
    if (
        len(parts) >= 2
        and parts[0] == STORE_DIRNAME
        and parts[1] in (STATE_FILE, STATE_MD_CMP)
    ):
        rel_display = "/".join(parts)
        return DENY, deny_state_reason(head, rel_display)

    # 规则二：HEAD 在分支上时 main/ 只读（含 .tmp 中间文件）
    if (
        head != MAIN
        and len(parts) >= 2
        and parts[0] == STORE_DIRNAME
        and parts[1] == MAIN
    ):
        return DENY, deny_main_reason(head, store)

    return ALLOW, ""


def evaluate(tool_name, file_path):
    """_evaluate 的兜底包装：任何未预料的异常一律放行。"""
    try:
        return _evaluate(tool_name, file_path)
    except Exception:
        return ALLOW, ""


def process(raw):
    """完整管线：解析 stdin JSON → 判定 → 产出 stdout 内容。

    返回 deny JSON 字符串，或 None（放行/无法判定 = 不输出任何内容）。
    """
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        file_path = (
            tool_input.get("file_path") if isinstance(tool_input, dict) else None
        )
    except Exception:
        return None  # 坏 JSON / 结构不符：静默放行
    decision, reason = evaluate(tool_name, file_path)
    if decision == DENY:
        return hook_json(reason)
    return None  # 放行：不输出任何 JSON，不介入正常权限流程


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _setup_stdio():
    """stdout/stderr 统一 UTF-8，避免 Windows 本地编码写中文时崩掉。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _setup_stdio()
    if "--selftest" in argv:
        return run_selftest()
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:
        return 0  # 连 stdin 都读不了：静默放行
    try:
        out = process(raw)
    except Exception:
        out = None
    if out:
        try:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        except Exception:
            pass  # 输出失败也仍 exit 0：宁放行不误伤
    return 0  # deny 也用 exit 0 + JSON 表达；绝不用 exit 2


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #
def run_selftest():
    """构造临时工作区，逐一断言全部场景。全部通过 exit 0；任何失败 exit 1。"""
    import shutil
    import tempfile

    results = []  # (name, ok, detail)

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    def run_raw(raw):
        """跑完整管线；返回 (decision, reason)。静默放行 → ("allow", "")。"""
        out = process(raw)
        if out is None:
            return ALLOW, ""
        hso = json.loads(out)["hookSpecificOutput"]
        return hso.get("permissionDecision"), hso.get("permissionDecisionReason", "")

    try:
        tmp = tempfile.mkdtemp(prefix="cb-guard-selftest-")
    except Exception as exc:
        print(f"guard selftest: 无法创建临时目录：{exc}")
        return 1
    try:
        base = Path(tmp)
        ws = base / "ws"
        store = ws / STORE_DIRNAME
        (store / MAIN / "outputs").mkdir(parents=True)
        (store / MAIN / "inputs").mkdir()
        (store / BRANCHES / "exp" / "outputs").mkdir(parents=True)
        (store / ARCHIVE).mkdir()
        outside = base / "outside"
        (outside / "deep").mkdir(parents=True)

        def sp(*rel):  # 工作区内路径（正斜杠输入，顺带测跨分隔符）
            return str(ws).replace("\\", "/") + "/" + "/".join(rel)

        def osp(*rel):  # 工作区外路径
            return str(outside).replace("\\", "/") + "/" + "/".join(rel)

        def set_head(head):
            (store / STATE_FILE).write_text(
                json.dumps(
                    {"head": head, "branches": {"exp": {"status": "testing"}}},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        def expect(name, tool, path, want, contains=None):
            decision, reason = run_raw(
                json.dumps({"tool_name": tool, "tool_input": {"file_path": path}})
            )
            ok = decision == want and (contains is None or contains in reason)
            detail = (
                ""
                if ok
                else f"want={want} contains={contains!r}, got={decision} reason={reason!r}"
            )
            check(name, ok, detail)

        def expect_raw(name, raw, want):
            decision, reason = run_raw(raw)
            check(
                name,
                decision == want,
                "" if decision == want else f"want={want}, got={decision} {reason!r}",
            )

        # —— 场景组 1：HEAD 在分支 exp 上 ——
        # 注意工作区结构：main/、branches/、archive/ 都在 <root>/.branches/ 之下
        set_head("exp")
        expect("分支上 Write main/PROMPT.md 被拦", "Write",
               sp(STORE_DIRNAME, MAIN, "PROMPT.md"), DENY, contains="exp")
        expect("分支上 Edit main/outputs 产物被拦", "Edit",
               sp(STORE_DIRNAME, MAIN, "outputs", "report.md"), DENY, contains="exp")
        expect("分支上写 main/PROMPT.md.tmp 被拦（tmp 视同本体）", "Write",
               sp(STORE_DIRNAME, MAIN, "PROMPT.md") + TMP_SUFFIX, DENY, contains="exp")
        expect("分支上写本分支 PROMPT.md 放行", "Write",
               sp(STORE_DIRNAME, BRANCHES, "exp", "PROMPT.md"), ALLOW)
        expect("分支上写本分支 outputs 放行", "Edit",
               sp(STORE_DIRNAME, BRANCHES, "exp", "outputs", "try.md"), ALLOW)
        expect("分支上写 archive/ 放行", "Write",
               sp(STORE_DIRNAME, ARCHIVE, "old.md"), ALLOW)
        expect("分支上写工作区根 README.md 放行", "Write", sp("README.md"), ALLOW)
        expect("分支上写 state.json 被拦", "Write",
               sp(STORE_DIRNAME, STATE_FILE), DENY, contains="cb.py")
        expect("分支上写 STATE.md 被拦", "Write",
               sp(STORE_DIRNAME, STATE_MD), DENY, contains="cb.py")
        expect("分支上写 state.json.tmp 被拦（cb.py 的 tmp 写法视同本体）", "Write",
               sp(STORE_DIRNAME, STATE_FILE) + TMP_SUFFIX, DENY, contains="cb.py")
        expect("分支上写 STATE.md.tmp 被拦", "Write",
               sp(STORE_DIRNAME, STATE_MD) + TMP_SUFFIX, DENY, contains="cb.py")
        expect("MultiEdit 同样按 file_path 处理", "MultiEdit",
               sp(STORE_DIRNAME, MAIN, "PROMPT.md"), DENY, contains="exp")
        expect("Bash 工具不归本守卫管", "Bash",
               sp(STORE_DIRNAME, MAIN, "PROMPT.md"), ALLOW)
        expect("工作区外路径放行", "Write", osp("note.md"), ALLOW)
        expect("工作区外深层路径放行", "Write", osp("deep", "a.md"), ALLOW)

        # —— 场景组 2：HEAD = main ——
        set_head(MAIN)
        expect("HEAD=main 写 main/PROMPT.md 放行（主线工作正常）", "Write",
               sp(STORE_DIRNAME, MAIN, "PROMPT.md"), ALLOW)
        expect("HEAD=main Edit main/outputs 放行", "Edit",
               sp(STORE_DIRNAME, MAIN, "outputs", "o.md"), ALLOW)
        expect("HEAD=main 写 branches/ 放行", "Write",
               sp(STORE_DIRNAME, BRANCHES, "exp", "PROMPT.md"), ALLOW)
        expect("HEAD=main 写 state.json 仍被拦", "Write",
               sp(STORE_DIRNAME, STATE_FILE), DENY, contains="cb.py")
        expect("HEAD=main 写 STATE.md 仍被拦", "Write",
               sp(STORE_DIRNAME, STATE_MD), DENY, contains="cb.py")

        # —— 场景组 3：非标准输入 / 容错 ——
        expect_raw("坏 JSON 放行", "this is not json", ALLOW)
        expect_raw("payload 非对象放行", "[1, 2, 3]", ALLOW)
        expect_raw("缺 tool_input 放行", json.dumps({"tool_name": "Write"}), ALLOW)
        expect_raw("tool_input 非 dict 放行",
                   json.dumps({"tool_name": "Write", "tool_input": "oops"}), ALLOW)
        expect_raw("缺 file_path 放行",
                   json.dumps({"tool_name": "Write", "tool_input": {"content": "x"}}),
                   ALLOW)

        # —— 场景组 4：state.json 损坏 → 守卫让路 ——
        (store / STATE_FILE).write_text("{broken json", encoding="utf-8")
        expect("state.json 损坏时写 main/ 放行（宁可漏拦）", "Write",
               sp(MAIN, "PROMPT.md"), ALLOW)
        expect("state.json 损坏时写状态文件也放行", "Write",
               sp(STORE_DIRNAME, STATE_FILE), ALLOW)

        # —— 场景组 5：Windows 大小写不敏感（仅 Windows 断言）——
        if os.name == "nt":
            set_head("exp")
            expect("Windows 大小写不敏感：MAIN/PROMPT.MD 一样被拦", "Write",
                   sp(STORE_DIRNAME, MAIN, "PROMPT.md").replace("/main/", "/MAIN/").replace(
                       "PROMPT.md", "PROMPT.MD"),
                   DENY, contains="exp")
            expect("Windows 大小写不敏感：.BRANCHES/STATE.JSON 一样被拦", "Write",
                   sp(STORE_DIRNAME, STATE_FILE).replace(".branches/", ".BRANCHES/"),
                   DENY, contains="cb.py")

        npass = sum(1 for _, ok, _ in results if ok)
        ntotal = len(results)
        for name, ok, detail in results:
            print(("  PASS  " if ok else "  FAIL  ") + name
                  + ("" if ok else f"\n        -> {detail}"))
        print(f"guard selftest: {npass}/{ntotal} passed")
        return 0 if npass == ntotal else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
