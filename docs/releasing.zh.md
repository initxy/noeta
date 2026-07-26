# 发布 { #releasing }

`noeta-runtime` / `noeta-sdk` 从同一个仓库、在同一个 tag 下发布，但它们**不必**一起动。对 `packages/noeta-runtime` 或 `packages/noeta-sdk` 的合并行为更改应紧随发布——已发布的包不得落后于 `main`。

## 一个 tag 会发布什么 { #what-a-tag-publishes }

一个 `vX.Y.Z` tag 触发 `release.yml`：它只构建一次两个分发物，然后为每个包跑一个 publish job。**每个 publish job 都以 tag 版本为闸门**：只有当构建产出的 wheel 版本等于 `X.Y.Z` 时才上传，否则带一条 notice 干净跳过。

实际后果是：只 bump 你这次真正要发的包。没 bump 的包会干净跳过，而不是在重复上传上失败。两种形态都受支持、都正常：

- **锁步（lockstep）**——两个包都 bump 到 `X.Y.Z`，两个闸门全开。
- **部分（partial）**——只 bump 变化了的包（例如一个仅 runtime 的修复，让 `noeta-sdk` 停在当前版本）；被按住的那个包的 job 跳过。

跨包 `>=` 下限是部分发布仍然自洽的关键：bump 了的 `noeta-sdk` 必须把 `noeta-runtime>=` 下限抬到承载它所依赖行为的那个版本。

## 版本策略 { #version-policy }

- **默认 bump patch**：bug 修复、小的附加 API、打包修复。
- **minor / major**：维护者的明确决定（功能级别或破坏性发布）——不要从 semver 机械推导；询问。

## 程序 { #procedure }

1. 确定范围：这次发布实际发这两个包中的哪些（见「一个 tag 会发布什么」）。源码没变的包保持当前版本不动。
2. 更新 `CHANGELOG.md`：将 `## [Unreleased]` 重命名为 `## [X.Y.Z] - <date>`（在其上方保留一个新的空 `Unreleased`）并从 `git log vPREV..HEAD` 完成其条目——精选的用户可见更改，不是 commit subject。若是部分发布，注明本次覆盖哪些包。更新底部的比较链接。行为更改的 PR *可以*直接将其条目添加到 `Unreleased`；发布 PR 是填补任何遗漏的最后保障。`release.yml` 拒绝发布其版本没有带日期的 changelog 节的 tag。
3. 把**范围内**每个 pyproject 的 `version` bump 掉，并抬高必须随之移动的跨包 `>=` 下限（`noeta-sdk` → `noeta-runtime>=X.Y.Z`）。范围外的包不要动。
4. 运行 `uv sync` 以刷新 `uv.lock`。
5. 通过 PR 合并到 `main`，CI 绿色。
6. `git tag vX.Y.Z && git push origin vX.Y.Z` —— `release.yml` 构建前端 + 所有 wheels，并通过 PyPI 可信发布把范围内的包发出去（无存储 token）。

## 验证 { #verification }

使用 `uv pip install --no-cache noeta-sdk==X.Y.Z` 从 PyPI 安装到干净的 venv（JSON API 和简单索引在 CDN 之后滞后发布一两分钟）并导入发布更改的接口。

部分发布还要看一眼 Actions 运行：范围内的 publish job 应当完成了上传，范围外的应当显示 `no <package>-X.Y.Z wheel — not part of this release; skipping` 这条 notice。某个 job 在你预期它发布时却跳过了，说明第 3 步漏了它的版本 bump。

## 注意 { #notes }

- pypi.org 上的可信发布者环境映射：runtime →（空白 env），sdk → `pypi-sdk`。
- 一个模块必须随着「其 import 的依赖所在的那个 wheel」一起发布。`tests/test_install_smoke.py::test_no_distribution_imports_outside_its_dependency_closure` 静态地强制这一点——它正是用来抓「在 checkout 里能跑，但单装一个包的人拿到 `ModuleNotFoundError`」这类问题的。
