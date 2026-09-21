---
name: conversation-branch
description: "当用户要修改长期任务的提示词或工作规则、又不想影响当前稳定对话时使用：为靠一套固定提示词持续产出的长期任务（会议纪要、日报/周报、审稿、周期性数据整理、固定格式文案等）提供 git-branch 式分支实验——主线规则锁定为唯一权威，每次改动先开独立分支、用同源素材 A/B 试跑，支持无限分支并行对比；效果差一键舍弃且主线字节级零改动，效果好一键提升为新主线，旧版自动归档、任意版本可回滚。也可把某个分支导出成独立交接包（export），交给全新对话做不受本对话历史影响的干净上下文测试，再把外部结论回流并留痕（verdict）；另提供只读完整性体检（check）与操作事件时间线（log）。触发说法：开个分支/branch 试一版、换套提示词或规则但别影响现在的对话、两个方案并行比一下哪个好、不行就丢弃好了就替换成现在的规则、回到之前版本、长期任务提示词 A/B 试验、导出到新对话测一版、把外面的测试结论回流、检查一下分支工作区有没有问题、看看这个任务做过哪些分支操作。"
---

# Conversation Branch：长期对话的分支实验

## 1. 心智模型（先理解再动手）

聊天窗口本身无法被程序分叉，本技能换一个等价实现：**把长期对话的"灵魂"——任务目标、长期提示词、产出规范——从易失的聊天历史中外置为版本化文件，再像 git branch 一样管理它们。**

- `main`（主线）：当前正式生效的规则，唯一事实来源，正式产物只从主线出。
- `branch`（分支）：一次规则改动实验，拥有独立目录，随便改、随便试，碰不到主线。
- 实验只有两种离场：`discard`（效果差，归档/删除，**主线字节级不变**）或 `promote`（效果好，成为新主线，旧主线完整归档，可 `rollback`）。
- "不污染原对话"靠三层保证：**物理隔离**（各分支独立目录，脚本保证 main 在实验期间只读）、**事实锚定**（主线工作只认 `main/PROMPT.md`，聊天里试过的实验措辞不回流）、**全程留痕**（STATE/CHANGELOG/archive 可审计、可回滚）。

典型场景：一个长期负责"整理会议纪要"的对话，前期用大量提示词沉淀了稳定规则；现在想调整规则但不确定新规则产出更好还是更差——开分支用同一份会议素材各跑一遍对比，好则提升、差则丢弃，主线工作始终不受影响。

## 2. 工作区结构

脚本 `scripts/cb.py` 在用户指定的**任务根目录** `root`（一项长期任务对应一个 root，默认用当前工作目录）下创建；初始化后可在该目录的任意子层级执行任何子命令，脚本会像 git 一样自动向上找到所属工作区。

```
<root>/.branches/
├── state.json              # 权威状态（机器可读，勿手改）
├── STATE.md                # 状态视图（每次操作自动重写；开工前先读它）
├── COMPARE.md              # 多分支横向对比总览（compare 自动生成，可随时刷新）
├── main/                   # 主线
│   ├── PROMPT.md           # 主线长期规则（唯一事实来源）
│   ├── CHANGELOG.md        # 主线版本演进（promote/rollback 自动追加）
│   ├── inputs/             # 基准测试素材
│   └── outputs/            # 主线正式产物
├── branches/<name>/        # 每个实验分支一个独立目录（数量无上限）
│   ├── PROMPT.md           # 本分支的候选规则（只改这里）
│   ├── .parent_prompt.md   # 创建时父版本快照（diff 基准，勿手改）
│   ├── DIFF.md             # 与父版本的规则差异（自动生成）
│   ├── NOTES.md            # 实验假设/评估维度/结论（试跑前先填）
│   ├── VERDICT.md          # 实验结论回流（verdict 写入，结论区可先由外部测试填写）
│   ├── inputs/             # 同源测试素材（默认自动复制；可用 --no-copy-inputs 关闭）
│   └── outputs/            # 分支试跑产物
└── archive/                # 一切被替换物的归档：旧主线、被舍弃分支
```

## 3. 标准工作流

### 命令速查（全部子命令；均为 `python scripts/cb.py <子命令> …`）

| 阶段 | 子命令 | 作用 |
|---|---|---|
| 入门 | `demo <目标目录>` | 生成主线+两候选的示例工作区，可整目录删除 |
| 固化 | `init <root> [--prompt F]` | 初始化工作区，把长期规则固化进 main/PROMPT.md |
| 开分支 | `branch <root> <name> [--from SRC] [--purpose TXT] [--no-copy-inputs]` | 创建实验分支并切过去（默认随分支复制同源素材） |
| 试跑 | `checkout <root> main\|name`、`status <root>` | 切换 HEAD / 看分支表与落后标记 |
| 试跑 | `note <root> <name> TXT`、`rename <root> <旧> <新>` | 记阶段结论 / 改名（HEAD 自动跟随） |
| 看差异 | `diff <root> [name] [--against-main\|--against B]` | 与父快照 / 当前主线 / 另一分支比 PROMPT |
| 评估 | `compare <root>` | 生成 COMPARE.md 多分支横向总览 |
| 评估 | `export <root> <name> --to DIR`、`verdict <root> <name> --from F` | 导出干净上下文交接包 / 回流外部结论 |
| 离场 | `discard <root> <name…> [--keep WIN] [--purge]` | 舍弃分支（默认归档留痕，主线零改动） |
| 离场 | `promote <root> <name> [--note TXT]`、`rollback <root> <版本号\|归档名> [--note]` | 提升为新主线（分支 PROMPT 与创建时完全相同则必须给 `--note`）/ 回滚旧主线 |
| 维护 | `log <root> [--limit N]`、`check <root>` | 事件时间线 / 工作区完整性自检（只读，不改任何文件） |

### 阶段 -1｜先体验（可选，面向第一次接触本技能的用户）

不确定这套机制是否合适时，先生成一个"会议纪要规则二选一"的完整示例沙盘（主线 + 两个候选分支 + 已生成的差异与对比总览），照输出里的体验路线走一遍即可，整个目录可随时删除、不触碰任何真实任务：

```bash
python scripts/cb.py demo <目标目录>
```

### 阶段 0｜首次固化（只做一次）

长期任务第一次使用时执行初始化，然后把散落在聊天历史里的长期提示词、规则、格式要求**完整、结构化地**整理进 `main/PROMPT.md`：

```bash
python scripts/cb.py init <root>                      # 生成结构与 PROMPT 模板
python scripts/cb.py init <root> --prompt <已有规则文件>  # 或直接导入现成规则
```

整理主线规则时主动与用户核对，确保它等价于当前对话里实际生效的那套逻辑；固化后告知用户："此后正式任务以这份文件为准。"

### 阶段 1｜开分支

用户提出规则改动想法时，先帮他把"实验目的（预期解决什么）"说清楚，再开分支：

```bash
python scripts/cb.py branch <root> <name> --purpose "一句话目的"
```

- `name`：小写字母/数字/连字符/下划线，1-40 字符，首字符须为字母或数字，且不得叫 `main`/`branches`/`archive`，如 `add-decision-context`；默认会把 `--from` 来源目录（默认 `main`）的 `inputs/` 素材复制进分支，**保证同输入对比**。只有明确不需要同源素材时才使用 `--no-copy-inputs`。
- 开完后 HEAD 自动指向新分支。向用户明示："已进入分支 \<name\>，以下试跑都不会影响主线。"
- **分支数量无上限**：多候选方案就开多个分支（如 `style-minimal`、`style-detailed`），并行试跑后用 compare 横向比较；也可用 `--from <另一分支>` 在候选之上继续分叉。名字起得不合适用 `rename` 修改：

```bash
python scripts/cb.py rename <root> <旧名> <新名>   # 重命名 testing 分支，HEAD 会自动跟随
```

### 阶段 2｜分支内试跑（严格隔离）

1. 先读 `STATE.md` 确认 HEAD 是该分支（状态可疑时先跑 `python scripts/cb.py check <root>` 做一次只读自检）；只修改该分支的 `PROMPT.md`，只向该分支的 `outputs/` 写产物。
2. **试跑前**在 `NOTES.md` 写清评估维度（不要跑完再找标准）；阶段性判断可用 note 记到状态表里，随时可见：
   ```bash
   python scripts/cb.py note <root> <name> "阶段结论，如：信息更全但偏长"
   ```
3. 用与主线**同源**的输入素材，按候选规则产出，写入分支 `outputs/`。
4. 随时刷新规则差异并讲给用户：

```bash
python scripts/cb.py diff <root> [name]                # 对比创建时父版本（省略 name 即当前 HEAD 分支；HEAD=main 时必须显式给分支名）
python scripts/cb.py diff <root> [name] --against-main # 对比当前主线
python scripts/cb.py diff <root> <A> --against <B>     # 两个分支互相对比
python scripts/cb.py checkout <root> main|name         # 在主线/各分支间切换焦点
python scripts/cb.py status <root>                     # 查看全貌（含备注、落后主线标记）
```

要看 MCP 适配层的手工烟测（换行分隔 JSON 帧）见 `MCP.md`；不要把它和 `cb.py` 的业务子命令混在一起跑。

### 阶段 3｜对比评估

把"主线旧产物（baseline）"与"分支试跑产物"并排放，按 `NOTES.md` 预先定好的维度逐项评判，输出对比结论与**建议**（提升/持平/变差），但决定权交给用户。严谨的对比方法（同源多素材、盲评、防自利偏差、一票否决项）见 `references/evaluation.md`，做正式 A/B 判断前必须读。

**多分支横向 PK（候选超过一个时的标准动作）：**

```bash
python scripts/cb.py compare <root>          # 生成 COMPARE.md：改动量/产物数/备注/是否落后主线总览
python scripts/cb.py diff <root> <A> --against <B>   # 对总览里接近的两个候选细看差异
```

读 `COMPARE.md` 后结合各分支 `outputs/` 产物给排名建议。注意被标"落后(vN→vM)"的分支基于更早主线：不要直接把它当最终候选，要么从当前主线重开，要么先人工确认主线新增内容已在该分支补齐（本技能不做自动文本合并，避免悄悄丢规则）。

### 阶段 3b｜跨对话测试与结论回流（要"干净上下文"时用）

`export` 会把分支的 PROMPT、同源素材、试跑产物，连同 `VERDICT.md`（已预填分支名、来源主线版本、实验目的与评估维度，只有结论区留空待填）与 `HANDOFF.md` 打成独立目录，交给一个全新对话窗口做不受本对话历史影响的测试：

```bash
python scripts/cb.py export <root> <name> --to <已存在的目录>
```

外部测试完成后，在 `VERDICT.md` 的结论区填写结论（`conclusion:` 必填，只认"提升/promote"或"discard/变差/舍弃"；留空会被拒绝，防止未评估就回流），再回到本对话执行：

```bash
python scripts/cb.py verdict <root> <name> --from <VERDICT.md 路径>
```

`verdict` 只把结论写入分支的 `VERDICT.md` 与状态表（`status` 会显示"【结论:…】"），**不执行 promote/discard**；随后仍由用户拍板走阶段 4a / 4b。全部结论可在 `python scripts/cb.py log <root>` 的时间线里追溯。

### 阶段 4a｜效果差：舍弃（主线零改动）

```bash
python scripts/cb.py discard <root> <name>                # 单个舍弃，默认移入 archive/ 留痕
python scripts/cb.py discard <root> <a> <b>               # 一次舍弃多个落选分支
python scripts/cb.py discard <root> --keep <赢家>         # 批量：保留赢家，其余 testing 分支全部归档
python scripts/cb.py discard <root> <name> --purge        # 用户明确要求彻底删除时
```

舍弃后若 HEAD 落在被删分支上会自动回 main。向用户确认：主线规则与正式产物全程未被触碰，之后继续按原主线工作。

**清理落选分支的推荐顺序**：先 `discard <root> --keep <赢家>` 归档全部落选分支，再 `promote <root> <赢家>`。因为 promote 会把赢家分支目录本身收走（它的规则已成为新主线，写入状态表的"赢家记录"）；若已经先 promote 了，用 `discard <root> --keep <赢家名>` 脚本仍能凭 promote 记录认出该名字并归档其余分支，也可以直接逐个列出落选分支名。两条注意：**`--keep` 的名字既不是 testing 分支又没有 promote 记录时，脚本会直接报错中止、不清理任何分支**，所以先用 `status` 核对名字；`--keep` 与显式分支名列表不能同时使用。另外，若某分支记录的实验结论是 promote 却要 discard，脚本会打印警告，属误操作应停手。

### 阶段 4b｜效果好：提升为新主线（必须用户明确拍板）

```bash
python scripts/cb.py promote <root> <name> --note "为什么提升、好在哪里"
```

脚本自动完成：旧主线整体归档到 `archive/main-vN-时间戳/` → 分支规则成为新 `main/PROMPT.md`（版本号 +1）→ 分支 `inputs/` 并入主线 `inputs/`、试跑产物复制到 `main/outputs/from-branch-<name>/` → CHANGELOG 追加差异与原因（含分支实验结论）→ 分支目录清除、HEAD 回 main、其他在测分支保留。

**promote 后的对话层动作（不可省略）：**
1. 立即重读新的 `main/PROMPT.md`；
2. 向用户复述新主线相对旧主线的变化点，声明"此后正式任务按新主线执行"；
3. 告知旧主线位置与一键回滚命令（CHANGELOG 里已自动写明）。

脚本同时把本次提升写入状态表的"赢家记录"（分支名、新版本号、旧主线归档位置，`status` 可见）；promote 中途失败会自动恢复到操作前状态并提示先跑 `check` 复核。

### 回滚与导出（按需）

```bash
python scripts/cb.py rollback <root> <版本号或archive目录名> --note "原因"
# 版本号单调递增、历史不倒转：回滚后内容等价旧版，但会得到新版本号，回滚前状态也会安全归档

python scripts/cb.py export <root> <name> --to <已存在的父目录>
# 导出分支交接包：PROMPT + NOTES + DIFF + 素材 + 产物 + VERDICT.md（结论回执，结论区留空待填）+ HANDOFF.md
# 用户可拿到一个全新对话窗口做"干净上下文"测试（--to 目录必须已存在，否则报错退出）
```

## 4. AI 隔离铁律（硬性，逐条遵守）

1. **入口先读状态**：任务根目录存在 `.branches/` 时，每次动手前先读 `STATE.md` 确认 HEAD 与主线版本。
2. **写入边界 = HEAD**：一切文件写入只允许发生在 HEAD 对应目录；严禁改其他分支、严禁在分支测试期间写 `main/`（promote/rollback 由脚本完成，不要手工搬改主线）。
3. **主线唯一事实来源**：HEAD=main 处理正式任务时，只以 `main/PROMPT.md` 为准；聊天历史中出现过的分支实验性说法一律不构成主线规则，即使它们在对话里出现过。
4. **正式产物只从 main 出**：HEAD=main 时产出不得引用 `branches/` 下的实验内容。
5. **promote/discard 只能由用户明确决定**：AI 只给对比结论和建议，不得因为"看起来更好"自行提升；用户表态模糊时先问清。
6. **切换必声明**：每次 checkout 后用一句话告诉用户当前在主线还是哪个分支，避免双方迷失。
7. **同源对比**：评估必须用相同输入，评估维度试跑前写入 NOTES；不为了证明分支好而换更容易的素材或事后改标准。
8. **不绕过脚本**：分支的创建、切换、对比、备注、重命名、舍弃、提升、回滚一律走 `cb.py`，不要手动新建/重命名/删除 `.branches` 内部目录；脚本会拒绝非法操作并给出原因。
9. **脚本报错先读信息**：分支名非法、重名、已归档、未初始化等都会非零退出并说明原因，按提示修正，不要强行操作文件系统绕过；工作区状态可疑（缺文件、孤儿目录、HEAD 对不上）时先跑 `python scripts/cb.py check <root>` 做只读体检，再按提示处理。

## 5. 边界与限制（如实告知用户）

- 本技能不复制聊天窗口、不创建界面分叉，也不删除/改写既有聊天记录；它隔离的是**任务规则、文件副作用与工作事实来源**，并让每次规则变更可对比、可回滚。
- 若用户希望连"聊天上下文"也完全干净地测试，用 `export` 导出交接包，到全新对话窗口中测试，再回本对话做 promote/discard。
- `.branches/` 是任务状态目录，提醒用户不要随意手动移动或改名；整个任务目录可以正常拷贝/备份/同步。
- 一项长期任务用一个 root；多项长期任务分别在各自根目录初始化，互不干扰。
- 运行环境：仅依赖 Python 3.8+ 标准库，零第三方依赖、全程本地离线运行（规则与产物不出本机）；示例中的 `python` 在 macOS/Linux 上若不可用请改用 `python3`。
- `check` 只报告、不修复：它列出的是结构层面的不一致（缺 PROMPT、孤儿分支目录、归档缺失、基准落后等），修复动作仍按脚本流程由用户与 AI 共同完成。
- MCP 适配层是本地 stdio 薄封装：它只调用 `cb.py` 的白名单子命令，不替代 CLI，也不接受任意 shell 命令；启动方式为 `python scripts/cb_mcp.py`，传输用换行分隔 JSON（不是 `Content-Length` 帧），支持 `ping`，并会回显受支持的协议版本。

## CLI 与 MCP 工具名映射

MCP 那层没有另一套语义，它只是"白名单 + 参数拼装"：工具名去掉 `cb_` 前缀就是子命令，
根目录永远作为第一个位置参数。所以下面的映射是机械规则，不是手工维护的表：

| MCP 工具 | CLI 等价写法 |
| --- | --- |
| `cb_init` | `cb.py init <root> [--prompt F]` |
| `cb_status` | `cb.py status <root>` |
| `cb_check` | `cb.py check <root>` |
| `cb_log` | `cb.py log <root> [--limit N]` |
| `cb_branch` | `cb.py branch <root> <name> [--from F] [--purpose T] [--no-copy-inputs]` |
| `cb_checkout` | `cb.py checkout <root> <name>` |
| `cb_note` | `cb.py note <root> <name> <text>` |
| `cb_rename` | `cb.py rename <root> <old> <new>` |
| `cb_diff` | `cb.py diff <root> [name] [--against F] [--against-main]` |
| `cb_compare` | `cb.py compare <root>` |
| `cb_verdict` | `cb.py verdict <root> <name> --from <file>` |
| `cb_export` | `cb.py export <root> <name> --to <dir>` |
| `cb_promote` | `cb.py promote <root> <name> --note <理由>` |
| `cb_discard` | `cb.py discard <root> <name...> [--keep K] [--purge]` |
| `cb_rollback` | `cb.py rollback <root> <ref> [--note 理由]` |

两点刻意保留的差异：

- **`cb_promote` 强制要求 `note`**（schema `required` + 服务端硬校验，缺了直接返回 `isError` 而不调用 `cb.py`）。
  CLI 只在你省略 `--note` 且 PROMPT 未变时才报错。主线的每一次变更都要留下可审计的理由。
- **结构化输出只有 `cb_status` / `cb_check` / `cb_log`**（`--json` + `outputSchema`）。
  `diff` / `compare` / `export` 的产物本身就是报告，包一层 JSON 只是换个容器，故仍是文本。

无论走哪条入口，`promote` / `discard` / `rollback` 都应由用户拍板——MCP 那层不会替你决定主线。
