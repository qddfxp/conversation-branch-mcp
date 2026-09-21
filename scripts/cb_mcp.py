#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal dependency-free MCP stdio adapter for Conversation Branch.

The adapter exposes a fixed allow-list of cb.py commands. It deliberately
invokes the CLI instead of duplicating state transitions, so CLI and MCP share
exactly the same validation and recovery behavior.

stdio 传输用 MCP 规范的帧格式：每条消息是一个单行 JSON，以 \n 分隔
（newline-delimited JSON），不是 LSP 的 Content-Length 头 + 正文。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
TIMEOUT_SECONDS = 120
SCRIPT = Path(__file__).with_name("cb.py")


def schema(properties, required=("root",)):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def text(description):
    return {"type": "string", "description": description}


def flag(description):
    return {"type": "boolean", "description": description}


COMMON_ROOT = text("Conversation Branch 工作区根目录")
BRANCH_NAME = text("实验分支名（只能用小写字母、数字、连字符；main 是保留名）")

INSTRUCTIONS = (
    "Conversation Branch 用 git-branch 式的规则隔离，给长期任务的提示词/工作规范做 A/B 实验。铁律："
    "(1) 一切写入只允许落在 HEAD 对应的目录；HEAD=main 时 main/ 只读，正式任务以 main/PROMPT.md 为唯一事实来源。"
    "(2) 分支试跑必须与主线同源输入，评估维度在试跑前写入分支 NOTES.md，不得事后找补。"
    "(3) 分支只有两种离场方式：discard（归档，主线零改动）或 promote（成为新主线，旧主线自动归档可回滚）。"
    "(4) promote/discard/rollback 必须由用户明确决定，AI 不得自行提升；提升必须给出理由（note）。"
    "(5) 典型流程：cb_status 看全貌 → cb_branch 建分支 → 在 HEAD 分支干活 → cb_diff/cb_verdict 记录 → 用户裁决后 cb_promote 或 cb_discard。"
)


def tool_annotations(title, read_only=False, destructive=False, idempotent=None):
    """MCP 工具注解：客户端据此决定是否弹审批、能否在传输失败后安全重试。

    规范原文（schema.ts / ToolAnnotations）：readOnlyHint 为 true 表示"does not modify its
    environment"；destructiveHint 为 false 表示"performs only additive updates"（默认 true）。
    所以新增文件的写入工具（如 cb_diff / cb_compare / cb_export）应当是 readOnly=false +
    destructive=false，只有会改动主线或删除分支的三个工具才标 destructive=true。
    idempotentHint 仅在 readOnlyHint=false 时有意义，只读工具天然幂等。
    """
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": read_only if idempotent is None else idempotent,
        "openWorldHint": False,
    }


# outputSchema：声明 status/check 的结构化输出，客户端可直接读字段而不用解析中文文本
STATUS_OUTPUT = {
    "type": "object",
    "required": ["root", "head", "main_version", "branches"],
    "properties": {
        "root": {"type": "string"},
        "schema_version": {"type": "integer"},
        "head": {"type": "string"},
        "main_version": {"type": "integer"},
        "updated": {"type": "string"},
        "branches": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "from": {"type": "string"},
                    "from_version": {"type": "integer"},
                    "prompt_changed": {"type": "boolean"},
                    "outputs": {"type": "integer"},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

CHECK_OUTPUT = {
    "type": "object",
    "required": ["root", "ok", "problems"],
    "properties": {
        "root": {"type": "string"},
        "ok": {"type": "boolean"},
        "info": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
}

LOG_OUTPUT = {
    "type": "object",
    "required": ["root", "total", "events"],
    "properties": {
        "root": {"type": "string"},
        "inferred": {"type": "boolean"},
        "total": {"type": "integer"},
        "returned": {"type": "integer"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "string"},
                    "type": {"type": "string"},
                    "branch": {"type": "string"},
                    "detail": {"type": "string"},
                },
            },
        },
    },
}

# 版本单一来源：cb.py 的 __version__（pyproject 也用 dynamic 从这里取），避免包元数据与
# serverInfo 各写一份、时间长了悄悄不一致。依赖方向与运行方式一致——适配器本来就依赖
# 同目录的 cb.py（它会子进程调起它）。
try:
    import cb as _core

    CORE_VERSION = _core.__version__
except Exception:  # 极端情况（文件被改名/裁剪）下不因读不到版本号而拒绝服务
    CORE_VERSION = "0.0.0"


# 单一声明表：哪个工具支持结构化输出、对应哪个 schema，只在这里写一次
OUTPUT_SCHEMAS = {"cb_status": STATUS_OUTPUT, "cb_check": CHECK_OUTPUT, "cb_log": LOG_OUTPUT}


TOOLS = [
    # 注意：cb_status 走 cb.py status，它会就地 refresh 重建 STATE.md 并刷新 updated
    # 时间戳，按规范（does not modify its environment）不能算只读，否则会误导客户端跳过确认。
    {"name": "cb_status", "description": "查看工作区状态（会就地重建 STATE.md 视图与 updated 时间戳，不改分支数据、版本号与 HEAD）",
     "annotations": tool_annotations("查看工作区状态"),
     "inputSchema": schema({"root": COMMON_ROOT})},
    {"name": "cb_check", "description": "体检工作区结构（只读）：报告孤儿分支目录、缺失 PROMPT.md、归档缺失等问题",
     "annotations": tool_annotations("体检工作区结构", read_only=True),
     "inputSchema": schema({"root": COMMON_ROOT})},
    {"name": "cb_log", "description": "查看工作区事件时间线（只读）",
     "annotations": tool_annotations("查看事件时间线", read_only=True),
     "inputSchema": schema({"root": COMMON_ROOT, "limit": {"type": "integer", "minimum": 1, "description": "只看最近 N 条事件"}})},
    {"name": "cb_diff", "description": "生成分支 PROMPT 与父版本（或主线）的规则差异（写入分支 DIFF.md，不改主线）",
     "annotations": tool_annotations("生成规则差异", idempotent=True),
     "inputSchema": schema({"root": COMMON_ROOT, "name": BRANCH_NAME, "against_main": flag("改和主线比，而不是和创建时的父版本比"), "against": text("改和指定分支比")}, required=("root",))},
    {"name": "cb_compare", "description": "生成多分支横向对比（写入 COMPARE.md，不改主线）",
     "annotations": tool_annotations("多分支横向对比", idempotent=True),
     "inputSchema": schema({"root": COMMON_ROOT})},
    {"name": "cb_init", "description": "初始化工作区（在 root 下建立 .branches/ 与主线）",
     "annotations": tool_annotations("初始化工作区"),
     "inputSchema": schema({"root": COMMON_ROOT, "prompt": text("主线 PROMPT.md 的初始内容；省略则生成占位模板")}, required=("root",))},
    {"name": "cb_branch", "description": "创建实验分支，默认复制同源 inputs",
     "annotations": tool_annotations("创建实验分支"),
     "inputSchema": schema({"root": COMMON_ROOT, "name": BRANCH_NAME, "from": text("从哪个分支复制，默认当前 HEAD"), "purpose": text("实验目的/假设，写入 NOTES.md"), "no_copy_inputs": flag("不复制 inputs（默认会复制）")}, required=("root", "name"))},
    {"name": "cb_checkout", "description": "切换 HEAD 到指定分支（实验期间主线只读，HEAD 决定谁可以被改动）",
     "annotations": tool_annotations("切换 HEAD", idempotent=True),
     "inputSchema": schema({"root": COMMON_ROOT, "name": text("目标分支名，或 main")}, required=("root", "name"))},
    {"name": "cb_note", "description": "更新分支 NOTES.md 的备注",
     "annotations": tool_annotations("更新分支备注", idempotent=True),
     "inputSchema": schema({"root": COMMON_ROOT, "name": BRANCH_NAME, "text": text("要写入的备注内容")}, required=("root", "name", "text"))},
    {"name": "cb_rename", "description": "重命名实验分支",
     "annotations": tool_annotations("重命名分支"),
     "inputSchema": schema({"root": COMMON_ROOT, "old": text("原分支名"), "new": text("新分支名")}, required=("root", "old", "new"))},
    {"name": "cb_discard", "description": "舍弃实验分支：names 指定要舍弃的分支，keep 指定保留的赢家（其余全舍弃）；names 与 keep 互斥，必须给其一。默认归档到 archive/，加 purge 则真删目录（不可恢复）。舍弃 HEAD 所在分支时 HEAD 自动回 main",
     "annotations": tool_annotations("舍弃实验分支", destructive=True),
     "inputSchema": schema({"root": COMMON_ROOT, "names": {"type": "array", "items": {"type": "string"}, "description": "要舍弃的分支名列表"}, "keep": text("要保留的分支名，其余分支全部舍弃（与 names 互斥）"), "purge": flag("真删除分支目录而不是归档（不可恢复，需用户明确同意）")}, required=("root",))},
    {"name": "cb_promote", "description": "把实验分支提升为新主线：旧主线归档、版本号 +1（必须由用户明确批准）。note 为必填的提升理由，写入主线 CHANGELOG.md 供事后审计",
     "annotations": tool_annotations("提升分支为新主线", destructive=True),
     "inputSchema": schema({"root": COMMON_ROOT, "name": BRANCH_NAME, "note": text("提升理由（必填）：为什么这个分支更好、依据是什么，写入主线 CHANGELOG.md")}, required=("root", "name", "note"))},
    {"name": "cb_rollback", "description": "把主线回滚到某个归档版本：ref 可以是版本号或 archive/ 下的目录名（必须由用户明确批准）",
     "annotations": tool_annotations("回滚主线到归档版本", destructive=True),
     "inputSchema": schema({"root": COMMON_ROOT, "ref": text("目标版本号（如 1）或 archive/ 下的归档目录名"), "note": text("回滚理由，写入主线 CHANGELOG.md")}, required=("root", "ref"))},
    {"name": "cb_export", "description": "导出分支交接包（不改工作区状态）：生成自带上下文的目录，可交给全新对话测试",
     "annotations": tool_annotations("导出分支交接包"),
     "inputSchema": schema({"root": COMMON_ROOT, "name": BRANCH_NAME, "to": text("导出容器目录（必须已存在），cb.py 会在其中新建 <name>-export-<时间戳>/ 交接包")}, required=("root", "name", "to"))},
    {"name": "cb_verdict", "description": "把外部测试结论回流到分支 VERDICT.md（只记录，不执行 promote/discard）",
     "annotations": tool_annotations("回流实验结论"),
     "inputSchema": schema({"root": COMMON_ROOT, "name": BRANCH_NAME, "from_file": text("结论文本文件路径，内容会被读取并写入该分支 VERDICT.md")}, required=("root", "name", "from_file"))},
]


def result(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


def run_tool(name, args):
    if not isinstance(args, dict):
        return result("arguments 必须是 JSON 对象", True)
    root = args.get("root")
    if not isinstance(root, str) or not root.strip():
        return result("缺少有效的 root", True)
    command = name[3:]
    # status/check 额外拿一份结构化输出：cb.py --json 只打印一个 JSON 对象，便于回填 structuredContent
    json_mode = command in ("status", "check", "log")
    argv = [sys.executable, str(SCRIPT), command, root]
    if json_mode:
        argv.insert(2, "--json")
    if command == "init":
        if args.get("prompt") is not None:
            argv += ["--prompt", str(args["prompt"])]
    elif command == "branch":
        argv += [str(args.get("name", ""))]
        if args.get("from"):
            argv += ["--from", str(args["from"])]
        if args.get("purpose"):
            argv += ["--purpose", str(args["purpose"])]
        if args.get("no_copy_inputs"):
            argv.append("--no-copy-inputs")
    elif command in ("status", "check", "compare"):
        pass
    elif command == "log":
        if args.get("limit") is not None:
            argv += ["--limit", str(args["limit"])]
    elif command == "diff":
        if args.get("name"):
            argv.append(str(args["name"]))
        if args.get("against_main"):
            argv.append("--against-main")
        elif args.get("against"):
            argv += ["--against", str(args["against"])]
    elif command == "checkout":
        argv.append(str(args.get("name", "")))
    elif command == "note":
        argv += [str(args.get("name", "")), str(args.get("text", ""))]
    elif command == "rename":
        argv += [str(args.get("old", "")), str(args.get("new", ""))]
    elif command == "discard":
        argv += [str(x) for x in args.get("names", [])]
        if args.get("keep"):
            argv += ["--keep", str(args["keep"])]
        if args.get("purge"):
            argv.append("--purge")
    elif command == "promote":
        if not str(args.get("note") or "").strip():
            return result(
                "cb_promote 必须提供 note（提升理由）：主线的每一次变更都要留下可审计的理由，"
                "并说明为什么这个分支比当前主线更好。",
                True,
            )
        argv += [str(args.get("name", ""))]
        argv += ["--note", str(args["note"])]
    elif command == "rollback":
        argv += [str(args.get("ref", ""))]
        if args.get("note"):
            argv += ["--note", str(args["note"])]
    elif command == "export":
        argv += [str(args.get("name", "")), "--to", str(args.get("to", ""))]
    elif command == "verdict":
        argv += [str(args.get("name", "")), "--from", str(args.get("from_file", ""))]
    else:
        return result("未知工具", True)
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=TIMEOUT_SECONDS,
        )
    except OSError as exc:
        return result(f"无法启动 cb.py：{exc}", True)
    except subprocess.TimeoutExpired:
        return result(f"cb.py 超过 {TIMEOUT_SECONDS} 秒未返回，已中断。可能是工作区被占用或磁盘卡顿，请稍后重试。", True)
    structured = None
    if json_mode:
        try:
            structured = json.loads(completed.stdout)
        except ValueError:
            structured = None
    text = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    is_error = completed.returncode != 0
    if name == "cb_check" and is_error and "[cb] 错误" not in completed.stderr:
        # check 用 exit 1 表达“发现结构问题”，那是有效的体检结果而不是工具失败；
        # 真正的崩溃/参数错误（stderr 带“[cb] 错误”）仍按 isError 返回。
        is_error = False
    payload = result(text.strip() or "（命令无输出）", is_error)
    if structured is not None:
        # cb.py --json 的 stdout 就是那一个 JSON 对象，因此 content[0] 已经是规范要求的
        # “序列化 JSON 的 text 块”（向后兼容），不要再追加第二份 JSON。
        payload["structuredContent"] = structured
    return payload


def dispatch(message):
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "conversation-branch", "version": CORE_VERSION}, "instructions": INSTRUCTIONS}}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        # outputSchema 统一在这里附加（见 OUTPUT_SCHEMAS），TOOLS 里只写工具本身
        tools = [
            dict(tool, outputSchema=OUTPUT_SCHEMAS[tool["name"]])
            if tool["name"] in OUTPUT_SCHEMAS
            else tool
            for tool in TOOLS
        ]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name", "")
        if not any(t["name"] == name for t in TOOLS):
            payload = result(f"未知工具：{name}", True)
        else:
            payload = run_tool(name, params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def read_message(stream):
    """读一条消息：换行分隔的单行 JSON（不是 Content-Length 帧）。EOF / 空行返回 None。"""
    line = stream.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def write_message(stream, payload):
    """写一条消息：紧凑 JSON + \n（ensure_ascii=False 保留中文；json.dumps 会转义字符串内换行，不会破坏分帧）。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(raw + b"\n")
    stream.flush()


def main():
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except (ValueError, json.JSONDecodeError) as exc:
            write_message(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
            continue
        if message is None:
            return 0
        response = dispatch(message)
        if response is not None:
            write_message(stdout, response)


if __name__ == "__main__":
    raise SystemExit(main())
