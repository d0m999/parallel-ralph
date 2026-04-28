# parallel-ralph

[![test](https://github.com/d0m999/parallel-ralph/actions/workflows/test.yml/badge.svg)](https://github.com/d0m999/parallel-ralph/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

> English version: [README.md](./README.md)

一个在 [Claude Code](https://claude.com/claude-code) 下**并行运行 N 个互不相交的
[ralph](https://ghuntley.com/ralph/) 循环**的 harness，包含：

- **集合论分片校验** — 从基线 manifest 渲染 N 个分片，审计两两不相交 +
  并集等于全集，任何漂移都 fail-stop。切分方式可以是自动平均、用
  `--splits "1-13,14-26,..."` 显式指定，或者从 JSON 配置文件加载。
- **可插拔的 acceptance gate** — `JsonlSchemaGate`（参数化的 5-gate 标注校验）、
  `CommandGate`（任意 shell 命令，exit 0 即 PASS）、`CompositeGate`（多 gate 与
  逻辑）。可在 `prd.json` 中按 story 通过 `acceptanceGate` 覆盖，或通过
  `acceptance.default_gate` 设置项目级默认。
- **写边界 `PreToolUse` hook** — 用环境变量 `RALPH_SHARD_ROOT` 启用，强制
  subagent 不能跑出自己的分片目录；项目级额外禁止前缀通过
  `RALPH_HARD_DENY_PREFIXES` 配置。单进程模式下是 no-op。
- **`<promise>YIELD/COMPLETE/VIOLATION</promise>` token 协议** — 与
  `prd.json` 交叉校验，防止假冒"已完成"。
- **流式写入** — 每条 verdict 单独 fsync，第 25 分钟崩溃也不会丢第 1 分钟的
  工作；schema 由 `append_verdict.py` 强制校验。
- **优雅降级（DEGRADE 路径）** — 分片死掉时，把它未做完的任务重新分发给
  存活分片，或者整体回退到单进程基线。
- **按原因分类的 monitor** — rate-limit / dirty-tree 失败自动重启；
  无法分类的失败发警报并停下来。
- **原子状态卫生** — 临时文件 + rename 写、PID + liveness 实例锁、
  陈旧锁回收、symlink 幂等重建。

这套 harness 最初是为了并行驱动数千次 LLM-as-judge 分类而写的，但 ralph 本身
也是一个**代码实现循环**：写入范围互不相交、acceptance gate 可确定的
story，可以由 N 个并行的 Claude Code 分片各自实现。可以看
[examples/openapi-impl/](examples/openapi-impl/) 这个 demo。

---

## 目录结构

```
parallel-ralph/
├── ralph.sh                                  # 主循环（单进程 / 分片）
├── .ralph/                                   # 基线状态（.gitignore）
│   └── scripts/
│       ├── acceptance.py                     # gate 插件接口
│       ├── run_batch.py                      # prepare / validate / finalize
│       └── append_verdict.py                 # 流式 append 助手
├── scripts_4x/
│   ├── render_shards.py                      # 把基线切成 N 个分片
│   ├── audit_shards.py                       # 5 层正确性审计
│   ├── merge_shards.py                       # 合并分片输出 + 审计
│   ├── monitor_shards.py                     # 原因分类 + 自动重启
│   ├── redistribute_remaining.py             # DEGRADE 路径
│   ├── recover_shards_after_limit.sh         # rate-limit 后的恢复
│   ├── run_shards.sh                         # 启动 N 个分片
│   ├── stop_shards.sh                        # 优雅停止
│   ├── dashboard.sh                          # ASCII 进度看板
│   ├── PROMPT.md.tmpl                        # operator prompt 模板
│   └── hooks/
│       └── deny_outside_shard.py             # PreToolUse 写边界 hook
├── examples/
│   ├── sample-jsonl/                         # JsonlSchemaGate demo
│   └── openapi-impl/                         # CommandGate demo
├── tests/                                    # 50+ unit/integration 测试
├── LICENSE                                   # MIT
├── pyproject.toml                            # stdlib 运行时，pytest dev
└── README.md
```

---

## 安装

**前置条件**

- Python **3.10+**
- 安装并完成认证的 [Claude Code](https://claude.com/claude-code)
  （`claude` 在 `$PATH`；`npm install -g @anthropic-ai/claude-code`）
- 一个 git 仓库（循环里会做 commit + 切分支）
- macOS 或 Linux（shell 工具假设了 BSD / GNU coreutils）

harness 本身是**纯标准库**——运行时没有任何第三方 Python 依赖。
`pytest` 和 `ruff` 仅在开发时需要。

**安装步骤**

```bash
git clone https://github.com/d0m999/parallel-ralph.git
cd parallel-ralph
pip install -e '.[dev]'      # 只在你想跑 pytest / ruff 时需要
pytest -q                    # smoke test（应输出 55 passed）
```

**先跑一个 demo 再把自己的任务接进来**

```bash
./examples/sample-jsonl/init.sh   # JsonlSchemaGate demo（情感分类）
./ralph.sh                         # 单进程循环

# 或者代码实现 demo（CommandGate 跑 pytest）：
./examples/openapi-impl/init.sh
./ralph.sh
```

---

## 工作流

### 概念模型

harness 是一个**按 story 推进的循环**：

1. ralph.sh 从 `prd.json` 里挑 `passes: false` 中优先级最高的 story，
   锁进 `current_story.json`。
2. 派发一个全新的 Claude Code 实例，prompt 指向锁定的 story 加三条
   plumbing 命令：`prepare` →（subagent 干实际的活）→ `validate` →
   `finalize`。
3. `validate` 跑配置的 acceptance gate（JSONL schema 校验、`pytest` 等）。
   PASS 时 `finalize` 把 `passes` 翻成 `true`、向 `progress.txt` 追加一行、
   输出 `<promise>YIELD</promise>`（或在没剩余 story 时输出
   `<promise>COMPLETE</promise>`）。
4. 循环带着干净 context 进入下一轮。两轮之间能保留的"记忆"只有：git
   history、`progress.txt`、`prd.json`（哪些 story 完成了）。

### 单进程

适用场景：
- 只有几十个左右的小 story，
- 或者还在调试——先把单进程跑通再上并行。

```bash
# 0. 准备基线（或者跑某个 example 的 init.sh）
./examples/sample-jsonl/init.sh

# 1. 跑循环
./ralph.sh                                    # 默认：200 轮，每个 story 最多 15 次重试
./ralph.sh 50 5 1200                          # max_iters=50, max_retries=5, agent_timeout=1200s
```

`ralph.sh` 会一直跑，直到所有 story 都 `passes: true`（输出
`<promise>COMPLETE</promise>` 后退出）或达到 `max_iterations`。

### N 路并行分片

适用场景：story 之间足够独立，N 个 Claude Code 实例可以并发跑而不互踩：

- **标注任务**：每个分片拥有 `task_ids` 的一个连续片段。
- **代码实现任务**：每个分片拥有不相交的文件路径集合（真冲突就用 git
  worktree 隔离）。

```bash
# 1. 先初始化基线一次
./examples/sample-jsonl/init.sh

# 2. 从 .ralph/ 渲染 N 个分片树
python3 scripts_4x/render_shards.py --num-shards 4
# → 创建 .ralph-shard-{a,b,c,d}，含 prd.json、manifest、batch symlink

# 3. 校验 5 个不变量（计数、不相交、并集、symlink、prd ↔ manifest）
python3 scripts_4x/audit_shards.py --num-shards 4

# 4. 后台启动 4 个分片（默认间隔 30s 错峰）
./scripts_4x/run_shards.sh a b c d
# → 派生 ./ralph.sh --shard-root .ralph-shard-X，日志写到 .ralph-shard-X/run.log

# 5. 看进度（随便重跑，不写任何东西）
./scripts_4x/dashboard.sh a b c d
watch -n 30 ./scripts_4x/dashboard.sh a b c d   # 每 30 秒自动刷新

# 6. 可选：原因分类的 monitor（rate-limit / dirty-tree 自动重启）
python3 scripts_4x/monitor_shards.py --shards a b c d

# 7. 全部分片 passes=true 时，合并 + 审计
python3 scripts_4x/merge_shards.py --num-shards 4 --out-dir eval_results
```

`run_shards.sh` 的可调环境变量：`LAUNCH_DELAY=30`、`MAX_ITER=200`、
`MAX_RETRIES=15`。

---

## 配置

### prd.json

权威的任务清单。每个 story 含 `id`、`passes`、`priority`、可选的
`acceptanceGate`，加上 operator prompt 引用的元数据（`title`、
`modifies`、`creates`、`acceptanceCriteria` 等）。

```json
{
  "branchName": "main",
  "userStories": [
    {
      "id": "BATCH-001",
      "passes": false,
      "priority": 1,
      "acceptanceGate": {
        "type": "command",
        "command": "pytest tests/test_endpoint_users.py -q"
      }
    },
    { "id": "BATCH-002", "passes": false, "priority": 2 }
  ],
  "acceptance": {
    "max_attempts": 3,
    "default_gate": {
      "type": "jsonl_schema",
      "schema_version": "judge-v1",
      "verdict_schema": {
        "required_fields": ["task_id", "qa", "reason"],
        "id_field": "task_id",
        "qa_field": "qa",
        "reason_field": "reason",
        "valid_qa": ["yes", "no", "uncertain"],
        "min_reason_chars": 150,
        "reason_long_ratio_min": 0.9,
        "distinct_qa_min": 2,
        "distinct_qa_min_small": 1,
        "small_batch_threshold": 33
      }
    }
  }
}
```

`run_batch.py validate` 和 `run_batch.py finalize` 会按 story 里指定的
gate（或项目级 `acceptance.default_gate`）派发，传入 story dict 和分片
根目录。

### manifest.json（仅标注模式）

JSONL / 标注任务里，manifest 声明 BATCH → task_ids 的映射：

```json
{
  "schema_version": "sentiment-v1",
  "batch_size": 4,
  "n_batches": 2,
  "total_tasks": 8,
  "batches": [
    { "story_id": "BATCH-001", "input_file": ".ralph/stories/batch-001.jsonl",
      "n_tasks": 4, "task_ids": ["s_001","s_002","s_003","s_004"] },
    { "story_id": "BATCH-002", "input_file": ".ralph/stories/batch-002.jsonl",
      "n_tasks": 4, "task_ids": ["s_005","s_006","s_007","s_008"] }
  ]
}
```

`n_batches` 和 `total_tasks` 是权威值；`audit_shards.py` 和
`merge_shards.py` 会用它们交叉验证每个分片的事实。

### 分片配置

```bash
# 自动平均切（最后一个分片吸收余数）
python3 scripts_4x/render_shards.py --num-shards 4

# 显式区间
python3 scripts_4x/render_shards.py --num-shards 4 \
        --splits "1-13,14-26,27-39,40-53"

# 从 JSON 配置加载
python3 scripts_4x/render_shards.py --num-shards 4 \
        --splits-file shard-splits.json
```

`shard-splits.json`:
```json
{"splits": [[1,13],[14,26],[27,39],[40,53]]}
```

---

## 关键概念

**每轮 fresh context。** 每个 story 都由一个全新的 Claude Code 实例
实现。两轮之间能保留的"记忆"只有 git history、`progress.txt`、
`prd.json`，再无其他。一段上下文如果必须跨轮存活，就要写进
`progress.txt` 或者 commit 进去。

**story 大小要刚好。** 一个 story 必须能塞进一个 context window。
太大 → LLM 还没到 finalize 就把 context 跑光，gate 失败，循环重试，
失败重复出现。把 story 切到"一个动作"的粒度（一对文件、一批 N 个
任务、一次 DB 迁移）。

**写入范围互不相交，是并行安全的根本。** N 个分片在 *task_id /
batch* 层面的不相交由 `render_shards.py` + `audit_shards.py` 保证。
写边界 hook（`scripts_4x/hooks/deny_outside_shard.py`）把这层保证
延伸到**文件系统**：分片 `a` 里的 subagent 不能写 `.ralph-shard-b/`、
`.ralph/`，也不能改任意 `*.py`。hook 由 `RALPH_SHARD_ROOT` 控制开关，
单进程模式下零开销。

**只允许流式写入。** 每条 verdict 通过 `append_verdict.py` 落盘，
helper 校验 schema + schema_version + reason 长度后 fsync。第 25
分钟崩溃也不会丢第 1 分钟的工作。**绝不**让 subagent 在内存里
攒一堆 verdict 最后批量写——那正是这条规则被设计来杜绝的失败模式。

**Promise token 协议。** 每轮 stdout 必须以
`<promise>YIELD</promise>`（还有剩余 story）、
`<promise>COMPLETE</promise>`（全部完成）或
`<promise>VIOLATION</promise>`（超过 max_attempts）结尾。
循环 driver 把这个 token 和 `prd.json` 交叉校验，防止 agent 假冒
完成。

**自动恢复。** 当 `attempts > 0` 且整批 task_id 已经在 `seen_task_ids`
里但 gate 还是失败，`prepare` 会把这个 batch 的 verdict + seen 全部
丢掉重做。是为了应对"subagent 写完了但 gate 在某个下游检查处持续
失败"这种死循环而加的。

**5 层集合论分片审计。** `audit_shards.py` 强制：
(1) 每个分片的 task 数 == manifest.total_tasks；
(2) 任意两个分片 *i* ∩ *j* = ∅；
(3) ⋃ 分片 = 基线输入集；
(4) 分片里的每个 batch symlink 都指向基线；
(5) 每个分片的 `prd.json` 里的 BATCH id 集合与该分片的 `manifest.json`
一致。任何漂移直接 fail-stop。

---

## 运维 / 排错

### 怎么读 dashboard

```bash
./scripts_4x/dashboard.sh a b c d
```

每个分片一行：ASCII 进度条（passes / total）、`run.log` 里的 429
（rate-limit）次数、`verdicts.jsonl` 里最近若干条 reason 的平均长度、
PID 状态。dashboard 不写任何东西——可以放心丢进 `watch`。

### 出问题时先看哪个文件

| 现象 | 先看 |
|------|-----|
| "这个 story 通过了吗？" | `<ROOT>/prd.json` 的 `passes`，`<ROOT>/progress.txt` |
| "agent 实际写了什么？" | `<ROOT>/state/verdicts.jsonl` |
| "loop driver 做了什么？" | `<ROOT>/loop.log` |
| "Claude Code agent 输出了什么？" | `<ROOT>/run.log` |
| "现在锁定的是哪个 story？" | `<ROOT>/current_story.json` |
| "有没有撞到写边界 hook？" | `<ROOT>/run.log`（搜 `exit 2`） |

`<ROOT>` 在单进程模式下是 `.ralph`，分片模式下是 `.ralph-shard-X`。

### 撞到 rate limit

`monitor_shards.py` 会检测到 429，sleep `--rate-limit-wait-sec`，
然后自动重启。手动恢复：

```bash
./scripts_4x/stop_shards.sh                    # SIGTERM 全部分片
./scripts_4x/recover_shards_after_limit.sh     # 重置每个 story 的 retry 计数、
                                               # 恢复优先级、重新启动
```

### 分片死了（DEGRADE）

干净停掉死分片，把它没做完的任务排到存活分片，重启：

```bash
# 4 分片 → 2 分片：c+d 死了，把它们的任务排给 a+b
./scripts_4x/stop_shards.sh c d
python3 scripts_4x/redistribute_remaining.py \
        --from-shards c,d --keep-shards a,b --target keep
python3 scripts_4x/audit_shards.py --num-shards 2
./scripts_4x/run_shards.sh a b

# 全部停掉 → 退回单进程基线
./scripts_4x/stop_shards.sh
python3 scripts_4x/redistribute_remaining.py \
        --from-shards a,b,c,d --target baseline
./ralph.sh
```

`redistribute_remaining.py` 在任意源/目标分片仍存活时会拒绝运行——
避免对 `prd.json` / `manifest.json` 的并发 RMW。

### dirty-tree gate 把循环卡住了

如果工作树在分片自身 state 目录之外有未提交修改，`ralph.sh` 会拒绝
开下一轮（单进程模式看整棵树，分片模式按分片切片看）。这是为了
防止循环把残留改动顺手 commit 进去。手动处理：

```bash
git status
# 要么 commit、要么 stash、要么 revert——然后重跑
```

### 干净停止

```bash
./scripts_4x/stop_shards.sh              # SIGTERM 全部（默认 a b c d）
./scripts_4x/stop_shards.sh c d          # 只停 c+d
GRACE_SEC=10 ./scripts_4x/stop_shards.sh # SIGKILL 之前给更长的宽限期
```

---

## 扩展：写自己的 acceptance gate

`acceptance.py` 是单一 source of truth。新增一个 gate 类型：

1. 实现 `Gate` 协议（一个有 `validate(story, root) -> GateResult`
   方法的类）。
2. 在 `_BUILTIN_GATES` 里按 `type` 字符串注册。
3. 在 `prd.json` 里通过 `acceptanceGate.type`（按 story）或
   `acceptance.default_gate.type`（项目级）引用。

```python
# .ralph/scripts/acceptance.py（草图）
class MyGate:
    def __init__(self, config: dict):
        self.threshold = config["threshold"]

    def validate(self, story: dict, root: Path) -> GateResult:
        ...
        return GateResult(
            passed=ok,
            failures=[] if ok else ["失败原因"],
            diagnostics={"score": score},
        )

_BUILTIN_GATES = {
    "jsonl_schema": JsonlSchemaGate,
    "command": CommandGate,
    "composite": CompositeGate,
    "my_gate": MyGate,        # ← 在这里注册
}
```

新 gate 的测试放在 `tests/test_acceptance_gates.py`，PASS 和 FAIL
两条路径都要覆盖。

---

## 代码 vs 标注两种 ralph

| 关注点              | 标注用例                          | 代码实现用例                              |
|---------------------|----------------------------------|------------------------------------------|
| 每个 story 的产物    | 往 `verdicts.jsonl` 追加行       | 代码 + 测试文件的 `git commit`            |
| Gate                 | `JsonlSchemaGate`                | 跑 pytest / cargo test 的 `CommandGate`   |
| 互斥范围             | task_id 切分                     | 文件路径切分（hook 强制）                 |
| 分片隔离             | 各自独立的 state 目录            | 各自独立目录 **或** git worktree         |

写边界 hook 已经在目录粒度上保证了互斥；story 粒度的互斥是同一思想
应用到文件路径上的版本。如果某些 story 真的会互相冲突，就把每个
分片放到自己的 `git worktree` 里跑，最后 merge。

---

## License

MIT — 见 [LICENSE](./LICENSE)。

## 来源 & 致谢

本仓库的 harness 是从一个私有的 LLM-eval 项目里抽取并 sanitize 出来的。
很多实现细节——5-gate acceptance 契约、自动恢复启发式、流式写入规则、
校准过的 subagent-serial-1×N 经验——都来自那边遇到的真实失败模式。

底层的 ralph 循环模式来自
[Geoffrey Huntley 的 "ralph"](https://ghuntley.com/ralph/) ——
"一份 PRD、一个 acceptance 文件、循环到完成"这一原始想法的出处。
本 harness 直接受
[snarktank/ralph](https://github.com/snarktank/ralph) 启发——它提供了
本项目所基于的具体参考实现。
