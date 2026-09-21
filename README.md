# conversation-branch-mcp（E 盘独立副本）

Conversation Branch 的 MCP 适配层独立副本，零第三方依赖，只用 Python 3.8+ 标准库。

完整规则与工作流说明见 `SKILL.md`；MCP 专用说明见 `MCP.md`。

## 目录

```
E:/conversation-branch-mcp/
├── README.md                 本文件
├── MCP.md                    MCP 配置与工具清单
├── SKILL.md                  技能完整说明（工作流 + 隔离铁律）
├── references/
│   └── evaluation.md         A/B 对比评估方法
├── scripts/
│   ├── cb_mcp.py             MCP stdio 服务入口
│   └── cb.py                 核心管理器（MCP 与 CLI 共用）
├── hooks/
│   ├── guard.py              可选 PreToolUse 硬守卫
│   └── README.md             守卫安装与局限说明
└── tests/
    └── test_cb.py            标准库回归测试
```

`cb_mcp.py` 与 `cb.py` 必须在同一目录：适配器按自身路径定位 `cb.py`。

## MCP 客户端配置

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

`python` 不在 PATH 时换成解释器绝对路径。

## 命令行直接使用

```bash
# 初始化一个长期任务工作区
python "E:/conversation-branch-mcp/scripts/cb.py" init "D:/long-task"

# 开实验分支（默认复制同源 inputs/）
python "E:/conversation-branch-mcp/scripts/cb.py" branch "D:/long-task" my-experiment --purpose "一句话目的"

# 查看状态 / 自检
python "E:/conversation-branch-mcp/scripts/cb.py" status "D:/long-task"
python "E:/conversation-branch-mcp/scripts/cb.py" check  "D:/long-task"
```

## 自检与测试

```bash
python "E:/conversation-branch-mcp/hooks/guard.py" --selftest        # 期望 29/29 passed
python -m unittest discover -s "E:/conversation-branch-mcp/tests" -v # 期望 4/4 OK
```

## 边界

- MCP 只暴露白名单工具，不执行任意 shell 命令。
- MCP 不是文件系统沙箱；hook 也拦不住 Bash 里的重定向、`mv`、`cp`、`rm`。
- `promote` / `discard` / `rollback` 属于正式主线变更，始终需要用户明确决定。

## 安装

两种用法都长期有效，**按需选一种即可**。

**A. 克隆即用（零安装，推荐先试）**

    git clone https://github.com/qddfxp/conversation-branch-mcp
    python scripts/cb.py demo /tmp/cb-demo    # 生成带主线与两个分支的示例工作区
    python scripts/cb.py --help

**B. 装成命令行工具（`pip` / `pipx`）**

    pipx install .        # 或：pip install .
    cb --help
    cb demo /tmp/cb-demo
    cb-mcp                # MCP stdio 服务器，供客户端以 stdio 方式拉起

装出来两个入口：`cb` 是 CLI，`cb-mcp` 是 MCP 服务器；零运行时依赖，`requires-python >= 3.8`。
安装**不移动仓库里的任何文件**，所以 A 里的 `scripts/cb.py` 路径永远可用，两种用法的文档与配置可以混着写。

MCP 客户端两种写法都行（详见 [MCP.md](MCP.md)）：

    {"command": "cb-mcp", "args": []}
    {"command": "python", "args": ["/绝对路径/scripts/cb_mcp.py"]}

## 授权

Apache License 2.0，见 [LICENSE](LICENSE)。商用、修改、再分发都允许，需保留版权与许可声明；
本项目按 Apache-2.0 的默认条款提供，不含额外附加条款。
