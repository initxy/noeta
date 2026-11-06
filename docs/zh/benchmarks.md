# 评测

noeta 1.0 在公开编程评测上和 Claude Code 比得怎么样：跑了什么、结果如何、结果说明不了什么、怎么
自己再跑一遍。完整记录（每一轮、重试、适配器、Docker 配置）在
[`bench/README.md`](https://github.com/initxy/noeta/blob/main/bench/README.md)。

## 主要结果

noeta 1.0（提交 `34f8acee`）和 Claude Code 2.1.312 用同一个模型
（DeepSeek V4.1 Flash，effort `high`），同样的题目、限时和机器，2026-10-30 到
2026-11-01 期间通过 [harbor](https://github.com/harbor-framework/harbor) 运行：

| 评测 | noeta | Claude Code | noeta − Claude Code（标准误） |
|---|---|---|---|
| SWE-rebench 07-2026，111 题，限时 50 分钟，各跑两轮合并 | 50.5 % | 45.0 % | +5.4（3.6） |
| 同上，去掉答案泄露的 91 题 | 51.1 % | 44.0 % | +7.1（3.6） |
| 同上，只看同时跑的那一轮 | 49.5 % | 42.3 % | +7.2（3.8） |
| Terminal-Bench 4.0，66 题中的 54 题，限时 2 小时，一轮 | 3.7 % | 1.9 % | +1.9（3.2） |

每轮 111 题 SWE-rebench 的花费：noeta $5.01–$5.77，Claude Code $5.70–$6.25。每轮 54 题
Terminal-Bench：noeta $5.23，Claude Code $6.50。花费按 DeepSeek 官方对 DeepSeek-V4.1-Flash 的标价
计算（2026-10-30 读取），全程处于非高峰时段。

差值是逐题的 noeta 通过率减去 Claude Code 通过率，再取平均，括号里是标准误。单独一轮的结果会上下
浮动 5 个点左右（Claude Code 在两轮 SWE-rebench 里分别得了 47.7 % 和 42.3 %），所以请看合并的那一行。

## 为什么选这几个评测

| 榜单 | 用了吗 | 理由 |
|---|---|---|
| SWE-rebench | 用了，2026 年 7 月那一期（111 题） | 题目来自模型训练数据之后的新 GitHub 问题，所有模型用同一套脚手架，而且一台机器上就能让两个 agent 各跑两轮 |
| Terminal-Bench 4.0 | 用了，66 题中的 54 题 | 2.1 版饱和后，它是终端类 agent 榜单的主榜；Claude Code 是它的参考 agent |
| SWE-bench Verified | 没用 | 已经饱和（最高分接近 97 %） |
| SWE-bench Pro | 没用 | 每轮 731 个大镜像，一台机器跑不了两个 agent 各两轮；而且固定的公开题集可能已混进训练数据 |

## 方法

- **一台机器**（16 核、62 GB 内存、一块 GPU 题目用不上的 NVIDIA T4），harbor 0.25.0。
- **两个 agent 放在同一个 harbor 任务里。** `bench/compare.py` 让双方的试次交错进行，所以双方随时间
  承受的网关负载相同，并发数也平分。模型、effort、题目、超时都相同，上下文长度（1M）和输出上限
  （65,536）也相同。
- **Claude Code 2.1.312** 用的是 harbor 自带的 `claude-code` agent，关掉遥测，只多做了一件事：到
  时限时把它在沙箱里的整棵进程树停掉（harbor 只取消自己的 `docker exec` 客户端，不这么做的话 agent
  会在评分程序运行时继续改文件）。noeta 的适配器也这样做。
- **noeta** 在每个题目容器里装进独立的虚拟环境，用
  `noeta -p "<题目>" --output-format stream-json --verbose --permission-mode bypassPermissions
  --cwd "$(pwd)" --model <id> --effort <e>` 运行，并附一个模型描述文件，写明模型的真实上限。
- **双方实际发出的请求**通过本地代理录过一次：`max_tokens`（65536）、`thinking`（`adaptive`）、
  `output_config.effort`（`high`）、`context_management` 都相同；双方都不发 `temperature` 和
  `top_p`。noeta 多一次调用：第一轮之前生成对话标题。
- **计分：** 每轮每题评一次（k=1）。撞到时限的试次照常评分；没拿到评分就结束的试次（安装失败、
  容器解析不到、网络问题）由 harbor 最多重试三次，这等于给了 agent 第二次机会。始终没拿到评分的
  算失败。
- **token 数**：noeta 的取自它自己的存储（含子代理和压缩），Claude Code 的取自它的会话文件（含子代理）。
- **不上传任何东西。** harbor 的遥测关掉，题集从本地副本读取，网关 key 从所有任务文件里抹掉。

## 结果

### SWE-rebench 07-2026（111 题，限时 50 分钟）

`ibragim-badertdinov/swe-rebench-07-2026` 的全部 111 题，按题集自己的 50 分钟时限，同时跑 16 个试次。

| 轮次 | agent（noeta 提交） | 通过 | 50 分钟超时 | token 输入 / 缓存命中 / 输出 | 花费 |
|---|---|---|---|---|---|
| 1 | noeta `54121930` | 50（45.0 %） | 59 | 326M / 317M / 5.4M | $5.58 |
| | Claude Code | 53（47.7 %） | 63 | 381M / 370M / 6.0M | $6.25 |
| fix | noeta `34f8acee` | 57（51.4 %） | 43 | 390M / 381M / 5.5M | $5.77 |
| r2 | noeta `34f8acee` | 55（49.5 %） | 49 | 300M / 292M / 4.9M | $5.01 |
| | Claude Code | 47（42.3 %） | 66 | 319M / 309M / 5.6M | $5.70 |

各轮的差值：第 1 轮（noeta `54121930`）−2.7（标准误 4.3）；"fix" 轮的 noeta `34f8acee` 对第 1 轮
的 Claude Code +3.6（标准误 5.1）；r2 +7.2（标准误 3.8）。两轮合并（Claude Code 取第 1 轮和 r2，
noeta 取 fix 和 r2）：+5.4（标准误 3.6）。

**两个 noeta 提交之间改了什么。** `54121930` 只把一小串白名单里的环境变量传给命令，题目镜像里的
`CARGO_HOME`、`RUSTUP_HOME`、`JAVA_HOME` 之类都丢了；Claude Code 是整个环境都传。`34f8acee` 传
整个环境，只去掉 noeta 自己的凭据，并且只对 Claude 模型发 token 预算提示，和 Claude Code 一样。
修复后，手动设置工具链变量的试次从 22 降到 5，碰到"找不到工具链"的从 7 降到 2，超时从 59 降到 43。

### Terminal-Bench 4.0（54 题，限时 2 小时）

`terminal-bench/terminal-bench@4.0.0` 的 66 题中的 54 题：3 道 GPU 题需要比这台机器驱动更新的
CUDA，8 道题自带的参考答案过不了它们自己的测试，还有 1 道的参考答案在这台机器上过不了。agent 时限
是 2 小时，比题目自己的时限短，所以这些数字不能和排行榜比。同时跑 8 个试次，k=1。

| 轮次 | agent（noeta 提交） | 通过 | 2 小时超时 | token 输入 / 缓存命中 / 输出 | 花费 |
|---|---|---|---|---|---|
| 1 | noeta `54121930` | 2（3.7 %） | 39 | 308M / 303M / 9.1M | $7.10 |
| | Claude Code | 1（1.9 %） | 41 | 287M / 283M / 8.3M | $6.50 |
| fix | noeta `34f8acee` | 2（3.7 %） | 37 | 176M / 172M / 6.9M | $5.23 |

双方都接近零：在这个模型、2 小时时限下，Terminal-Bench 4.0 分不出高下。

### 一个更大的私有模型（20 道 SWE-rebench 题）

一个更大的私有模型（双方都是 1M 上下文、128K 输出上限），用种子 0 抽了 20 题，一次跑一个，
限时 50 分钟：

| 轮次 | agent（noeta 提交） | 通过 | 50 分钟超时 |
|---|---|---|---|
| 1 | noeta `54121930` | 6（30 %） | 12 |
| | Claude Code | 10（50 %） | 8 |
| fix | noeta `34f8acee` | 10（50 %） | 5 |

和第 1 轮的 Claude Code 比：noeta `54121930` −20 个点（标准误 11.7），noeta `34f8acee` 0（标准误
10.3；双方各有两题是对方没过的）。只有 20 题，两个差值都不显著。

## 注意事项

- **样本小，只有一个模型。** SWE-rebench 上的优势大约是 1.5 个标准误；单独一轮会浮动 ±5 个点。更大
  模型的对比只有 20 题。
- **答案泄露。** agent 能上网，而上游的修复是公开的。某次网络或 git 调用拿回了参考修复里的代码行，
  这个试次就算泄露；各轮加起来有 20 道题受影响，双方都有，"去掉答案泄露"那一行把它们排除了。
  Claude Code 用的是网关提供的服务端 `WebSearch`；noeta 没有搜索 key，只有 `WebFetch`。
- **读的项目说明文件不同。** Claude Code 读仓库里的 `CLAUDE.md` 和 `AGENTS.md`；noeta 读
  `NOETA.md`，没有时读 `AGENTS.md`，从不读 `CLAUDE.md`。111 道 SWE-rebench 题里有 34 道，Claude
  Code 读到了项目说明而 noeta 什么都没读到。这一点有利有弊，评测里没有拉平。
- **重试。** 双方都有题目是在 harbor 重试后才通过的；`bench/README.md` 列出了这些题。在
  Terminal-Bench 上，noeta 的 13 次重试里有 9 次是同一个故障：模型返回了既没有文字也没有工具调用的
  一轮，noeta 把这一轮当成错误结束了。
- **限流。** Claude Code 遇到 HTTP 429 / 529 重试时会记日志；noeta 在 provider 客户端里重试而不记，
  所以它的次数未知（不是零）。没有哪个试次因为限流而失败。
- **不是排行榜成绩。** Terminal-Bench 只跑了 66 题中的 54 题，时限 2 小时而不是 8 小时。没有向任何
  排行榜提交过。

## 自己跑一遍

需要 Docker、装在任意虚拟环境里的 harbor 0.25.0，以及写在 `.env` 文件里的网关凭据
（`NOETA_BASE_URL`、`NOETA_API_KEY`）。评测要花真金白银的 token，所以不在 `make check` 里。

```bash
uv venv && uv pip install harbor==0.25.0
export NOETA_ENV_FILE=/path/to/.env
H=/path/to/venv/bin/harbor

# 先把题集下载一次（运行时只读本地副本）
HARBOR_TELEMETRY=0 harbor download ibragim-badertdinov/swe-rebench-07-2026 -o bench/datasets/swe-rebench

# noeta 和 Claude Code 同场：先冒烟测一题，再按 50 分钟时限跑完整的 SWE-rebench
bench/compare.py run swe-rebench --tasks open-jarvis__OpenJarvis-465 \
  --agent-max-timeout-sec 1500 --concurrency 4 --harbor $H
bench/compare.py run swe-rebench --k 1 --concurrency 10 --harbor $H
bench/compare.py run tb4 --k 1 --concurrency 8 --agent-max-timeout-sec 7200 --harbor $H

bench/compare.py summary bench/jobs/<job-name>     # 对跑完的任务重新出汇总
```

每个任务目录里会有 `summary.md` / `summary.json`（通过率、逐题差值、token、花费、耗时、限流信号）
和 `provenance.json`（提交号、工作区是否有未提交改动、wheel 的哈希、harbor 和 Claude Code 的版本）。
只发布汇总，不要发布任务目录：agent 的日志里可能有它打印过的任何东西。

只跑 noeta，每种模式一条命令：

```bash
NOETA_EFFORT=high bench/run_benchmark.sh swe-rebench -k 3
NOETA_EFFORT=high bench/run_benchmark.sh tb4-full
bench/summarize.py --dataset bench/datasets/terminal-bench-2-1 bench/jobs/<job>
```

模型用 `--model <model-id>`（`compare.py`）或 `NOETA_MODEL=<model-id>`（`run_benchmark.sh`）指定，
填你的 provider 或网关给 DeepSeek V4.1 Flash 起的 id；脚本里的默认值是这几轮用的 id，换个环境多半没有。
effort 也最好指定：不指定时，请求带的是模型目录里这一行的默认值（没写默认值的就用 `high`），而 DeepSeek V4.1 不接受 `medium`。模型
目录里没有的模型 id 需要一个模型描述文件（`NOETA_MODELS_CONFIG`，或者用
`NOETA_MODEL_CONTEXT_WINDOW` 和 `NOETA_MODEL_MAX_OUTPUT_TOKENS` 让适配器生成一个），否则按 128K /
16K 运行。受限网络、Docker 清理、网关并发、固定 SWE-bench 子集等内容见
[`bench/README.md`](https://github.com/initxy/noeta/blob/main/bench/README.md)。
