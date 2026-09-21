# Conversation Branch MCP

`scripts/cb_mcp.py` 是一个零第三方依赖的 MCP stdio 适配器。它复用 `scripts/cb.py`，所以 CLI 和 MCP 共享同一套校验、版本记录和恢复逻辑。

## 启动配置

将下面命令配置到 MCP 客户端：

```json
{
  "mcpServers": {
    "conversation-branch": {
      "command": "python",
      "args": [
        "E:/conversation-branch-mcp/scripts/cb_mcp.py"
      ]
    }
  }
}
```

Windows 如果 `python` 不在 PATH，把 `command` 换成 Python 可执行文件的绝对路径。

## 工具范围

只暴露 Conversation Branch 的白名单命令：

- 完全只读（不改任何文件）：`cb_check`、`cb_log`
- 生成报告/视图（只写自己的报告文件，不动分支数据与版本号）：`cb_status`（会就地重建 `STATE.md` 视图）、`cb_diff`、`cb_compare`
- 工作区：`cb_init`、`cb_branch`、`cb_checkout`、`cb_note`、`cb_rename`
- 实验生命周期：`cb_discard`、`cb_promote`、`cb_rollback`
- 交接：`cb_export`、`cb_verdict`

MCP 工具不会执行任意 shell 命令，也不会直接接受任意 `cb.py` 子命令。`cb_promote`、`cb_discard`、`cb_rollback` 仍然需要用户明确决定；MCP 只是调用入口，不改变技能的审批规则。schema 里每个工具都显式声明了 `required` 字段（`root` 恒为必填，写操作再要求 `name`/`ref`/`to`/`from_file` 等），并设置了 `additionalProperties: false`。

## 手工烟测

stdio 传输用的是 MCP 规范的帧格式：**每条消息一个单行 JSON，以 `\n` 分隔（newline-delimited JSON）**，不是 LSP 那种 `Content-Length: N\r\n\r\n` 头 + 正文。手工发一条 `initialize`、一条 `tools/list` 和一条 `ping`：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"ping"}' \
  | python scripts/cb_mcp.py
```

预期看到 3 行回复（通知不回），`tools/list` 返回 15 个工具，`ping` 返回空结果 `{}`。支持的协议版本为 `2025-06-18`（默认）、`2025-03-26`、`2024-11-05`；客户端请求的版本在列表内就回显该版本，否则回最新版。

不要把日志写到 stdout；当前适配器的业务结果作为 MCP tool result 返回。`cb.py` 子进程有 120 秒超时，超时后返回明确的 `isError` 文本而不是挂死。CLI 与 MCP 并发调用同一工作区时，`cb.py` 用 `.branches/state.lock` 互斥锁串行化对 `state.json` 的写入（持锁进程崩溃后 30 秒陈旧锁自动强拆）；`cb_check` 发现结构问题以 exit 1 表达，MCP 层会把它作为正常体检结果返回，而不是 `isError`。

## 设计边界

MCP 解决的是工具发现、参数结构化和客户端集成，不是文件系统安全沙箱。建议同时启用 `hooks/guard.py`，并定期运行：

```bash
python scripts/cb.py check <root>
python hooks/guard.py --selftest
```
