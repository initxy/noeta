# 基准测试

Noeta 是运行时，不是 coding agent —— 所以衡量它最诚实的办法，是把一个**建在它之上**的 agent 拿去跑公开基准：同一套 harness、同一批 verifier，和整个领域比。

那个 agent 是 [`noeta-agent`](https://github.com/initxy/noeta-agent)，建在 `noeta-sdk` / `noeta-runtime` 之上的参考产品。它的 `main` 预设完全由公开 SDK 面组装而成 —— 工具、policy、context composer、事件账本，都是本站文档里的这些东西。因此下面每一个数字都端到端地压到了运行时本身：组装每次 prompt 的 composer、工具分发路径、guard，以及在步骤之间重建状态的 fold。

运行通过 [harbor](https://github.com/harbor-framework/harbor) 完成 —— 官方的 Terminal-Bench harness，也是公开排行榜背后的同一套。适配器、harness 配置和确切命令都在 `noeta-agent` 仓库的 [`bench/`](https://github.com/initxy/noeta-agent/tree/main/bench) 目录里。

## 结论

| 基准 | 范围 | `noeta-agent` `main`（Claude Opus 4.8） | 领域水平（公开排行榜） |
|------|------|----------------------------------------|------------------------|
| Terminal-Bench 2.1 | 40 题分层抽样 | **82.5%**（33/40） | 全集榜单区间 58.7%–83.8% |
| SWE-bench Verified | 15 实例子集 | **86.7%**（13/15） | 榜首约 79%，中段约 66–77% |

在 Terminal-Bench 2.1 上，agent 解决了抽样中的 **82.5%** —— 落在全集排行榜（58.7%–83.8%）的顶部区间，略低于 Claude Code + Fable 5（83.8%）与 Codex + GPT-5.5（83.1%），高于榜上所有 Opus/Sonnet 的 Claude Code 条目和所有 Terminus 2 条目。在 15 实例的 SWE-bench Verified 子集上解决 **13/15**。两者都跑 `Claude Opus 4.8`（终端榜用 `xhigh`，SWE-bench 用 `high`）。

这些是**抽样**，并如实标注 —— 是在领域区间里的一个定位，不是全集榜单成绩。见[本页不主张什么](#本页不主张什么)。

## Terminal-Bench 2.1（40 题分层抽样）

- **Harness：** harbor 0.20.0，数据集 `terminal-bench/terminal-bench-2-1`
  @ `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`
- **Agent：** `noeta-agent` 0.6.0（`main` 预设），依赖 `noeta-sdk` /
  `noeta-runtime` ≥ 0.6.10
- **模型：** `opus4.8`，reasoning effort `xhigh`
- **命令：** `NOETA_MODEL=opus4.8 NOETA_EFFORT=xhigh bench/run_benchmark.sh
  tb21-sample40`（40 个 task id 固定在该脚本的 `TB_SAMPLE40` 数组里，所以这份抽样可以逐字复跑）

| 日期 | 范围 | 通过 |
|------|------|------|
| 2026-08-10 | 40 题分层抽样（4 易 / 24 中 / 12 难） | **33/40 = 82.5%** |

按难度拆开：

| 难度 | 通过 |
|------|------|
| easy | 4/4（100%） |
| medium | 20/24（83%） |
| hard | 9/12（75%） |

**这个分数该怎么读。** 判定以每道题自己的 harbor verifier 为准（`X passed, 0 failed`），而不是 agent *进程*是否干净退出。7 个未通过都是真失败 —— 每一个在 verifier 输出里都带着真实的 `N failed`（`build-cython-ext`、`chess-best-move`、`count-dataset-tokens`、`dna-assembly`、`protein-assembly`、`raman-fitting`、`video-processing`）。

## SWE-bench Verified（固定子集）

- **Harness：** harbor，数据集 `swe-bench/swe-bench-verified`（适配器把 Verified 转成 harbor task，每个命名为 `swe-bench/<instance_id>`）
- **模型：** `opus4.8`，reasoning effort `high`
- **子集：** 固定的 15 个实例（不是全部 500 个），覆盖 Verified 里全部 12 个仓库、每仓 1–2 个，使抽样不被 django 的 231 行带偏。固定在 `run_benchmark.sh` 的 `SWE_SUBSET15` 里。
- **命令：** `bench/run_benchmark.sh swe-15`

| 日期 | 子集规模 | 通过 |
|------|----------|------|
| 2026-08-09 | 15 | **13/15 = 86.7%** |

两个真实失败（`django-11820`、`requests-1724`），其余 13 个通过。SWE-bench Verified 的镜像自带 Python 3.9–3.11，低于 `noeta-agent` 的 3.12 下限，所以适配器会先用 `uv` 装一份私有 3.12 再跑。

子集实例 id：

```
django__django-10097   django__django-11820   django__django-13195
sympy__sympy-11618     sympy__sympy-13877     sphinx-doc__sphinx-10323
matplotlib__matplotlib-13989   scikit-learn__scikit-learn-10297
pydata__xarray-3095    astropy__astropy-12907   pytest-dev__pytest-10051
pylint-dev__pylint-4551   psf__requests-1724   pallets__flask-5014
mwaskom__seaborn-3069
```

## 这套 harness 覆盖了什么

agent 在 harbor 上的跑法和领域里其他 agent 完全一致 —— 它是一个 `BaseInstalledAgent`（与 harbor 自己的 `pi`、`codex`、`claude-code`、`terminus` 同一个基类），被安装进每道题的容器，由 `noeta run` CLI 无人值守地驱动，再由 harbor 自己的逐题 verifier 打分。评分路径上没有任何 Noeta 特有的东西。

- **相同的数据集** —— 官方的 `terminal-bench/terminal-bench-2-1` 与 `swe-bench/swe-bench-verified` 注册表数据集，按 digest 固定。
- **相同的 verifier** —— 每道题自己的 `test.sh` / verifier 判定通过与否；agent 从不自己给自己打分。
- **相同的对照面** —— Terminal-Bench 2.1 的公开排行榜就是下文"领域水平"的参照。

### 覆盖范围与排除项

89 题的 TB2.1 全集里，有少数几道这套 harness 打不了分，原因出在环境而非能力 —— 在这里明说，而不是藏起来：

- **7 道题**的基础镜像自带 Python < 3.12（5 个 `python:3.10` / `3.11` 镜像，另有两道 `qemu-*` 跑在 `debian:bullseye` = 3.9），低于 `noeta-agent` 的 3.12 下限。适配器可以用 `uv` 装一份私有 3.12（SWE-bench 那轮就走的这条路），所以这并不是硬限制；它们不进*这一份*抽样，只是为了让抽样构成保持固定。
- **4 道题**在有界时间内无法打分：`make-mips-interpreter`、`make-doom-for-mips`、`install-windows-3-11`（会耗掉数小时的超时黑洞），以及 `polyglot-rust-c`（被标记为 `no-verified-solution` —— 连参考答案都过不了它自己的 verifier）。排除。

40 题的抽样从剩下的 78 道里按难度分层抽取。

## 领域水平（公开排行榜，作为参照）

Terminal-Bench 2.1 数字取自官方排行榜（[tbench.ai](https://www.tbench.ai/leaderboard/terminal-bench/2.1)）；SWE-bench Verified 取自 [swebench.com](https://www.swebench.com/)。这些是**全集**成绩 —— 引来作参照，**不**能与抽样直接比较。

| 名次 | Agent | 模型 | Effort | Terminal-Bench 2.1 |
|------|-------|------|--------|--------------------|
| 1 | Claude Code | Fable 5 | xhigh | 83.8% ± 1.2% |
| 2 | Codex | GPT-5.5 | xhigh | 83.1% ± 1.1% |
| 3 | Terminus 2 | Fable 5 | high | 80.4% ± 1.2% |
| 5 | Claude Code | Opus 4.8 | high | 78.9% ± 1.3% |
| 7 | Terminus 2 | GPT-5.5 | xhigh | 78.0% ± 1.2% |
| 10 | Claude Code | Sonnet 5 | high | 74.6% ± 1.6% |
| 12 | Claude Code | Opus 4.7 | max | 68.9% ± 1.4% |
| 13 | Terminus 2 | Opus 4.7 | max | 66.1% ± 1.4% |
| 14 | Gemini CLI | Gemini 3 Pro | high | 65.8% ± 1.4% |
| 17 | Claude Code | GLM-5.1 | max | 58.7% ± 1.2% |

来源：[tbench.ai](https://www.tbench.ai/leaderboard/terminal-bench/2.1)（共 17 条；上表节选为在售 CLI + 参考 agent）。榜单区间为 **58.7%–83.8%**，**82.5%** 的抽样落在顶部区间。请把它读作"处于在售 agent 的第一梯队"，而不是同题目上的名次。

## 可复现性

每个公布的数字都带着足以复跑同一评测的信息：模型 id、按 `name@version` 固定的数据集、可直接粘贴的命令，以及 SWE-bench 的完整实例 id 列表。harness 需要的环境（Docker、代理、wheel、模型目录）记在 [`bench/README.md`](https://github.com/initxy/noeta-agent/blob/main/bench/README.md)。

成本不作定价：这轮跑在一个网关上，其模型 id 不在 SDK 的价目表里，所以只报告 token 总量，不编造美元成本。

基准测试**刻意**不进 `make check`。它们需要 Docker、`harbor` CLI 和真实的网关凭证，还要花掉真实 token —— 这是你在自己掌控的机器上手动跑的一道门，不是自动化套件的一部分。

## 本页不主张什么

- 不是完整的 Terminal-Bench 2.1（89 题）成绩 —— 2026-08-10 那一行是 40 题分层抽样，并如实标注。
- 不是完整的 SWE-bench Verified（500）成绩 —— 是一个固定子集。
- 不是排行榜名次 —— 82.5% 是抽样，放进领域区间作参照，而不是同题目上的正面对决。
- 不是对*你的* agent 的承诺。这个分数属于一个预设、一个模型；运行时贡献的是它底下的机器，不是那段 prompt。

## 下一步

- [快速上手](tutorials/quickstart.md) —— 五分钟跑起一个 agent
- [预设代理](reference/presets.md) —— 内置 agent 都接了什么
- [对比](reference/comparison.md) —— Noeta 与其他 agent 框架的横向对照
