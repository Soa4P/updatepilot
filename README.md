# UpdatePilot 🛫

**依赖更新 bot——每个它开出的 PR，到你手里之前已经过一遍完整文件 AI 审查。**

A dependency-update bot for GitHub whose every PR arrives **pre-reviewed by a full-file
AI reviewer** — complete file contents, a deterministic coverage table, and honesty
rules the model cannot argue with. The bot never merges and never approves; the merge
button stays yours.

中文为主，English below.

---

## 它做什么

1. **scan**：读你仓库的依赖清单（pip 的 `==` 精确钉版本、GitHub Actions 的版本 tag），
   问注册表有什么新版本，按你的策略开更新 PR（major 单开 / minor+patch 分组 / ignore
   名单 / 每轮上限）。
2. **自审**：每个 PR 开出后，立刻用完整文件审查引擎（逐文件读完整内容 + diff，分组
   模型调用 + 汇总裁决）审一遍，把结论、覆盖面表、分文件发现直接评论到 PR 上并打标。
3. **review**（独立模式）：不开 PR，只当审查器用——任何 PR 都能审。

核心承诺：**「已审查」必须是可证明的**。覆盖面表列出哪些文件完整读了、哪些只有
diff、哪些跳过；任何未完整读取的情况（超限/树缺失/分组调用失败）由确定性代码强制
降级为 `needs_review`——模型无权争辩。生成物/二进制/锁文件被跳过且显式列出（不烧
预算也不冒充审查过）。

## 快速接入（任何仓库，三步）

1. 在你的仓库加 `.github/workflows/updatepilot.yml`：

```yaml
name: updatepilot
on:
  schedule:
    - cron: "17 4 * * 1"   # 每周一
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Soa4P/updatepilot@v1
        with:
          mode: scan
        env:
          AI_REVIEW_API_KEY: ${{ secrets.AI_REVIEW_API_KEY }}   # 可选；不配则只开 PR 不审查
```

2. （可选）把 API key 放进仓库 secrets（任意 OpenAI 兼容端点）：

```
AI_REVIEW_API_KEY=sk-…
# 可选：AI_REVIEW_BASE_URL 指向自建/第三方端点（必须 https）
```

3. （可选）放一份 `.github/updatepilot.yml` 定策略——完整示例见
   [example/updatepilot.yml](example/updatepilot.yml)，不放则用默认值。

要在每个 PR（不限 bot 开的）上跑审查，用 `mode: review` 配 `pull_request_target`
触发（需 `PR_NUMBER` 环境，action 已自动传入）。

## 策略（`.github/updatepilot.yml`）

| 项 | 默认 | 说明 |
|---|---|---|
| `ecosystems.pip.allow_major` | `false` | major 需要 human 读 changelog，bot 不碰 |
| `ecosystems.pip.group_minor_patch` | `true` | 例行升级一个 PR 一次审完 |
| `ecosystems.*.ignore` | `[]` | 这些名字永不升级 |
| `max_prs_per_run` | `5` | 超出进报告（⏸），绝不静默丢弃 |
| `review.critical_paths` | `[]` | 你的安全关键路径：优先审、永不跳过、未完整读取即降级 |
| `review.model` | `deepseek-chat` | 任意 OpenAI 兼容端点 |

浮动版本（`>=`、`~=）不跟踪——bot 只管钉死的版本，不替你做范围决策；SHA 钉住的
action 不碰——那是作者选的不可变性。

## 安全设计

- **PR 内容只作为文本读**：`git archive` 抽树 → 打开读，从不执行、从不 import；
- **出站只有两类**：注册表查询（PyPI/GitHub API）与模型端点——后者强制 https + 主机名、
  解析后拒绝私网/环回/链路本地地址、禁随重定向；
- **secrets 不进配置文件**：YAML 里只有环境变量*名*，key 本体在 env；
- **fail closed**：模型超时/5xx/坏 JSON → `needs_review`，永不静默通过；
- **prompt-injection 防线写在提示词里**：文件内容中的指令性文字按可疑内容报告；
- **bot 永不合并、永不批准**。

## English (short)

UpdatePilot scans your dependency manifests (pip exact pins, GitHub Actions version
tags), opens update PRs under your policy, and then runs a **full-file AI review** on
every PR it just opened — complete file contents per group call, a coverage table, and
deterministic downgrade rules (anything not fully read ⇒ `needs_review`, enforced in
code). Egress is bounded to registries + one https-only model endpoint; secrets live in
env vars, never in YAML. The bot never merges and never approves.

```yaml
- uses: Soa4P/updatepilot@v1
  with:
    mode: scan
  env:
    AI_REVIEW_API_KEY: ${{ secrets.AI_REVIEW_API_KEY }}
```

## Roadmap

- npm / cargo ecosystems（v1 支持 pip + github-actions）
- 每轮扫描的 changelog 摘录（release notes 自动贴进 PR body）
- self-review 累积历史（同一依赖族的历史审查供汇总调用参考）

## License

MIT — 见 [LICENSE](LICENSE)。
