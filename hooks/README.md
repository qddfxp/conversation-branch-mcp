# conversation-branch 硬守卫（PreToolUse hook）

## 定位

conversation-branch 技能的隔离铁律（SKILL.md 第 4 节）本质是**提示词约束**——AI "应该"遵守，但没有强制力。本目录的 `guard.py` 是一个**可选的强制层**：通过 ZCode 的 PreToolUse 钩子，把其中两条规则在工具层变成硬拦截——

1. **HEAD 在某分支上时，该工作区 `main/` 只读**（铁律 2）：任何经 Write/Edit/MultiEdit 工具对 `main/` 内文件的写入直接被拒绝。
2. **`state.json` / `STATE.md` 只许 cb.py 改**（铁律 8）：任何 HEAD 状态下，经 Write/Edit/MultiEdit 手改这两个文件都被拒绝。

装不装这个钩子都不影响 skill 本身的功能；它只是把"相信 AI 自律"升级为"工具层兜底"。

## 拦截规则一览

| 场景 | 结果 |
|---|---|
| HEAD = 某分支名，写 `<root>/.branches/main/` 内文件（含 `xxx.md.tmp`） | **deny**（JSON 拦截，说明当前分支与正确做法） |
| 任何 HEAD，写 `<root>/.branches/state.json` 或 `STATE.md`（含 `.tmp` 变体） | **deny** |
| HEAD = main，写 `main/` 内文件 | 放行（主线工作正常） |
| 写 `branches/`、`archive/`、工作区根、工作区外 | 放行 |
| 任何异常（坏 JSON、字段缺失、state.json 损坏等） | 放行并 exit 0（守卫绝不把工作搞瘫） |

实现细节：

- **tmp 视同本体**：cb.py 的 `write_text` 先写 `<目标>.tmp` 再 replace，所以 `PROMPT.md.tmp`、`state.json.tmp`、`STATE.md.tmp` 都按对应正式文件判定。
- **状态文件位置自动发现**：钩子从**被写文件路径**向上逐级查找 `.branches/state.json`（与 cb.py 的 git 式向上发现一致），因此**无需在钩子配置里指定任务路径**；嵌套工作区取最近的一个。
- **放行 = 静默**：放行时不输出任何 JSON、exit 0，完全不介入 ZCode 正常的权限流程（守卫只在拦截时说话；不用 exit 2）。
- **Windows 兼容**：路径统一 `Path.resolve()` + `os.path.normcase` 后比较，大小写/正反斜杠不敏感。

## 安装（ZCode）

### 第 0 步：先自检

```bash
python "C:/Users/a1817/.agents/skills/conversation-branch/hooks/guard.py" --selftest
```

应看到全部场景 `PASS`、末行 `guard selftest: N/N passed`、退出码 0。自检通过再装。

### 第 1 步：写配置

钩子配置放在 ZCode 配置文件的顶层 `hooks` 键下，两个位置任选：

- **用户级**：`~/.zcode/cli/config.json`（即 `C:\Users\a1817\.zcode\cli\config.json`）——对所有工作区生效，推荐（skill 是用户级安装的）。
- **工作区级**：`<repo>/.zcode/config.json`（或 `<repo>/zcode.json`）——只对当前项目生效，可随仓库分享给团队。

配置内容（两处相同；文件里已有 `hooks` 块时把 `events.PreToolUse` 数组项合并进去即可）：

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PreToolUse": [
        {
          "matcher": "Write|Edit|MultiEdit",
          "hooks": [
            {
              "type": "command",
              "command": "python C:/Users/a1817/.agents/skills/conversation-branch/hooks/guard.py"
            }
          ]
        }
      ]
    }
  }
}
```

要点：

- **`"enabled": true` 必须有**。ZCode 的配置文件钩子默认禁用（只有插件钩子才会自动启用 runner），漏了它钩子永远不会跑。
- `matcher` 是**大小写敏感正则**；`Write|Edit|MultiEdit` 同时覆盖 ApplyPatch（ZCode 把 ApplyPatch 别名映射到 Write/Edit）。
- **command 路径用正斜杠**：cmd 与 Git Bash 都认。写成反斜杠 JSON 转义形式 `"python C:\\Users\\a1817\\.agents\\...\\guard.py"`（解析后即 `python C:\Users\...`）在 cmd 下可用，但 ZCode 经 Git Bash 执行钩子时反斜杠会被当转义符吃掉——所以推荐正斜杠，或用下面的 `process` 类型。
- 若 `python` 不在 PATH，把 `python` 换成 python.exe 的绝对路径或 `py`。
- 更稳的免 shell 写法（钩子文档推荐的跨平台方式，不经 shell、无转义问题）：

  ```json
  { "type": "process", "command": "python", "args": ["C:/Users/a1817/.agents/skills/conversation-branch/hooks/guard.py"] }
  ```

### 第 2 步：验证安装

手动喂一份钩子输入（HEAD 在分支上时应输出 deny JSON；否则无输出且退出码 0）：

```bash
echo '{"tool_name":"Write","tool_input":{"file_path":"<任务root>/.branches/main/PROMPT.md"}}' \
  | python "C:/Users/a1817/.agents/skills/conversation-branch/hooks/guard.py"
```

会话内验证可看 ZCode 日志里的 hook run records（source/matcher/outcome/duration/错误预览），确认钩子被触发且无失败。

## 卸载

删掉配置文件里对应的 `hooks` 块（或整块、或只删 `PreToolUse` 那一项、或把 `hooks.enabled` 改为 `false`），保存后重启会话即可。`hooks/` 目录留着不碍事。

## 已知局限（如实告知）

1. **只拦 Write/Edit/MultiEdit 工具调用，拦不住 Bash 里的文件操作**：`echo x > .branches/main/PROMPT.md`、`sed -i`、`mv/cp/rm` 等 shell 命令不经过本钩子。铁律 8（不绕过脚本）以及 Bash 侧的 main 只读，仍靠 SKILL.md 的提示词约束与 AI 自律。
2. 只强制 SKILL.md 点名的两条：**分支期 `main/` 只读**、**状态文件勿手改**。跨分支写入（HEAD=A 时改 `branches/B/`）不在本守卫范围。
3. 相对路径的 `file_path` 会相对钩子进程 cwd 解析（通常即会话 cwd）；ZCode 的 Write/Edit 正常传绝对路径，实际影响很小。
4. 钩子同步执行，每次 Write/Edit 多一次 Python 启动开销（Windows 上约几十到一两百毫秒）；介意可只在单个工作区级配置中启用。
