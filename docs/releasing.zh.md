# 发布 { #releasing }

`noeta-runtime` / `noeta-sdk` / `noeta-agent` 共享一个版本，始终一起发布。对 `packages/noeta-runtime`、`packages/noeta-sdk` 或 `apps/noeta-agent` 的合并行为更改应紧随发布——已发布的包不得落后于 `main`。

## 版本策略 { #version-policy }

- **默认 bump patch**：bug 修复、小的附加 API、打包修复。
- **minor / major**：维护者的明确决定（功能级别或破坏性发布）——不要从 semver 机械推导；询问。

## 程序 { #procedure }

1. 更新 `CHANGELOG.md`：将 `## [Unreleased]` 重命名为 `## [X.Y.Z] - <date>`（在其上方保留一个新的空 `Unreleased`）并从 `git log vPREV..HEAD` 完成其条目——精选的用户可见更改，不是 commit subject。更新底部的比较链接。行为更改的 PR *可以*直接将其条目添加到 `Unreleased`；发布 PR 是填补任何遗漏的最后保障。`release.yml` 拒绝发布其版本没有带日期的 changelog 节的 tag。
2. 将所有三个成员 pyprojects 中的 `version` **以及**锁步 `>=` 跨包下限 bump 到相同值（`noeta-sdk` → `noeta-runtime>=X.Y.Z`；`noeta-agent` → 两者）。
3. 更新 `tests/test_install_smoke.py` 中的版本断言（`test_pyproject_metadata_is_present`）。
4. 运行 `uv sync` 以刷新 `uv.lock`。
5. 通过 PR 合并到 `main`，CI 绿色。
6. `git tag vX.Y.Z && git push origin vX.Y.Z` —— `release.yml` 构建前端 + 所有 wheels 并通过 PyPI 可信发布发布（无存储 token）。

## 验证 { #verification }

使用 `uv pip install --no-cache noeta-sdk==X.Y.Z` 从 PyPI 安装到干净的 venv（JSON API 和简单索引在 CDN 之后滞后发布一两分钟）并导入发布更改的接口。

## 注意 { #notes }

- `noeta-agent` 是**仅 wheel**：其 wheel 强制包含 `../web/*`，sdist 无法到达。本地构建，使用 `uv build --all-packages --wheel`——永远不要普通 `uv build`。
- pypi.org 上的可信发布者环境映射：runtime →（空白 env），sdk → `pypi-sdk`，agent → `pypi-agent`。
