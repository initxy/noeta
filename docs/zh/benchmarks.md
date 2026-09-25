# 基准测试

Noeta 是运行时，所以我们评测的是基于它搭的 agent：[`noeta-agent`](https://github.com/initxy/noeta-agent) 的 `main` 预设，只用公开 SDK 组装。跑的是业界公开基准，用同一套 harness、同一批判分脚本。

## 结果

| 基准 | 范围 | `noeta-agent` `main`（Claude Opus 4.8） | 业界水平（公开排行榜） |
|------|------|----------------------------------------|------------------------|
| Terminal-Bench 2.1 | 40 题分层抽样 | **82.5%**（33/40） | 全集榜单区间 58.7%–83.8% |
| SWE-bench Verified | 15 实例子集 | **86.7%**（13/15） | 榜首约 79%，中段约 66–77% |

Terminal-Bench 2.1 的抽样成绩落在全集排行榜的第一梯队：略低于 Claude Code + Fable 5（83.8%）和 Codex + GPT-5.5（83.1%），高于榜上所有 Opus/Sonnet 版 Claude Code 和所有 Terminus 2。两项都用 `Claude Opus 4.8`（Terminal-Bench 用 `xhigh`，SWE-bench 用 `high`）。

::: warning 这些是抽样
只说明落在业界哪个区间，不是全集榜单成绩。见[本页不代表什么](#本页不代表什么)。
:::

## 方法

通过 [harbor](https://github.com/harbor-framework/harbor) 运行，这是 Terminal-Bench 官方 harness，公开排行榜用的也是它。agent 以 harbor `BaseInstalledAgent` 的形式接入（和 harbor 自带的 `pi`、`codex`、`claude-code`、`terminus` 同一个基类），装进每道题的容器，由 `noeta-agent` 的 `noeta run` CLI 无人值守地跑，最后由每道题自己的判分脚本打分，agent 不给自己打分。数据集用官方注册表里的版本，按摘要固定。适配代码、环境配置和命令都在 [`bench/`](https://github.com/initxy/noeta-agent/tree/main/bench)。

### Terminal-Bench 2.1（40 题分层抽样）

| 项目 | 取值 |
|---|---|
| Harness | harbor 0.20.0，`terminal-bench/terminal-bench-2-1` @ `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a` |
| Agent | `noeta-agent` 0.6.0（`main` 预设），`noeta-sdk` / `noeta-runtime` ≥ 0.6.10 |
| 模型 | `opus4.8`，reasoning effort `xhigh` |
| 命令 | `NOETA_MODEL=opus4.8 NOETA_EFFORT=xhigh bench/run_benchmark.sh tb21-sample40`（题号固定在 `TB_SAMPLE40`） |
| 日期 | 2026-08-10 |
| 结果 | **33/40 = 82.5%**：easy 4/4（100%），medium 20/24（83%），hard 9/12（75%） |

判定通过看的是 harbor 判分脚本输出 `X passed, 0 failed`，不是 agent 进程是否正常退出。7 道没过的题都有真实的 `N failed`：`build-cython-ext`、`chess-best-move`、`count-dataset-tokens`、`dna-assembly`、`protein-assembly`、`raman-fitting`、`video-processing`。

**覆盖范围。** 89 道题里有 11 道因为环境原因没纳入，和能力无关：

- 7 道的基础镜像 Python 低于 3.12（五个 `python:3.10` / `3.11`，两个 `qemu-*` 用 `debian:bullseye`，即 3.9），低于 `noeta-agent` 要求的 3.12。适配器可以用 `uv` 装一个 3.12（SWE-bench 就是这么跑的），不纳入只是为了让这份抽样保持固定。
- 4 道在限定时间内没法打分：`make-mips-interpreter`、`make-doom-for-mips`、`install-windows-3-11`（动辄超时几个小时），以及 `polyglot-rust-c`（标记为 `no-verified-solution`，连参考答案都过不了自己的判分）。

40 题从剩下的 78 道里按难度分层抽取（4 easy / 24 medium / 12 hard）。

### SWE-bench Verified（15 实例子集）

| 项目 | 取值 |
|---|---|
| Harness | harbor，`swe-bench/swe-bench-verified`（每个实例是一个 harbor 任务 `swe-bench/<instance_id>`） |
| 模型 | `opus4.8`，reasoning effort `high` |
| 子集 | 固定 15 个实例，覆盖 Verified 全部 12 个仓库、每个一到两个（避免偏向 django 的 231 条）；固定在 `SWE_SUBSET15` |
| 命令 | `bench/run_benchmark.sh swe-15` |
| 日期 | 2026-08-09 |
| 结果 | **13/15 = 86.7%**；没过的是 `django-11820`、`requests-1724` |

Verified 的镜像自带 Python 3.9–3.11，所以适配器会用 `uv` 单独装一个 3.12。

```
django__django-10097   django__django-11820   django__django-13195
sympy__sympy-11618     sympy__sympy-13877     sphinx-doc__sphinx-10323
matplotlib__matplotlib-13989   scikit-learn__scikit-learn-10297
pydata__xarray-3095    astropy__astropy-12907   pytest-dev__pytest-10051
pylint-dev__pylint-4551   psf__requests-1724   pallets__flask-5014
mwaskom__seaborn-3069
```

### 业界成绩

全集成绩取自 [Terminal-Bench 2.1 官方排行榜](https://www.tbench.ai/leaderboard/terminal-bench/2.1)（共 17 条，这里只列正式发布的 CLI 和参考 agent）；SWE-bench Verified 取自 [swebench.com](https://www.swebench.com/)。仅作参照，不能和抽样成绩直接比。

| 排名 | Agent | 模型 | Effort | Terminal-Bench 2.1 |
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

### 复现

每个数字都带有模型 id、固定的数据集版本、可以直接复制的命令，SWE-bench 还给了完整实例列表。Docker、代理、wheel 和模型目录的准备步骤见 [`bench/README.md`](https://github.com/initxy/noeta-agent/blob/main/bench/README.md)。没有报美元成本：网关的模型 id 不在 SDK 的价格目录里，只有 token 数。基准测试不在 `make check` 里，因为它需要 Docker、`harbor` CLI、真实凭据，还要真花 token。

## 本页不代表什么

- 不是 Terminal-Bench 2.1 全集（89 题）成绩：2026-08-10 那一行是 40 题分层抽样，已如实标注。
- 不是 SWE-bench Verified 全集（500 题）成绩：是固定子集。
- 不是排行榜名次：82.5% 是抽样结果，放进业界区间只作参照，不是在同一批题上的正面比较。
- 不代表**你的** agent 能拿到同样分数。这个成绩属于一个预设加一个模型；运行时贡献的是底层机制，不是提示词。

## 下一步

- [快速开始](start/quickstart.md)
- [预设](reference/presets.md)
- [为什么选 Noeta](why-noeta.md)
