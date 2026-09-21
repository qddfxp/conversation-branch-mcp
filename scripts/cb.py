#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cb.py - Conversation Branch 管理器

为承担长期任务的对话（固定提示词/工作规范/模板持续产出，如会议纪要、日报、
审稿、数据整理）提供 git-branch 式的规则隔离实验能力：

  main 是唯一权威主线；分支在独立目录试跑；
  效果差 -> discard（主线零改动）；效果好 -> promote（旧主线自动归档，可 rollback）。

子命令（分支数量无上限，支持多候选并行）：
  demo     <target>                                      生成示例工作区(主线+两分支)供快速体验
  init     <root> [--prompt FILE]                         初始化分支工作区并固化主线
  branch   <root> <name> [--from SRC] [--purpose TXT] [--no-copy-inputs]  创建分支
  checkout <root> <name>                                  切换当前工作焦点(HEAD)
  status   <root>                                         查看主线与全部分支状态
  diff     <root> [name] [--against-main|--against NAME]  生成 PROMPT 差异(可分支互比)
  compare  <root>                                         多分支横向对比总览(生成 COMPARE.md)
  note     <root> <name> <text>                           更新分支备注/阶段结论
  rename   <root> <old> <new>                             重命名 testing 分支
  discard  <root> [name ...] [--keep NAME] [--purge]      舍弃分支；--keep 批量归档其余
  promote  <root> <name> [--note TXT]                     将分支提升为新主线
  rollback <root> <ref> [--note TXT]                      用 archive 中旧主线回滚
  export   <root> <name> --to DIR                         导出分支包(供全新对话测试)
  log      <root> [--limit N]                             查看统一事件时间线(旧工作区自动降级推导)
  verdict  <root> <name> --from FILE                      回流分支实验结论(只记录，不执行 promote/discard)
  check    <root>                                         工作区完整性自检(只读，不修改任何文件)

所有状态以 <root>/.branches/state.json 为权威来源，STATE.md 为其人类可读视图，
每次状态变更后自动重写，请勿手改。
"""

import argparse
import copy
import difflib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

STORE_DIRNAME = ".branches"
STATE_FILE = "state.json"
STATE_MD = "STATE.md"
MAIN = "main"
BRANCHES = "branches"
ARCHIVE = "archive"
PARENT_SNAPSHOT = ".parent_prompt.md"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# state.json 结构版本：旧工作区（无该键）在下次写入时自动补记，便于将来做迁移判断
STATE_SCHEMA_VERSION = 1
RESERVED = {MAIN, BRANCHES, ARCHIVE}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def now_ts():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def now_human():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_text(path: Path) -> str:
    # utf-8-sig: 兼容 Windows 记事本等写出的带 BOM 文件，无 BOM 时等价 utf-8
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, content: str):
    """原子写入：mkstemp（O_CREAT|O_EXCL，无符号链接风险）+ fsync + os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp), str(path))
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def store_of(root: Path, must_exist=True) -> Path:
    root = Path(root).resolve()
    direct = root / STORE_DIRNAME
    if (direct / STATE_FILE).exists():
        return direct
    # git 式向上发现：在任务目录的任意子层级执行命令都能找到所属工作区
    for parent in (root, *root.parents):
        cand = parent / STORE_DIRNAME
        if (cand / STATE_FILE).exists():
            return cand
    if must_exist:
        die(
            f"{root} 及其上级目录均未发现分支工作区。\n"
            f"请先执行: python cb.py init \"{root}\""
        )
    return direct


def die(msg: str):
    print(f"[cb] 错误: {msg}", file=sys.stderr)
    sys.exit(1)


def load_state(store: Path) -> dict:
    # utf-8-sig：兼容手写/记事本写出的带 BOM 的 state.json
    with (store / STATE_FILE).open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_state(store: Path, state: dict):
    # 写入前先拦降级：旧脚本碰到更新格式的工作区必须拒绝写，而不是把版本号静默改回去
    version = state.get("schema_version")
    if isinstance(version, int) and version > STATE_SCHEMA_VERSION:
        die(
            f"state.json 的 schema_version=v{version} 高于本脚本支持的 v{STATE_SCHEMA_VERSION}："
            f"已拒绝写入，以免把工作区静默降级。请用更新版本的 cb.py 操作该工作区。"
        )
    state["schema_version"] = STATE_SCHEMA_VERSION
    state["updated"] = now_human()
    write_text(store / STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


LOCK_FILE = "state.lock"
LOCK_STALE_SECONDS = 30.0
LOCK_WAIT_SECONDS = 10.0


def _lock_wait_seconds() -> float:
    raw = os.environ.get("CB_LOCK_WAIT_SECONDS")
    if not raw:
        return LOCK_WAIT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return LOCK_WAIT_SECONDS


@contextmanager
def state_lock(root: Path):
    """跨进程互斥锁，保护 state.json 的读-改-写不被 CLI/MCP 并发调用互相踩踏。

    零依赖实现：用 O_CREAT|O_EXCL 抢占式锁文件；持锁进程崩溃会留下陈旧锁，
    超过 LOCK_STALE_SECONDS 秒后按陈旧锁强拆，不会永久卡死工作区。
    工作区尚未初始化（init/demo）时不加锁。
    """
    store = store_of(Path(root), must_exist=False)
    lock_path = store / LOCK_FILE
    if not (store / STATE_FILE).exists():
        yield
        return
    deadline = time.monotonic() + _lock_wait_seconds()
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock_path.unlink()  # 陈旧锁：持锁进程已不在，强拆重抢
                    continue
            except OSError:
                pass  # 锁刚好被正常释放，下一轮重抢即可
            if time.monotonic() > deadline:
                die(
                    f"等待工作区锁超时（{_lock_wait_seconds():.0f} 秒）：{lock_path}\n"
                    f"可能有另一个 cb.py / MCP 进程正在操作该工作区。"
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def add_event(state: dict, etype: str, branch: str, detail: str):
    """向 state 顶层 events 追加一条事件（旧工作区无该键时自动创建）。"""
    state.setdefault("events", []).append(
        {"time": now_human(), "type": etype, "branch": branch, "detail": detail}
    )


def validate_name(name: str):
    if name in RESERVED or not NAME_RE.match(name):
        die(
            f"分支名 '{name}' 非法：仅允许小写字母/数字/连字符/下划线，"
            f"以字母或数字开头，长度 1-40，且不得为 main/branches/archive。"
        )


def branch_dir(store: Path, name: str) -> Path:
    return store / BRANCHES / name


def branch_record(state: dict, name: str) -> dict:
    rec = state["branches"].get(name)
    if rec is None:
        die(f"分支 '{name}' 不存在。可用 status 查看全部分支。")
    return rec


def count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file())


def prompt_changed(b_dir: Path) -> bool:
    snap, cur = b_dir / PARENT_SNAPSHOT, b_dir / "PROMPT.md"
    if not snap.exists() or not cur.exists():
        return False
    return read_text(snap) != read_text(cur)


def unified_diff(old: str, new: str, old_label: str, new_label: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=old_label,
        tofile=new_label,
        n=3,
    )
    return "".join(lines)


def diff_stats(old: str, new: str):
    """返回相对 old 的 (新增行数, 删除行数)，用于多分支对比总览。"""
    plus = minus = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            plus += 1
        elif line.startswith("- "):
            minus += 1
    return plus, minus


def force_utf8_streams():
    """把被重定向的 stdout/stderr 统一成 UTF-8。

    Windows 上输出接管道/文件时标准流默认用本地代码页（如 cp936），中文会变成
    乱码，读取输出的调用方（AI 工具、日志）也会解析失败；连接真实控制台时 Python
    本身就用 UTF-8 写控制台，此处是幂等的 no-op。任何异常一律忽略。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if enc != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def cell(text, width=22) -> str:
    """Markdown 表格单元格转义并截断，避免长文本撑坏状态表。"""
    text = (text or "-").replace("|", "/").replace("\n", " ").strip() or "-"
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------- #
# STATE.md 渲染
# --------------------------------------------------------------------------- #
RULES_BLOCK = """## 操作铁律（AI 每次工作前必读）

1. 先读本文件确认 **HEAD**；一切写入只允许发生在 HEAD 对应目录，严禁写入其他分支或归档目录。
2. HEAD=main 时，正式任务严格以 `main/PROMPT.md` 为唯一事实来源；聊天历史中出现过的
   分支实验性措辞一律不构成主线规则。
3. `main/` 在任何分支 testing 期间只读；只有 promote/rollback 会改变 main，
   且旧 main 必然已完整归档到 `archive/`，任何时候都可回滚。
4. 分支试跑必须与主线使用**同源输入**；评估维度须在试跑前写入分支 `NOTES.md`，不得事后找补。
5. 分支只有两种离场方式：`discard`（归档/删除，主线零改动）或
   `promote`（成为新主线）；是否 promote 只能由用户明确决定，AI 不得自行提升。
"""


def render_state_md(store: Path, state: dict) -> str:
    lines = [
        "# Conversation Branch 状态（自动生成，勿手改；权威状态见 state.json）",
        "",
        f"- 当前 HEAD：**{state['head']}**",
        f"- 主线版本：v{state['main_version']}",
        f"- 最近更新：{state.get('updated', '-')}",
        "",
        "## 主线目录",
        f"- 规则文件：`{MAIN}/PROMPT.md`（唯一事实来源）",
        f"- 正式产物：`{MAIN}/outputs/`（当前 {count_files(store / MAIN / 'outputs')} 个文件）",
        "",
        "## 分支列表",
    ]
    active = {k: v for k, v in state["branches"].items() if v.get("status") == "testing"}
    archived = {k: v for k, v in state["branches"].items() if v.get("status") != "testing"}
    if active:
        lines += [
            "| 分支 | 来源基准 | 实验目的 | 备注结论 | PROMPT | 产物数 | 创建时间 |",
            "|---|---|---|---|---|---|---|",
        ]
        main_v = state["main_version"]
        for name, rec in sorted(active.items()):
            b_dir = branch_dir(store, name)
            changed = "已修改" if prompt_changed(b_dir) else "未修改"
            fv = rec.get("from_version", "?")
            basis = f"{rec.get('from', '-')}@v{fv}"
            if fv != "?" and fv != main_v:
                basis += f" ⚠落后于v{main_v}"
            n = count_files(b_dir / "outputs")
            note_txt = rec.get("note") or "-"
            v = rec.get("verdict")
            if v:
                mark = f"【结论:{v.get('conclusion', '-')}】"
                note_txt = mark if note_txt == "-" else note_txt + mark
            lines.append(
                f"| {name} | {basis} | {cell(rec.get('purpose'), 24)} "
                f"| {cell(note_txt, 20)} | {changed} | {n} | {rec.get('created', '-')} |"
            )
    else:
        lines.append("（暂无 testing 分支）")
    if archived:
        lines += ["", "### 已离场分支（归档记录）", "| 分支 | 状态 | 时间 | 位置 |", "|---|---|---|---|"]
        for name, rec in sorted(archived.items()):
            lines.append(
                f"| {name} | {rec['status']} | {rec.get('closed_at', '-')} "
                f"| {rec.get('archive_path', '-')} |"
            )
    promotions = state.get("promotions") or []
    if promotions:
        lines += [
            "",
            "### 已提升为新主线的分支（赢家记录）",
            "| 分支 | 成为版本 | 时间 | 旧主线归档 |",
            "|---|---|---|---|",
        ]
        for p in promotions:
            if not isinstance(p, dict):
                continue
            lines.append(
                f"| {p.get('branch', '-')} | v{p.get('version', '?')} | {p.get('time', '-')} "
                f"| {p.get('archive', '-')} |"
            )
    lines += [
        "",
        "维护命令：`check <root>`（只读自检） / `log <root>`（事件时间线）",
        "",
        RULES_BLOCK,
    ]
    return "\n".join(lines)


def refresh(store: Path, state: dict):
    save_state(store, state)
    state = load_state(store)
    write_text(store / STATE_MD, render_state_md(store, state))


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
MAIN_PROMPT_TEMPLATE = """# 主线长期规则（main/PROMPT.md）

> 本文件是这条长期任务对话的**唯一事实来源**：任务目标、长期提示词、产出规范、
> 风格与格式要求都固化在这里。每次执行正式任务前重读本文件；聊天中的临时说法
> 不构成主线规则。主线规则只能通过「开分支试跑 → 对比 → promote」修改，不直接手改。

## 1. 任务定位
（这项长期工作是什么，服务谁，产出用于什么场景）

## 2. 输入与产出
- 输入形态：
- 产出物及格式：

## 3. 长期提示词 / 工作规则
（把前期对话中沉淀的底层逻辑、提示词、处理步骤完整写在这里）

## 4. 风格与质量标准

## 5. 例外与边界
"""


def cmd_init(args):
    root = Path(args.root).resolve()
    store = root / STORE_DIRNAME
    if (store / STATE_FILE).exists():
        die(f"分支工作区已存在：{store}，无需重复初始化。")
    root.mkdir(parents=True, exist_ok=True)
    (store / MAIN / "inputs").mkdir(parents=True)
    (store / MAIN / "outputs").mkdir(parents=True)
    (store / BRANCHES).mkdir()
    (store / ARCHIVE).mkdir()

    if args.prompt:
        src = Path(args.prompt).resolve()
        if not src.is_file():
            die(f"--prompt 指定的文件不存在：{src}")
        prompt = read_text(src)
    else:
        prompt = MAIN_PROMPT_TEMPLATE
    write_text(store / MAIN / "PROMPT.md", prompt)
    write_text(
        store / MAIN / "CHANGELOG.md",
        f"# 主线版本记录\n\n## v1 — {now_human()}\n- 初始化主线，固化长期规则。\n",
    )
    state = {
        "schema": 1,
        "created": now_human(),
        "updated": now_human(),
        "head": MAIN,
        "main_version": 1,
        "branches": {},
    }
    refresh(store, state)
    print(f"[cb] 已初始化分支工作区：{store}")
    print("[cb] 下一步：把长期任务的提示词/规则整理写入 main/PROMPT.md，")
    print("     之后用 branch 子命令开实验分支。")


# --------------------------------------------------------------------------- #
# demo（一键生成可玩的示例工作区）
# --------------------------------------------------------------------------- #
DEMO_MAIN = """# 示例任务：会议纪要整理（主线 v1）

## 角色
把会议录音转写整理成结构化会议纪要。

## 输出结构
1. 会议信息（主题/时间/参会人）
2. 决议事项
3. 待办清单（必须含负责人与截止时间）

## 规则
- 客观转述，不得补充会议中未提及的内容
- 语言简洁，按议题顺序组织
"""

DEMO_INPUT = """【会议转写·示例同源素材】
张敏：这次季度复盘定在周五下班前交，数据分析部分由王磊负责。
李伟：我建议把用户流失原因单列一章，上次就是混在总结里看不清。
张敏：可以，流失分析单列，李伟你来写，周三中午前给初稿。
王磊：数据源我周二对齐，有问题群里说。
"""

DEMO_BASELINE = """# 季度复盘会议纪要（主线 v1 基线产物）

## 会议信息
- 主题：季度复盘安排；参会：张敏、李伟、王磊

## 决议事项
- 季度复盘周五下班前提交
- 用户流失原因单列一章

## 待办
- 王磊：数据分析，周五下班前；数据源周二对齐
- 李伟：流失分析初稿，周三中午前
"""

DEMO_DETAILED = """# 示例任务：会议纪要整理（候选：详尽版）

## 角色
把会议录音转写整理成结构化会议纪要。

## 输出结构
1. 会议信息（主题/时间/参会人）
2. 决策背景（每个决议为什么这么定）
3. 讨论中的不同意见
4. 决议事项
5. 待办清单（必须含负责人与截止时间）

## 规则
- 客观转述，不得补充会议中未提及的内容
- 保留关键分歧及其最终取舍理由
- 语言简洁，按议题顺序组织
"""

DEMO_DETAILED_OUT = """# 季度复盘会议纪要（详尽版试跑产物）

## 会议信息
- 主题：季度复盘安排；参会：张敏、李伟、王磊

## 决策背景
- 流失分析单列：此前混在总结中，问题指向看不清

## 不同意见
- 李伟提议用户流失原因单列成章，最终获采纳

## 决议事项
- 季度复盘周五下班前提交；流失原因单列一章

## 待办
- 王磊：数据分析（周五）、数据源周二对齐
- 李伟：流失分析初稿，周三中午前
"""

DEMO_MINIMAL = """# 示例任务：会议纪要整理（候选：极简版）

## 输出结构（只保留两块）
1. 决议事项（一句话一条）
2. 待办（负责人 + 截止时间）

## 规则
- 不写会议信息与背景，不记录讨论过程
- 每条不超过两行
"""

DEMO_MINIMAL_OUT = """决议：
- 季度复盘周五下班前提交
- 用户流失原因单列一章

待办：
- 王磊：数据分析/数据源周二对齐
- 李伟：流失分析初稿，周三中午前
"""

DEMO_NOTES_DETAILED = """# 分支实验记录：style-detailed

- 来源：main@v1
- 实验目的：候选：增加决策背景与不同意见

## 2. 评估维度（试跑前先定）
| 维度 | 主线表现 | 分支表现 | 胜出 |
|---|---|---|---|
| 关键信息完整度 | 缺背景与分歧 | 补齐 | 分支 |
| 篇幅简洁度 | 简洁 | 约长 40% | 主线 |
| 待办要素完整 | 完整 | 完整 | 平 |

## 5. 结论（待用户裁决：信息更全但更长，是否值得？）
"""


def cmd_demo(args):
    target = Path(args.target).resolve()
    store = target / STORE_DIRNAME
    if (store / STATE_FILE).exists():
        die(f"目标目录已存在分支工作区，为避免覆盖已中止：{store}")
    target.mkdir(parents=True, exist_ok=True)

    def ns(**kw):
        return SimpleNamespace(**kw)

    # 用真实命令链路构建，保证示例工作区与正常流程完全同构
    cmd_init(ns(root=str(target), prompt=None))
    write_text(store / MAIN / "PROMPT.md", DEMO_MAIN)
    write_text(store / MAIN / "inputs" / "sample-meeting.txt", DEMO_INPUT)
    write_text(store / MAIN / "outputs" / "baseline-v1.md", DEMO_BASELINE)

    cmd_branch(ns(root=str(target), name="style-detailed", from_=MAIN,
                  purpose="候选：增加决策背景与不同意见", no_copy_inputs=False))
    b1 = store / BRANCHES / "style-detailed"
    write_text(b1 / "PROMPT.md", DEMO_DETAILED)
    write_text(b1 / "outputs" / "try-detailed.md", DEMO_DETAILED_OUT)
    write_text(b1 / "NOTES.md", DEMO_NOTES_DETAILED)

    cmd_branch(ns(root=str(target), name="style-minimal", from_=MAIN,
                  purpose="候选：只留决议与待办的极简版", no_copy_inputs=False))
    b2 = store / BRANCHES / "style-minimal"
    write_text(b2 / "PROMPT.md", DEMO_MINIMAL)
    write_text(b2 / "outputs" / "try-minimal.md", DEMO_MINIMAL_OUT)

    state = load_state(store)
    state["branches"]["style-detailed"]["note"] = "信息更全，篇幅约+40%，待裁决"
    state["branches"]["style-minimal"]["note"] = "最快，但丢失会议信息"
    state["head"] = MAIN
    refresh(store, state)
    cmd_diff(ns(root=str(target), name="style-detailed", against_main=False, against=None))
    cmd_diff(ns(root=str(target), name="style-minimal", against_main=False, against=None))
    cmd_compare(ns(root=str(target)))

    print("\n[cb] 示例工作区已生成（可随时整个删除，不影响任何真实任务）：")
    print(f"     {store}")
    print("[cb] 推荐体验路线：")
    print(f"  1) python cb.py status  \"{target}\"                 # 看主线与两个候选分支")
    print(f"  2) python cb.py compare \"{target}\"                  # 看横向对比总览 COMPARE.md")
    print(f"  3) python cb.py diff   \"{target}\" style-detailed --against style-minimal")
    print("  4) 选定后 promote 赢家，再用 discard --keep 清理；不满意可 rollback 1 回退")


# --------------------------------------------------------------------------- #
# branch
# --------------------------------------------------------------------------- #
NOTES_TEMPLATE = """# 分支实验记录：{name}

- 来源：{src}@v{version}
- 创建时间：{created}
- 实验目的：{purpose}

## 1. 实验假设（本次改动预期解决什么问题、带来什么提升）

## 2. 评估维度（试跑前先定，例如：信息完整度 / 结构清晰度 / 冗余度 / 格式合规）
| 维度 | 主线表现 | 分支表现 | 胜出 |
|---|---|---|---|
|  |  |  |  |

## 3. 测试素材（必须与主线同源；默认已复制到本分支 inputs/）

## 4. 试跑结果（产物放本分支 outputs/，勿动 main/）

## 5. 结论（提升 → 请用户 promote；持平/变差 → discard）
"""


def resolve_source(store: Path, state: dict, src: str) -> Path:
    if src == MAIN:
        return store / MAIN
    rec = state["branches"].get(src)
    if rec is None or rec.get("status") != "testing":
        die(f"来源分支 '{src}' 不存在或已离场。")
    return branch_dir(store, src)


def cmd_branch(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    name = args.name
    validate_name(name)
    if name in state["branches"]:
        die(f"分支 '{name}' 已存在。")
    src = args.from_ or MAIN
    src_dir = resolve_source(store, state, src)
    src_prompt = src_dir / "PROMPT.md"
    if src == MAIN:
        source_version = state["main_version"]
    else:
        source_version = state["branches"][src].get("base_version", state["branches"][src].get("from_version"))
        if not isinstance(source_version, int):
            die(f"来源分支 '{src}' 缺少可靠的基准版本，无法安全派生。")
    if not src_prompt.is_file():
        die(f"来源缺少 PROMPT.md：{src_prompt}")

    b_dir = branch_dir(store, name)
    (b_dir / "outputs").mkdir(parents=True)
    write_text(b_dir / "PROMPT.md", read_text(src_prompt))
    write_text(b_dir / PARENT_SNAPSHOT, read_text(src_prompt))
    if not args.no_copy_inputs and (src_dir / "inputs").exists():
        shutil.copytree(src_dir / "inputs", b_dir / "inputs", dirs_exist_ok=True)
    else:
        (b_dir / "inputs").mkdir()
    write_text(
        b_dir / "NOTES.md",
        NOTES_TEMPLATE.format(
            name=name,
            src=src,
            version=source_version,
            created=now_human(),
            purpose=args.purpose or "（未填写）",
        ),
    )
    write_text(b_dir / "DIFF.md", "（PROMPT.md 尚未修改，暂无差异。运行 diff 子命令刷新）\n")

    state["branches"][name] = {
        "created": now_human(),
        "from": src,
        "from_version": source_version,
        "base_version": source_version,
        "status": "testing",
        "purpose": args.purpose or "",
    }
    state["head"] = name
    add_event(state, "branch", name, f"创建实验分支（来源 {src}@v{source_version}）")
    refresh(store, state)
    print(f"[cb] 已创建并切换到分支 '{name}'（来源 {src}）：{b_dir}")
    print("[cb] 现在只在该分支目录内工作：修改 PROMPT.md，用 inputs/ 同源素材试跑，产物写入 outputs/。")


# --------------------------------------------------------------------------- #
# checkout / status
# --------------------------------------------------------------------------- #
def cmd_checkout(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    name = args.name
    if name != MAIN:
        rec = branch_record(state, name)
        if rec.get("status") != "testing":
            die(f"分支 '{name}' 状态为 {rec.get('status')}，不能切换。")
    state["head"] = name
    refresh(store, state)
    target = store / MAIN if name == MAIN else branch_dir(store, name)
    print(f"[cb] HEAD -> {name}，工作目录：{target}")
    print("[cb] 提醒：只允许向当前 HEAD 目录写入。")


def status_payload(store: Path, state: dict) -> dict:
    """status 的结构化版本：与 STATE.md 同源（都来自 state.json + 分支目录现状），供 --json 与 MCP structuredContent 使用。"""
    branches = {}
    for name, rec in sorted((state.get("branches") or {}).items()):
        b_dir = branch_dir(store, name)
        exists = b_dir.is_dir()
        branches[name] = {
            "status": rec.get("status"),
            "from": rec.get("from"),
            "from_version": rec.get("from_version"),
            "prompt_changed": prompt_changed(b_dir) if exists else False,
            "outputs": count_files(b_dir / "outputs") if exists else 0,
            "note": rec.get("note") or "",
        }
    return {
        "root": str(store.parent),
        "schema_version": state.get("schema_version"),
        "head": state.get("head"),
        "main_version": state.get("main_version"),
        "updated": state.get("updated"),
        "branches": branches,
    }


def cmd_status(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    refresh(store, state)
    if getattr(args, "json", False):
        print(json.dumps(status_payload(store, state), ensure_ascii=False))
        return
    print(read_text(store / STATE_MD))


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def cmd_diff(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    name = args.name or state["head"]
    if name == MAIN:
        die("main 没有父版本可对比；请指定一个分支名，或直接查看 archive/ 中的历史主线。")
    branch_record(state, name)
    b_dir = branch_dir(store, name)
    cur = b_dir / "PROMPT.md"
    if not cur.is_file():
        die(f"分支缺少 PROMPT.md：{cur}")
    if args.against_main:
        base = store / MAIN / "PROMPT.md"
        old_label = "main/PROMPT.md (current)"
        basis_desc = "当前 main"
    elif getattr(args, "against", None):
        other = args.against
        if other == name:
            die("分支不能与自身对比，请指定另一个分支。")
        orec = branch_record(state, other)
        if orec.get("status") != "testing":
            die(f"对比目标分支 '{other}' 状态为 {orec.get('status')}，不能作为对比基准。")
        base = branch_dir(store, other) / "PROMPT.md"
        old_label = f"{other}/PROMPT.md"
        basis_desc = f"另一分支 {other}"
    else:
        base = b_dir / PARENT_SNAPSHOT
        old_label = f"{name}@创建时父版本"
        basis_desc = "创建分支时的父版本快照"
    if not base.is_file():
        die(f"对比基准缺失：{base}")
    diff_text = unified_diff(
        read_text(base), read_text(cur), old_label, f"{name}/PROMPT.md (now)"
    )
    out = b_dir / "DIFF.md"
    header = (
        f"# {name} 的 PROMPT 差异（{now_human()} 自动生成）\n\n"
        f"对比基准：{basis_desc}\n\n"
    )
    body = diff_text if diff_text.strip() else "（无差异）"
    write_text(out, header + "```diff\n" + body + "\n```\n")
    print(diff_text if diff_text.strip() else "[cb] PROMPT.md 与基准完全一致，暂无差异。")
    print(f"[cb] 差异已写入：{out}")


# --------------------------------------------------------------------------- #
# discard
# --------------------------------------------------------------------------- #
def _discard_one(store: Path, state: dict, name: str, purge: bool):
    """舍弃单个 testing 分支；返回 (名称, 动作描述, 位置)。"""
    rec = branch_record(state, name)
    if rec.get("status") != "testing":
        die(f"分支 '{name}' 已离场，无需重复舍弃。")
    b_dir = branch_dir(store, name)
    if purge:
        shutil.rmtree(b_dir)
        del state["branches"][name]
        return name, "已彻底删除", "-"
    archive_name = f"branch-{name}-{now_ts()}"
    dest = store / ARCHIVE / archive_name
    shutil.move(str(b_dir), str(dest))
    loc = f"{ARCHIVE}/{archive_name}"
    rec["status"] = "archived"
    rec["closed_at"] = now_human()
    rec["archive_path"] = loc
    return name, "已归档", loc


def cmd_discard(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    testing = [k for k, v in state["branches"].items() if v.get("status") == "testing"]
    if args.keep:
        if args.names:
            die("--keep 与逐个列出分支名不能同时使用，二选一。")
        keep = args.keep
        keep_rec = state["branches"].get(keep)
        promoted = {
            p.get("branch")
            for p in (state.get("promotions") or [])
            if isinstance(p, dict)
        }
        if keep_rec is None and keep not in promoted:
            die(
                f"分支 '{keep}' 不存在（既不是 testing 分支，也没有 promote 记录）。"
                f"若名字打错，请不要用 --keep，以免误清理其他分支；可用 status 查看全部分支。"
            )
        if keep_rec is not None and keep_rec.get("status") != "testing":
            die(f"--keep 保留的分支 '{keep}' 状态为 {keep_rec.get('status')}，不是 testing 分支。")
        targets = [n for n in testing if n != keep]
        if not targets:
            if keep_rec is None:
                print(
                    f"[cb] 赢家 '{keep}' 已在 promote 时离场，除它以外没有其他 testing 分支需要清理，"
                    f"本次无需操作。"
                )
            else:
                print(f"[cb] 除 '{keep}' 外没有其他 testing 分支，无需清理。main/ 与正式产物未受影响。")
            return
    else:
        targets = args.names
        if not targets:
            die("请指定要舍弃的分支名（可一次多个），或用 --keep <赢家> 批量归档其余分支。")

    # 两阶段执行：先把全部目标校验干净，避免逐个搬运中途退出后 state 与磁盘不一致
    deduped = []
    for n in targets:
        if n not in deduped:
            deduped.append(n)
    targets = deduped
    for n in targets:
        rec = state["branches"].get(n)
        if rec is None:
            die(
                f"分支 '{n}' 不存在（当前 testing 分支：{', '.join(sorted(testing)) or '无'}）。"
                f"为避免误操作，本次未做任何清理。"
            )
        if rec.get("status") != "testing":
            die(f"分支 '{n}' 状态为 {rec.get('status')}，不能舍弃。为避免误操作，本次未做任何清理。")

    for n in targets:
        v = (state["branches"].get(n) or {}).get("verdict") or {}
        if v.get("conclusion") == "promote":
            print(
                f"[cb] 注意：分支 '{n}' 已记录的实验结论是 promote（提升），本次却要舍弃；"
                f"如属误操作请立即停止（归档后仍可从 archive/ 找回）。"
            )

    results = []
    try:
        for n in targets:
            results.append(_discard_one(store, state, n, args.purge))
    finally:
        # 即使某个目标中途出错，也要把已完成的部分写回状态，保持 state 与磁盘一致
        for name, action, loc in results:
            add_event(state, "discard", name, f"{action}至 {loc}" if loc != "-" else "已彻底删除（不保留归档）")
        still_active = {MAIN, *(k for k, v in state["branches"].items() if v.get("status") == "testing")}
        if state["head"] not in still_active:
            state["head"] = MAIN
        refresh(store, state)
    for name, action, loc in results:
        print(f"[cb] 分支 '{name}' {action}：{loc}")
    if args.keep:
        print(f"[cb] 已保留赢家分支 '{args.keep}'。")
    print(f"[cb] main/ 全程未被触碰，主线规则与正式产物保持原样；当前 HEAD：{state['head']}。")


# --------------------------------------------------------------------------- #
# promote
# --------------------------------------------------------------------------- #
def cmd_promote(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    name = args.name
    rec = branch_record(state, name)
    if rec.get("status") != "testing":
        die(f"分支 '{name}' 状态为 {rec.get('status')}，不能提升。")
    b_dir = branch_dir(store, name)
    base_version = rec.get("base_version", rec.get("from_version"))
    if isinstance(base_version, int) and base_version != state["main_version"]:
        die(
            f"分支 '{name}' 基于旧主线 v{base_version}，当前主线已是 v{state['main_version']}；"
            "为避免覆盖新规则，请从当前 main 重新开分支后再提升。"
        )
    if not (b_dir / "PROMPT.md").is_file():
        die(f"分支 '{name}' 缺少 PROMPT.md（{b_dir / 'PROMPT.md'}），无法提升；请先用 check 复核该工作区。")
    if not (b_dir / PARENT_SNAPSHOT).is_file():
        die(
            f"分支 '{name}' 缺少创建时的父版本快照 {PARENT_SNAPSHOT}，无法生成规则差异，故不提升。\n"
            f"请先跑 check 复核；若该分支不完整，可舍弃后从当前主线重新开一个分支。"
        )
    if not prompt_changed(b_dir):
        print("[cb] 警告：该分支的 PROMPT.md 与创建时完全相同（没有任何规则改动）。")
        if not args.note:
            die("若确认要提升，请用 --note 说明理由；否则请 discard。")

    old_version = state["main_version"]
    ts = now_ts()
    old_main = store / MAIN
    archive_name = f"main-v{old_version}-{ts}"
    archived_main = store / ARCHIVE / archive_name

    # move 前先取出需要延续/对比的内容
    old_changelog = read_text(old_main / "CHANGELOG.md") if (old_main / "CHANGELOG.md").exists() else ""
    diff_text = unified_diff(
        read_text(b_dir / PARENT_SNAPSHOT),
        read_text(b_dir / "PROMPT.md"),
        f"v{old_version}/PROMPT.md",
        f"v{old_version + 1}/PROMPT.md",
    ) or "（规则文本无差异）"

    verdict = rec.get("verdict") or {}
    verdict_line = ""
    if verdict.get("conclusion"):
        verdict_line = (
            f"- 分支实验结论：{verdict.get('conclusion')}"
            f"（记录于 {verdict.get('time', '-')}）\n"
        )
        if verdict.get("conclusion") == "discard":
            print(
                f"[cb] 注意：分支 '{name}' 已记录的实验结论是 discard（舍弃），"
                f"本次却要提升为正式主线；请确认这是用户的明确决定。"
            )

    # 快照：promote 中途失败时用于恢复，绝不让主线停在半成品状态
    orig_state = copy.deepcopy(state)

    def _restore_after_failure(exc):
        # 只有确认旧主线已离开 main/（搬运成功）时才动 main/，避免误删未被移动过的原主线
        try:
            if archived_main.is_dir() and not old_main.exists():
                if (store / MAIN).is_dir():
                    shutil.rmtree(store / MAIN)
                shutil.move(str(archived_main), str(store / MAIN))
            save_state(store, orig_state)
            write_text(store / STATE_MD, render_state_md(store, orig_state))
        except Exception:
            pass
        die(
            f"promote 中途失败，已尽力恢复到操作前状态：{exc}\n"
            f"请先运行 python cb.py check \"{root}\" 确认工作区完整性后再重试。"
        )

    try:
        # 1. 旧主线整体归档（绝不删除）
        shutil.move(str(old_main), str(archived_main))

        # 2. 用分支重建主线
        new_main = store / MAIN
        (new_main / "inputs").mkdir(parents=True)
        (new_main / "outputs").mkdir(parents=True)
        write_text(new_main / "PROMPT.md", read_text(b_dir / "PROMPT.md"))
        if (b_dir / "inputs").exists():
            shutil.copytree(b_dir / "inputs", new_main / "inputs", dirs_exist_ok=True)
        if count_files(b_dir / "outputs"):
            shutil.copytree(
                b_dir / "outputs",
                new_main / "outputs" / f"from-branch-{name}",
                dirs_exist_ok=True,
            )
        new_entry = (
            f"\n## v{old_version + 1} — {now_human()}\n"
            f"- 来源分支：{name}（基于 v{old_version}）\n"
            f"- 提升说明：{args.note or '（未填写）'}\n"
            f"{verdict_line}"
            f"- 规则差异：\n\n```diff\n{diff_text}\n```\n"
            f"- 上一版主线完整归档：{ARCHIVE}/{archive_name}/"
            f"（回滚命令：python cb.py rollback \"{root}\" {old_version}）\n"
        )
        write_text(new_main / "CHANGELOG.md", old_changelog.rstrip() + "\n" + new_entry)

        # 3. 状态推进（赢家记入 promotions，供 status / discard --keep 识别）
        del state["branches"][name]
        state["main_version"] = old_version + 1
        state["head"] = MAIN
        promotions = state.get("promotions")
        if not isinstance(promotions, list):
            state["promotions"] = promotions = []
        promotions.append(
            {
                "branch": name,
                "version": old_version + 1,
                "from_version": old_version,
                "time": now_human(),
                "archive": f"{ARCHIVE}/{archive_name}",
            }
        )
        add_event(
            state,
            "promote",
            name,
            f"提升为新主线 v{old_version + 1}，旧主线归档至 {ARCHIVE}/{archive_name}",
        )
        refresh(store, state)
    except Exception as exc:
        _restore_after_failure(exc)

    # 4. 旧分支目录清理：失败只提示，不影响已就绪的新主线
    try:
        shutil.rmtree(b_dir)
    except Exception as exc:
        print(
            f"[cb] 警告：新主线已就绪，但旧分支目录未能删除（{exc}）：{b_dir}\n"
            f"     该目录只是残留副本，主线不受影响；确认后可稍后手动删除。"
        )

    print(f"[cb] 分支 '{name}' 已提升为新主线 v{old_version + 1}。")
    print(f"[cb] 旧主线 v{old_version} 完整归档于：{archived_main}")
    leftover = [k for k, v in state["branches"].items() if v.get("status") == "testing"]
    if leftover:
        print(f"[cb] 仍有其他 testing 分支保留：{', '.join(sorted(leftover))}（它们基于更早的主线版本）。")
    print("[cb] 对话层动作：立即重读 main/PROMPT.md，向用户复述新主线要点，此后正式工作按新主线执行。")


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #
def resolve_archive_ref(store: Path, ref: str) -> Path:
    # 安全校验：归档引用只允许单段安全文件名，杜绝 ../ 路径穿越
    if not re.fullmatch(r"[A-Za-z0-9._-]+", ref) or ".." in ref:
        die(f"非法归档引用 '{ref}'：只允许版本号或 archive 下的目录名。")
    candidate = store / ARCHIVE / ref
    if candidate.is_dir():
        return candidate
    matches = sorted(
        (m for m in (store / ARCHIVE).glob(f"main-v{ref}-*") if m.is_dir()),
        key=lambda p: p.name,
    )
    if not matches:
        die(f"archive 中找不到与 '{ref}' 匹配的历史主线（版本号或归档目录名）。")
    normal = [m for m in matches if not m.name.endswith("-before-rollback")]
    snapshots = [m for m in matches if m.name.endswith("-before-rollback")]
    if not normal:
        print(
            f"[cb] 提示：v{ref} 只有回滚前快照 {snapshots[-1].name}，将按其内容恢复。",
            file=sys.stderr,
        )
        return snapshots[-1]
    return normal[-1]


def cmd_rollback(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    target = resolve_archive_ref(store, args.ref)
    if not (target / "PROMPT.md").is_file():
        die(f"归档内容不完整，缺少 PROMPT.md：{target}")

    cur_version = state["main_version"]
    ts = now_ts()
    cur_main = store / MAIN
    # move 前取出当前完整 CHANGELOG，保证回滚后版本链条依然连续
    cur_changelog = cur_main / "CHANGELOG.md"
    current_log = read_text(cur_changelog) if cur_changelog.exists() else ""
    safety_name = f"main-v{cur_version}-{ts}-before-rollback"
    safety_dir = store / ARCHIVE / safety_name
    staged_main = Path(tempfile.mkdtemp(prefix=".main-rollback-", dir=str(store)))
    new_version = cur_version + 1
    try:
        # 先在工作区内完整构建新主线，避免移动 current main 后复制失败留下空工作区。
        shutil.rmtree(staged_main)
        shutil.copytree(target, staged_main)
        changelog = staged_main / "CHANGELOG.md"
        entry = (
            f"\n## v{new_version} — {now_human()}\n"
            f"- 内容回滚至归档：{ARCHIVE}/{target.name}/\n"
            f"- 回滚说明：{args.note or '（未填写）'}\n"
            f"- 回滚前主线已安全归档：{ARCHIVE}/{safety_name}/（版本号单调递增，历史不倒转）\n"
        )
        write_text(changelog, current_log.rstrip() + "\n" + entry)
        if safety_dir.exists():
            raise FileExistsError(f"回滚安全归档已存在：{safety_dir}")
        shutil.move(str(cur_main), str(safety_dir))
        try:
            shutil.move(str(staged_main), str(cur_main))
        except Exception:
            if not cur_main.exists() and safety_dir.exists():
                shutil.move(str(safety_dir), str(cur_main))
            raise
    except Exception as exc:
        if staged_main.exists():
            shutil.rmtree(staged_main, ignore_errors=True)
        if not cur_main.exists() and safety_dir.exists():
            try:
                shutil.move(str(safety_dir), str(cur_main))
            except Exception:
                pass
        die(
            f"rollback 中途失败，已尽力恢复原主线：{exc}\n"
            f"请运行 python cb.py check \"{root}\" 检查工作区完整性。"
        )

    state["main_version"] = new_version
    state["head"] = MAIN
    add_event(state, "rollback", "-", f"主线回滚至 {ARCHIVE}/{target.name}，当前版本 v{new_version}（回滚前已归档 {safety_name}）")
    try:
        refresh(store, state)
    except Exception as exc:
        print(f"[cb] 警告：主线内容已恢复，但状态文件刷新失败：{exc}。请立即运行 check。", file=sys.stderr)
        raise
    print(f"[cb] 已用 {target.name} 的内容恢复主线，当前版本号 v{new_version}（内容等价旧版，版本号继续递增）。")
    print(f"[cb] 回滚前状态安全归档于：{store / ARCHIVE / safety_name}/")


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def extract_notes_section(notes_path: Path) -> str:
    """从分支 NOTES.md 提取「## 2. 评估维度」小节正文（至下一个 ## 标题为止）；无则返回空串。"""
    if not notes_path.is_file():
        return ""
    body, in_sec = [], False
    for line in read_text(notes_path).splitlines():
        if re.match(r"^##\s", line):
            in_sec = bool(re.match(r"^##\s*2[.、]?\s*评估维度", line))
            continue
        if in_sec:
            body.append(line)
    return "\n".join(body).strip()


def cmd_export(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    name = args.name
    rec = branch_record(state, name)
    b_dir = branch_dir(store, name)
    to_dir = Path(args.to).resolve()
    if not to_dir.exists():
        die(f"导出目标父目录不存在，请先创建：{to_dir}")
    try:
        to_dir.relative_to(b_dir)
    except ValueError:
        pass
    else:
        die(f"导出目标不能位于源分支目录内部：{to_dir}")
    dest = to_dir / f"{name}-export-{now_ts()}"
    if dest.exists():
        die(f"导出目标已存在：{dest}")
    dest.mkdir(parents=True)
    try:
        for item in ("PROMPT.md", "NOTES.md", "DIFF.md"):
            src = b_dir / item
            if src.is_file():
                shutil.copy2(src, dest / item)
        for sub in ("inputs", "outputs"):
            if (b_dir / sub).exists():
                shutil.copytree(b_dir / sub, dest / sub, dirs_exist_ok=True)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    # VERDICT.md 结论模板：预填分支信息；评估维度小节尽量从 NOTES.md 带出，带回不了就留空表
    dims = extract_notes_section(b_dir / "NOTES.md")
    if dims:
        dims_block = "\n".join(("  " + ln).rstrip() for ln in dims.splitlines())
    else:
        dims_block = (
            "  | 维度 | 主线表现 | 分支表现 | 胜出 |"
            "\n  |---|---|---|---|"
            "\n  |  |  |  |  |"
        )
    verdict_tpl = (
        f"# 实验结论回执：{name}\n\n"
        f"- 分支：{name}\n"
        f"- 来源主线版本：v{rec.get('from_version', '?')}\n"
        f"- 实验目的：{rec.get('purpose') or '（未填写）'}\n"
        f"- 生成时间：{now_human()}（本文件由 cb.py export 自动生成）\n\n"
        "## 结论区（按极简 key:value 格式填写；空行与 # 开头行会被忽略）\n\n"
        "conclusion: \n"
        "dimensions:\n"
        f"{dims_block}\n"
        "scores:\n"
        "notes:\n\n"
        "---\n\n"
        "## 填写说明\n"
        "1. conclusion 必填，二选一：提升（或 promote）/ discard（或 变差、舍弃），大小写不限。默认留空——留空会被 verdict 拒绝，防止未评估就回流。\n"
        "2. dimensions / scores / notes 选填；多行内容（如维度表格）每行行首缩进两个空格，会被视为该键值的续行。\n"
        "3. 只需修改上方结论区，本行以下的说明无需改动。\n\n"
        "## 回流方法\n"
        "填完后回到**原长期任务对话**执行（<root> 为工作区路径，引号内为本文件完整路径）：\n\n"
        f"    python cb.py verdict <root> {name} --from \"{dest / 'VERDICT.md'}\"\n\n"
        "verdict 只记录结论，不执行 promote/discard；随后按结论在原对话执行\n"
        f"`python cb.py promote <root> {name}`（效果好）或 `python cb.py discard <root> {name}`（效果差）。\n"
    )
    write_text(dest / "VERDICT.md", verdict_tpl)
    handoff = (
        f"# 分支实验交接包：{name}\n\n"
        f"导出时间：{now_human()}\n来源主线版本：v{rec.get('from_version')}\n\n"
        "## 在全新对话窗口中的测试方法\n"
        "1. 开一个新对话，把 `PROMPT.md` 全文作为该任务的规则发给它；\n"
        "2. 把 `inputs/` 中的同源测试素材交给它处理；\n"
        "3. 产出结果后与主线旧产物对比，把结论填入 VERDICT.md；\n"
        "4. 效果好在原长期对话执行 promote，效果差执行 discard。\n"
    )
    write_text(dest / "HANDOFF.md", handoff)
    add_event(state, "export", name, f"导出交接包至 {dest}")
    refresh(store, state)
    print(f"[cb] 分支 '{name}' 已导出到：{dest}（可复制到任意新对话窗口做干净上下文测试）")


# --------------------------------------------------------------------------- #
# compare / note / rename（多分支管理）
# --------------------------------------------------------------------------- #
def cmd_compare(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    mv = state["main_version"]
    active = sorted(k for k, v in state["branches"].items() if v.get("status") == "testing")
    if not active:
        die("当前没有 testing 分支，无需横向对比；先用 branch 开候选分支。")
    main_prompt_path = store / MAIN / "PROMPT.md"
    main_prompt = read_text(main_prompt_path) if main_prompt_path.is_file() else ""

    rows, sections = [], []
    for name in active:
        rec = state["branches"][name]
        b_dir = branch_dir(store, name)
        cur = read_text(b_dir / "PROMPT.md") if (b_dir / "PROMPT.md").is_file() else ""
        plus, minus = diff_stats(main_prompt, cur)
        fv = rec.get("from_version", mv)
        lag = "同步" if fv == mv else f"落后(v{fv}->v{mv})"
        changed = "已改" if prompt_changed(b_dir) else "未改"
        rows.append(
            f"| {name} | {rec.get('from', '-')}@v{fv} | {lag} "
            f"| {cell(rec.get('purpose'), 26)} | +{plus}/-{minus} | {changed} "
            f"| {count_files(b_dir / 'outputs')} | {cell(rec.get('note'), 20)} |"
        )
        d = unified_diff(main_prompt, cur, "main/PROMPT.md", f"{name}/PROMPT.md")
        sections.append(
            f"### {name} vs 当前 main（+{plus}/-{minus}，基准{lag}）\n\n"
            f"```diff\n{d if d.strip() else '（与主线无差异）'}\n```"
        )

    md = [
        f"# 多分支横向对比（{now_human()} 自动生成）",
        "",
        f"当前主线版本 v{mv}；改动量为各分支相对当前 main/PROMPT.md 的增/删行数。",
        "标注“落后”的分支基于更早主线，建议从当前主线重开或先人工补齐主线新增内容。",
        "",
        "| 分支 | 来源基准 | 与主线同步 | 实验目的 | 相对主线改动 | PROMPT | 产物数 | 备注结论 |",
        "|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "## 下一步",
        "- 两两细看：`python cb.py diff <root> <A> --against <B>`",
        "- 定赢家后：`promote <root> <赢家>`，再 `discard <root> --keep <赢家>` 批量清理落选分支。",
        "",
        "## 各分支 vs 当前 main 差异明细",
        "",
        "\n\n".join(sections),
        "",
    ]
    write_text(store / "COMPARE.md", "\n".join(md))
    print(f"[cb] 多分支横向对比（主线 v{mv}，共 {len(active)} 个 testing 分支）")
    print("| 分支 | 来源基准 | 同步 | 目的 | 改动 | PROMPT | 产物 | 备注 |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(r)
    print(f"[cb] 完整差异明细已写入：{store / 'COMPARE.md'}")


def cmd_note(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    rec = branch_record(state, args.name)
    if rec.get("status") != "testing":
        die(f"分支 '{args.name}' 状态为 {rec.get('status')}，不能改备注。")
    rec["note"] = args.text
    add_event(state, "note", args.name, f"备注更新：{args.text}")
    refresh(store, state)
    print(f"[cb] 分支 '{args.name}' 备注已更新：{args.text}")


def cmd_rename(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    old, new = args.old, args.new
    rec = branch_record(state, old)
    if rec.get("status") != "testing":
        die(f"分支 '{old}' 状态为 {rec.get('status')}，不能重命名。")
    validate_name(new)
    if new in state["branches"]:
        die(f"分支名 '{new}' 已被占用。")
    old_dir, new_dir = branch_dir(store, old), branch_dir(store, new)
    shutil.move(str(old_dir), str(new_dir))
    state["branches"][new] = rec
    del state["branches"][old]
    if state["head"] == old:
        state["head"] = new
    add_event(state, "rename", new, f"由 '{old}' 重命名而来")
    refresh(store, state)
    print(f"[cb] 分支已重命名：{old} -> {new}；目录：{new_dir}")


# --------------------------------------------------------------------------- #
# verdict（实验结论回流）
# --------------------------------------------------------------------------- #
def parse_verdict_file(path: Path) -> dict:
    """解析极简 key:value 结论文件：忽略空行与 # 开头行；缩进续行归入上一键值。"""
    data: dict = {}
    cur = None
    for raw in read_text(path).splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            cur = None
            continue
        if raw[:1] in (" ", "\t") and cur:
            data[cur] = f"{data[cur]}\n{stripped}" if data[cur] else stripped
            continue
        m = re.match(r"^([^:：]+)[：:]\s*(.*)$", raw)
        if m:
            cur = m.group(1).strip().lower()
            data[cur] = m.group(2).strip()
        else:
            cur = None
    return data


def cmd_verdict(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    name = args.name
    rec = branch_record(state, name)
    if rec.get("status") != "testing":
        die(f"分支 '{name}' 状态为 {rec.get('status')}，verdict 只允许对 testing 分支执行。")
    src = Path(args.from_file).resolve()
    if not src.is_file():
        die(f"--from 指定的结论文件不存在：{src}")
    data = parse_verdict_file(src)
    raw = data.get("conclusion", "")
    if not raw:
        die(
            "结论文件缺少必填键 'conclusion'（写法：`conclusion: 提升` 或 `conclusion: discard`，"
            f"大小写不限）。\n可选键：dimensions / scores / notes。文件：{src}"
        )
    low = raw.strip().lower()
    if low in ("提升", "promote"):
        conclusion = "promote"
    elif low in ("discard", "变差", "舍弃"):
        conclusion = "discard"
    else:
        die(f"conclusion 取值非法：'{raw.strip()}'（只允许 提升/promote 或 discard/变差/舍弃，大小写不限）。")
    conclusion_disp = "promote（提升）" if conclusion == "promote" else "discard（舍弃）"

    def sec(key: str) -> str:
        return data.get(key, "").strip() or "（未填写）"

    ts = now_human()
    content = (
        f"# 分支实验结论：{name}\n\n"
        f"- 结论：**{conclusion_disp}**\n"
        f"- 解析时间：{ts}\n"
        f"- 来源文件：{src}\n"
        f"- 分支：{name}（来源主线 v{rec.get('from_version', '?')}）\n\n"
        f"## conclusion（实验结论）\n\n{conclusion_disp}\n\n"
        f"## dimensions（评估维度结论）\n\n{sec('dimensions')}\n\n"
        f"## scores（维度打分）\n\n{sec('scores')}\n\n"
        f"## notes（补充说明）\n\n{sec('notes')}\n"
    )
    write_text(branch_dir(store, name) / "VERDICT.md", content)
    rec["verdict"] = {"conclusion": conclusion, "time": ts}
    add_event(state, "verdict", name, f"实验结论回流：{conclusion_disp}（来源 {src}）")
    refresh(store, state)
    print(f"[cb] 分支 '{name}' 实验结论已记录：{conclusion_disp}，详见 {branch_dir(store, name) / 'VERDICT.md'}")
    print("[cb] verdict 只记录结论，不执行 promote/discard。")
    if conclusion == "promote":
        print(f"[cb] 下一步：与用户确认后执行 python cb.py promote \"{root}\" {name} 提升为新主线。")
    else:
        print(f"[cb] 下一步：与用户确认后执行 python cb.py discard \"{root}\" {name} 舍弃该分支。")


# --------------------------------------------------------------------------- #
# log（统一事件时间线）
# --------------------------------------------------------------------------- #
def ts_to_human(ts: str) -> str:
    try:
        return datetime.strptime(ts, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def derive_legacy_timeline(store: Path) -> list:
    """旧工作区降级：从 CHANGELOG 版本标题行与 archive 目录名推导时间线（时间正序）。"""
    entries = []
    changelog = store / MAIN / "CHANGELOG.md"
    if changelog.is_file():
        for line in read_text(changelog).splitlines():
            m = re.match(r"^##\s+v(\d+)\s+—\s+(.+)$", line)
            if m:
                entries.append(
                    (m.group(2).strip(), "version", MAIN, f"主线更新到 v{m.group(1)}（源自 CHANGELOG.md）")
                )
    archive_dir = store / ARCHIVE
    if archive_dir.is_dir():
        for d in sorted(archive_dir.iterdir()):
            if not d.is_dir():
                continue
            m = re.fullmatch(r"main-v(\d+)-(\d{8}-\d{6})-before-rollback", d.name)
            if m:
                entries.append((ts_to_human(m.group(2)), "rollback", "-",
                                f"回滚前主线 v{m.group(1)} 安全归档（{ARCHIVE}/{d.name}）"))
                continue
            m = re.fullmatch(r"main-v(\d+)-(\d{8}-\d{6})", d.name)
            if m:
                entries.append((ts_to_human(m.group(2)), "promote", "-",
                                f"旧主线 v{m.group(1)} 归档（{ARCHIVE}/{d.name}）"))
                continue
            m = re.fullmatch(r"branch-(.+)-(\d{8}-\d{6})", d.name)
            if m:
                entries.append((ts_to_human(m.group(2)), "discard", m.group(1),
                                f"分支 '{m.group(1)}' 归档离场（{ARCHIVE}/{d.name}）"))
    entries.sort(key=lambda e: e[0])
    return entries


def cmd_log(args):
    root = Path(args.root).resolve()
    store = store_of(root)
    state = load_state(store)
    limit = args.limit
    events = state.get("events") or []
    if events:
        shown = events[-limit:] if limit else events
        print(f"[cb] 工作区事件时间线（共 {len(events)} 条，按时间正序显示最近 {len(shown)} 条）：")
        for ev in shown:
            print(f"{ev.get('time', '-')} [{ev.get('type', '-')}] {ev.get('branch', '-')} — {ev.get('detail', '')}")
        return
    print("[cb] 该工作区创建于旧版本（无事件记录），以下为从 CHANGELOG 与 archive 推导的时间线，仅含主线版本变更与归档动作。")
    entries = derive_legacy_timeline(store)
    if not entries:
        print("[cb] 未推导出任何版本变更或归档记录，暂无可显示的时间线。")
        return
    if limit:
        entries = entries[-limit:]
    for time_s, etype, branch, detail in entries:
        print(f"{time_s} [{etype}] {branch} — {detail}")


def cmd_check(args):
    """只读自检：报告工作区结构问题，不修改任何文件。"""
    root = Path(args.root).resolve()
    store = store_of(root)
    oks, warns, probs = [], [], []

    state = None
    try:
        state = load_state(store)
        if not isinstance(state, dict):
            raise ValueError("顶层不是 JSON 对象")
        missing = [k for k in ("head", "main_version", "branches") if k not in state]
        if missing:
            raise ValueError("缺少必需键：" + "、".join(missing))
        if not isinstance(state.get("branches"), dict):
            raise ValueError("branches 不是对象")
        oks.append(
            f"state.json 可解析（HEAD={state.get('head')}，主线 v{state.get('main_version')}，"
            f"{len(state['branches'])} 条分支记录）"
        )
    except Exception as exc:
        probs.append(f"state.json 无法解析或结构异常：{exc}")
        state = None

    main_dir = store / MAIN
    if (main_dir / "PROMPT.md").is_file():
        oks.append("main/PROMPT.md 存在（唯一事实来源）")
    else:
        probs.append(f"缺少主线规则文件：{MAIN}/PROMPT.md")
    if not (main_dir / "CHANGELOG.md").is_file():
        warns.append(f"缺少 {MAIN}/CHANGELOG.md（主线版本演进记录）")
    for sub in ("inputs", "outputs"):
        if not (main_dir / sub).is_dir():
            warns.append(f"缺少目录 {MAIN}/{sub}/")

    # 符号链接会让词法路径与真实写入位置分离，属于隔离审计中的高风险结构。
    for protected in (store / MAIN, store / BRANCHES, store / ARCHIVE):
        if not protected.exists():
            continue
        for current, dirs, files in os.walk(protected, followlinks=False):
            current_path = Path(current)
            for entry in [*(current_path / d for d in dirs), *(current_path / f for f in files)]:
                if entry.is_symlink():
                    warns.append(f"发现符号链接（请确认不会绕过隔离）：{entry.relative_to(store)}")

    if state is not None:
        version = state.get("schema_version")
        if version is None:
            warns.append(
                f"state.json 无 schema_version（旧格式工作区，下次任何写入会自动补记为 v{STATE_SCHEMA_VERSION}）"
            )
        elif isinstance(version, int) and version > STATE_SCHEMA_VERSION:
            probs.append(
                f"state.json 的 schema_version=v{version} 高于本脚本支持的 v{STATE_SCHEMA_VERSION}："
                f"该工作区由更新版本的 cb.py 写过，请勿用当前脚本继续写入"
            )
        else:
            oks.append(f"state.json 结构版本 v{version}（当前脚本 v{STATE_SCHEMA_VERSION}）")
        head = state.get("head")
        if head == MAIN:
            oks.append("HEAD=main（当前处于主线）")
        elif head in state["branches"]:
            rec = state["branches"][head]
            if rec.get("status") == "testing":
                oks.append(f"HEAD={head}（testing 分支）")
            else:
                probs.append(f"HEAD={head} 指向已离场分支（状态 {rec.get('status')}）")
        else:
            probs.append(f"HEAD={head} 在 state.json 中找不到对应记录")

        for name, rec in sorted(state["branches"].items()):
            b_dir = branch_dir(store, name)
            status = rec.get("status")
            if status == "testing":
                if not b_dir.is_dir():
                    probs.append(f"分支 '{name}' 标记为 testing，但目录不存在：{b_dir}")
                    continue
                if not (b_dir / "PROMPT.md").is_file():
                    probs.append(f"分支 '{name}' 缺少 PROMPT.md")
                if not (b_dir / PARENT_SNAPSHOT).is_file():
                    warns.append(f"分支 '{name}' 缺少父版本快照 {PARENT_SNAPSHOT}，diff 基准会退化")
                if not (b_dir / "DIFF.md").is_file():
                    warns.append(f"分支 '{name}' 尚未生成 DIFF.md（可运行 diff 刷新）")
                fv = rec.get("from_version")
                if isinstance(fv, int) and fv != state.get("main_version"):
                    warns.append(
                        f"分支 '{name}' 基于 v{fv}，落后当前主线 v{state.get('main_version')}"
                        f"（对比前应先确认基准）"
                    )
                if count_files(b_dir / "outputs") == 0:
                    warns.append(f"分支 '{name}' 尚无试跑产物（outputs/ 为空）")
            elif status == "archived":
                loc = rec.get("archive_path")
                if not loc or not (store / loc).is_dir():
                    probs.append(f"分支 '{name}' 记录为已归档，但归档目录不存在：{loc or '（记录缺失）'}")

        promoted_dirs = {
            p.get("branch")
            for p in (state.get("promotions") or [])
            if isinstance(p, dict)
        }
        b_root = store / BRANCHES
        if b_root.is_dir():
            for d in sorted(b_root.iterdir()):
                if not d.is_dir() or d.name in state["branches"]:
                    continue
                if d.name in promoted_dirs:
                    warns.append(
                        f"{BRANCHES}/{d.name} 是已 promote 分支的残留副本（其规则已是当前主线），"
                        f"确认无需保留后可直接删除该目录"
                    )
                else:
                    probs.append(
                        f"发现 state.json 未记录的孤儿分支目录：{BRANCHES}/{d.name}"
                        f"（请按 branch/promote/discard 流程处理，勿手工删除）"
                    )

        for p in state.get("promotions") or []:
            if not isinstance(p, dict):
                continue
            arc = p.get("archive")
            if arc and not (store / arc).is_dir():
                warns.append(f"promote 记录 '{p.get('branch')}' 的旧主线归档缺失：{arc}")

        # STATE.md 头部写着“勿手改”，这里就是那个执行者：与渲染结果逐字对比。
        state_md_path = store / STATE_MD
        if not state_md_path.is_file():
            warns.append(f"缺少 {STATE_MD}（人类可读视图；任何写入都会自动重建）")
        else:
            try:
                if read_text(state_md_path) != render_state_md(store, state):
                    warns.append(
                        f"{STATE_MD} 与 state.json 不一致（被手工编辑或未刷新）："
                        f"以 state.json 为准，跑任意写命令即可重建"
                    )
                else:
                    oks.append(f"{STATE_MD} 与 state.json 完全一致")
            except Exception as exc:
                warns.append(f"无法校验 {STATE_MD}：{exc}")

    if getattr(args, "json", False):
        # JSON 必须先于任何人类可读行打印，否则 stdout 不再是单个 JSON 对象
        print(json.dumps(
            {"root": str(root), "ok": not probs, "info": oks, "warnings": warns, "problems": probs},
            ensure_ascii=False,
        ))
        if probs:
            sys.exit(1)
        return

    print(f"[cb] 工作区自检：{store}")
    for line in oks:
        print(f"  [OK]   {line}")
    for line in warns:
        print(f"  [警告] {line}")
    for line in probs:
        print(f"  [问题] {line}")
    print(f"[cb] 小结：{len(oks)} 项通过，{len(warns)} 项警告，{len(probs)} 项问题。")
    if probs:
        print("[cb] 存在结构性问题：请按上面提示处理（本命令只读，未做任何修改）。")
        sys.exit(1)
    print("[cb] 工作区结构完整。")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Conversation Branch 管理器",
        epilog="所有命令都接受 --json（位置任意）：只输出一个 JSON 对象，供程序/MCP 层读取；目前 status 与 check 已实现。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("demo", help="生成含主线与两个分支的示例工作区，用于快速体验")
    sp.add_argument("target", help="示例工作区创建位置（不应已有 .branches）")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("init", help="初始化分支工作区")
    sp.add_argument("root")
    sp.add_argument("--prompt", help="从已有文件导入主线 PROMPT")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("branch", help="创建实验分支")
    sp.add_argument("root")
    sp.add_argument("name")
    sp.add_argument("--from", dest="from_", default=MAIN, help="来源：main 或另一 testing 分支")
    sp.add_argument("--purpose", default="", help="一句话实验目的")
    sp.add_argument(
        "--no-copy-inputs",
        action="store_true",
        help="不复制来源 inputs/（默认复制，只有明确不需要同源素材时使用）",
    )
    sp.set_defaults(func=cmd_branch)

    sp = sub.add_parser("checkout", help="切换 HEAD")
    sp.add_argument("root")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_checkout)

    sp = sub.add_parser("status", help="查看状态")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("diff", help="生成 PROMPT 差异（父快照/主线/另一分支）")
    sp.add_argument("root")
    sp.add_argument("name", nargs="?")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--against-main", action="store_true", help="对比当前 main 而非父版本快照")
    grp.add_argument("--against", default=None, help="对比另一个 testing 分支名")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("compare", help="多分支横向对比总览，生成 COMPARE.md")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("note", help="更新分支备注/阶段结论")
    sp.add_argument("root")
    sp.add_argument("name")
    sp.add_argument("text", help="备注内容（建议加引号）")
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("rename", help="重命名 testing 分支")
    sp.add_argument("root")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("discard", help="舍弃分支（默认归档；可多个或 --keep 批量）")
    sp.add_argument("root")
    sp.add_argument("names", nargs="*", help="一个或多个分支名")
    sp.add_argument("--keep", default=None, help="保留该赢家分支，归档其余所有 testing 分支")
    sp.add_argument("--purge", action="store_true", help="彻底删除而非归档")
    sp.set_defaults(func=cmd_discard)

    sp = sub.add_parser("promote", help="提升分支为新主线")
    sp.add_argument("root")
    sp.add_argument("name")
    sp.add_argument("--note", default="", help="提升说明（写入 CHANGELOG）")
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("rollback", help="用归档历史主线回滚")
    sp.add_argument("root")
    sp.add_argument("ref", help="版本号(如 1)或 archive 下目录名")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("export", help="导出分支包到独立目录")
    sp.add_argument("root")
    sp.add_argument("name")
    sp.add_argument("--to", required=True, help="导出目标父目录")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("log", help="查看工作区事件时间线（旧工作区自动降级为推导时间线）")
    sp.add_argument("root")
    sp.add_argument("--limit", type=int, default=None, help="只显示最近 N 条（默认全部，时间正序）")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("verdict", help="记录分支实验结论（写入 VERDICT.md，不执行 promote/discard）")
    sp.add_argument("root")
    sp.add_argument("name")
    sp.add_argument("--from", dest="from_file", required=True, help="结论文件路径（极简 key:value 格式）")
    sp.set_defaults(func=cmd_verdict)

    sp = sub.add_parser("check", help="工作区完整性自检（只读，不修改任何文件）")
    sp.add_argument("root")
    sp.set_defaults(func=cmd_check)
    return p


def main():
    force_utf8_streams()
    argv = sys.argv[1:]
    # --json 允许出现在任意位置（含子命令之后）：解析前统一摘掉，省得给每个子解析器都声明一遍
    as_json = "--json" in argv
    if as_json:
        argv = [item for item in argv if item != "--json"]
    args = build_parser().parse_args(argv)
    args.json = as_json
    root = getattr(args, "root", None) or getattr(args, "target", None)
    if root is None:
        args.func(args)
        return
    # 所有命令都在工作区锁内执行，串行化对 state.json 的读-改-写
    with state_lock(Path(root)):
        args.func(args)


if __name__ == "__main__":
    main()
