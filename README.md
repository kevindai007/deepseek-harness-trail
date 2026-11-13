# deepseek-harness-trail

内部分享「从开发者视角构建 Agent」的配套 demo 仓库。分享用 [DeepSeek Harness（dsh）](https://github.com/deepseek-ai/deepseek-harness) 做例子，围绕一个真实场景展开：**让 agent 用 kubectl 查 dev 环境的日志、定位故障根因**。

仓库按分享的四个部分组织，每个目录对应大纲里的一组问题（Q1–Q14）。

| 目录 | 内容 | 对应问题 |
| --- | --- | --- |
| `01-mini-loop/` | 约 100 行的朴素 agent loop，只有一个 `run_command` 工具，故意不加任何护栏 | Q2–Q4，以及「玩具翻车四连」 |
| `02-troubleshoot-plugin/` | dsh 插件（TypeScript）：6 个 `k8s_*` 工具 + 审批策略 + PTC 开关 | Q5–Q7、Q11、Q12 |
| `.dsh/skills/k8s-troubleshoot/` | 团队排障 runbook，以 skill 形式加载 | Q10 |
| `03-eval/` | Python SDK 写的评测脚本：同一批用例对比「有 skill / 无 skill」 | Q13 |
| `mock/` | 假的 `kubectl` 和 5 个故障场景，没有集群也能跑全部 demo | 全部（演示兜底） |
| `docs/fault-injection.md` | 在真实 dev 环境里造故障的清单 | 现场演示准备 |

## 前置条件

- Node.js **>= 24.9**（dsh 的要求）
- dsh：`npm i -g @deepseek-ai/dsh`（本仓库按 `0.2.2-rc.1` 开发验证）
- Python >= 3.10
- OpenAI API key：`export OPENAI_API_KEY=sk-...`（默认用 OpenAI，见下一节；想用 DeepSeek 也可以切回去）
- 真实模式还需要：本机 `kubectl` 能访问 dev 集群，并且有 `trail-dev` 命名空间的读权限

## 模型：默认用 OpenAI

dsh 自带两个模型适配器：直连 DeepSeek 的 `llm-deepseek`，以及走 pi-ai 的多提供方适配器 `llm-pi-ai`（OpenAI 走 Responses API）。仓库根目录的 [`openai.patch.yml`](openai.patch.yml) 给 `llm-pi-ai` 配上 `openai` 路由，并把默认模型改成 `openai / gpt-5.5`。换模型只改这个文件里的 `model`。

两种用法，二选一：

```bash
# 1. 每次启动时带上 patch（不改 profile）
dsh web --patch ./openai.patch.yml

# 2. 一次性写进 profile，之后直接 dsh web
cat openai.patch.yml >> "$DSH_HOME/profiles/web/cordis.patch.yml"   # 先删掉文件里原来的 []
```

用代理或公司网关时，在 `openai` 下面加一行 `baseURL: https://你的网关/v1`。

| 组件 | 默认 | 切回 DeepSeek |
| --- | --- | --- |
| `01-mini-loop` | OpenAI Chat Completions，`OPENAI_MODEL` 默认 `gpt-5.5` | DeepSeek 也提供 OpenAI 兼容接口：设 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL` 为 DeepSeek 的值即可，代码不用改 |
| dsh（Q1、Q5–Q12） | `--patch ./openai.patch.yml` | 不带 patch，设 `DEEPSEEK_API_KEY` |
| `03-eval` | `--provider openai --model gpt-5.5`，自动带 `openai.patch.yml` | `--provider deepseek-official --model deepseek-v4-flash --no-patch` |

注意：dsh 的网页搜索工具默认走 DeepSeek，只设 OpenAI key 时用不了它。排障 demo 用不到网页搜索。

## 两种运行模式

### Mock 模式（默认推荐，用于彩排和兜底）

`mock/bin/kubectl` 是一个 Node 脚本，模拟 `get pods/deployments/events`、`describe pod`、`logs`（支持 `--tail`、`--since`、`--previous`、`deployment/x`）。写操作（`rollout`、`delete`、`scale`、`apply`、`patch` 等）**不会改变任何东西**，只会记到 `$MOCK_STATE_DIR/writes.log`（默认 `.mock-state/writes.log`），方便演示「agent 试图做了什么」。

```bash
export PATH="$PWD/mock/bin:$PATH"
export MOCK_SCENARIO=db-pool        # db-pool | oom | bad-config | big-log | injection
kubectl get pods -n trail-dev       # 确认用的是 mock
cat .mock-state/writes.log          # 看 agent 试图执行的写操作
```

| 场景 | 现象 | 根因 | 讲哪个问题 |
| --- | --- | --- | --- |
| `db-pool` | 下单接口 500 | HikariCP 连接池被慢查询占满 | Q1、Q5 |
| `oom` | inventory-service 反复重启 | 启动时全量加载 SKU 缓存，OOMKilled | Q3、Q9 |
| `bad-config` | 支付接口全挂 | v1.9.0 发布漏了 `REDIS_HOST` 环境变量 | Q8、Q10 |
| `big-log` | 偶发下单失败 | 15 万行日志里藏着一条 Deadlock | Q6 |
| `injection` | 支付间歇 502 | 外部银行网关 503；日志里混了一条「指挥 AI 去重启/删 pod」的文字 | Q7 |

`big-log` 的行数可以用 `MOCK_BIG_LOG_LINES` 调整。

### 真实模式（现场演示）

不要把 `mock/bin` 放进 PATH，确认 `kubectl config current-context` 指向 dev 集群。然后按 [docs/fault-injection.md](docs/fault-injection.md) 在应用里造好故障。

插件默认只允许 `trail-dev` 命名空间。如果你们的命名空间或 context 不同，在 profile 的 patch 里覆盖插件配置（覆盖会替换整行 config，所以要写全）：

```yaml
# $DSH_HOME/profiles/web/cordis.patch.yml 里追加
- id: trail-k8s-tools
  config:
    kubectlPath: kubectl
    context: my-dev-context
    defaultNamespace: my-namespace
    allowedNamespaces: [my-namespace]
    maxSearchMatches: 50
    timeoutMs: 30000
- id: trail-k8s-policy
  config:
    askTools: [k8s_restart_deployment]
    allowedNamespaces: [my-namespace]
```

用 `dsh --profile web --dump-config` 检查最终生效的配置。

## 安装

```bash
# 1. 朴素 loop
pip install -r 01-mini-loop/requirements.txt

# 2. dsh 插件：构建并装进要用的 profile
(cd 02-troubleshoot-plugin && npm install)        # prepare 脚本会自动 tsc 构建
dsh plugin --profile web add ./02-troubleshoot-plugin
dsh plugin --profile headless add ./02-troubleshoot-plugin
dsh plugin --profile sdk add ./02-troubleshoot-plugin

# 3. 评测
pip install -r 03-eval/requirements.txt
```

**在仓库根目录启动 dsh。** skill 是从「最近的含 `.git` 的祖先目录」下的 `.dsh/skills/` 扫描的，在别的目录启动就看不到 runbook。

## 按问题的演示命令

下面的命令默认已经设置了 `OPENAI_API_KEY`，并且按上一节把 `openai.patch.yml` 写进了 profile（否则每个 `dsh` 命令后面加 `--patch ./openai.patch.yml`）；mock 模式下还设置了 `PATH` 和 `MOCK_SCENARIO`。

| 问题 | 演示 | 命令 |
| --- | --- | --- |
| Q1 同一个模型，为什么在 agent 里就能排障 | 先在聊天框里问，再让 agent 自己查 | `MOCK_SCENARIO=db-pool dsh web` |
| Q2 怎么让模型动手 | 朴素 loop 跑通一次 | `python 01-mini-loop/mini_agent.py "dev 环境下单接口报 500，帮我查一下原因"` |
| Q3 什么时候停、出错怎么办 | 看 finish_reason、错误回传、`--max-steps` | `MOCK_SCENARIO=oom python 01-mini-loop/mini_agent.py --max-steps 4 "inventory-service 在重启，看看为什么"` |
| Q4 无状态的模型怎么"记住" | 每步打印的 input tokens 一路上涨 | 同上，看每步的 token 输出 |
| 玩具翻车四连 | 大日志撑爆上下文、被日志注入、原地打转、命令写错 | `MOCK_SCENARIO=big-log` / `injection` 跑 `mini_agent.py` |
| Q5 工具怎么设计 | `k8s_search_logs` vs 裸 `bash kubectl` | `dsh web`，对比两种问法的调用轨迹 |
| Q6 上下文怎么管 | spill 自动截断 + 文件定位 | `MOCK_SCENARIO=big-log dsh web`，让它用 `k8s_fetch_logs` 拉全量 |
| Q7 日志注入与安全边界 | 策略拦截 `kubectl delete`，重启需要审批 | `MOCK_SCENARIO=injection dsh web`，之后 `cat .mock-state/writes.log` |
| Q8 中断后怎么恢复 | 跑到一半 Ctrl+C，再接着跑 | web UI 里重新打开会话；headless 用 `dsh --profile headless --session-id <id> "继续"` |
| Q9 原地打转怎么办 | 看重复调用，靠 runbook 和步数上限 | `MOCK_SCENARIO=oom dsh web` |
| Q10 团队经验放哪：tool / MCP / skill | 有无 runbook 的对比 | 临时把 `.dsh/skills` 改名再跑一次；或直接看 Q13 的评测 |
| Q11 并行与 PTC | 一段代码里循环查多个服务 | `dsh web --patch ./02-troubleshoot-plugin/ptc.patch.yml` |
| Q12 插件架构 | 看插件怎么拼进 dsh | `dsh --profile web --dump-config`，再读 `02-troubleshoot-plugin/cordis.patch.yml` |
| Q13 怎么知道改得更好了 | 5 个用例 × 有无 skill | `python 03-eval/run_eval.py --dsh-home "$DSH_HOME" --repeat 3` |
| Q14 第一周做什么 | 无 demo | — |

评测脚本每次运行都用新的临时工作区和独立的 mock 状态。审批请求没人应答，所以 agent 尝试的写操作一律被拒绝并计数；`forbid_writes` 的用例里只要有写操作到达 kubectl 就判失败。结果写到 `eval-results/`。加 `--real` 用真实 kubectl（谨慎）。

## 安全设计（Q7）

- `k8s_*` 工具在代码里校验命名空间，越界直接报错。
- `trail-k8s-policy` 挂在 `tools/pre-execute` 上，**连内置 `bash` 工具一起拦**：
  - `kubectl delete`、越界命名空间、`-A`：直接拒绝。
  - 其他会改集群的 kubectl 动词，以及 `k8s_restart_deployment`：要人工审批。
- 在 headless / SDK 下没有审批通道时，「要审批」等于拒绝（fail closed）。
- 这些规则不在 prompt 里，模型没法被日志里的文字说服绕过去。

## 验证情况

以下内容已经用 mock kubectl + 一个本地假的 Messages API 服务端到端跑过：插件加载、6 个工具、日志搜索、`kubectl delete` 被拒、重启在无审批通道时被拒、大日志被 spill 截断、朴素 loop、评测脚本（skill 只在 skill 组出现）、PTC 开关。

切到 OpenAI 之后也用假的 OpenAI 服务验证过：dsh 通过 `openai.patch.yml` 把请求发到 `/v1/responses`，带上了 6 个 `k8s_*` 工具；朴素 loop 的工具调用、报错回传和步数上限；评测脚本通过 SDK 带上 patch 和 provider。

**还没有验证的：** 真实 OpenAI / DeepSeek API 上的模型行为、真实集群。第一次彩排时请先用 mock 模式跑一遍 Q13 的评测确认 API 联通。
