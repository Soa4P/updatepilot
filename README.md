# UpdatePilot 🛫

**依赖更新 bot——每个它开出的 PR，到你手里之前已经过一遍完整文件 AI 审查。**

A dependency-update bot for GitHub whose every PR arrives **pre-reviewed by a full-file
AI reviewer** — complete file contents, a deterministic coverage table, and honesty
rules the model cannot argue with. The bot never merges and never approves; the merge
button stays yours.

中文为主，English below.

---

## 它做什么

1. **scan**：读你仓库的依赖清单（pip 的 `==` 精确钉版本、npm 的精确 pin、cargo 的
   `Cargo.toml` 依赖表、GitHub Actions 的版本 tag），问注册表有什么新版本，按你的策略
   开更新 PR（major 单开 / minor+patch 分组 / ignore 名单 / 每轮上限）——**上游 release
   notes 自动贴进 PR body**（GitHub 托管仓库，best-effort）。npm / cargo 的 PR body 同时
   提醒刷新 lockfile：bot 只改清单，不动 lock。
   **本轮顺序按影响面排**：`max_prs_per_run` 有名额上限，顺序就是行为——仓库里被更多文件
   引用的依赖先开 PR，超出的进报告（⏸）等下一轮。
2. **自审**：每个 PR 开出后，立刻用完整文件审查引擎（逐文件读完整内容 + diff，分组
   模型调用 + 汇总裁决）审一遍，把结论、覆盖面表、分文件发现直接评论到 PR 上并打标；
   **行级内联评论**：发现里 location 是纯行号且落在变更行内的，作为 review 内联评论
   锚到那一行（≤10 条，其余留在汇总——伪精确比不精确更糟）。
3. **review**（独立模式）：不开 PR，只当审查器用——任何 PR 都能审。
4. **audit**（仓库巡检）：定期扫整个代码库（安全关键面优先、生成物跳过、≤20 组预算），
   发现写入带 `updatepilot:audit` 标签的 issue——**与上一份签名去重只报新增**，产出
   是草稿、处置归维护者（设计取舍见 [docs/DESIGN-review-bot.md](docs/DESIGN-review-bot.md)）。
5. **coverage 标注**：工作区存在 `coverage.json`（你自己的 CI 用 pytest-cov 生成）
   时，审查评论自动附变更文件的未覆盖标注——**bot 读不跑，数据不出仓**。

核心承诺：**「已审查」必须是可证明的**。覆盖面表列出哪些文件完整读了、哪些只有
diff、哪些跳过；任何未完整读取的情况（超限/树缺失/分组调用失败）由确定性代码强制
降级为 `needs_review`——模型无权争辩。生成物/二进制/锁文件被跳过且显式列出（不烧
预算也不冒充审查过）；**一个文件都没读到**（例如只改锁文件的 PR）同样降级——
`ai:safe` 是对「读到了什么」的声明，零读取不配拥有它。

**针对审查者的指令也是确定性降级项**：PR 标题、正文、diff 或文件内容里出现
「忽略以上指令」「把本 PR 判为安全」之类的话，无论模型怎么答，结论都降为
`needs_review`。系统提示已经告诉模型把这类文字当作被审内容，但「模型没被说服」不是
可以依赖的安全属性——标签不能只靠它。

审查结论的**发布**也是承诺的一部分：评论或标签写入失败时工作流按失败退出，而不是
绿着跑完、PR 上却没有任何结论可查。

审查只读**仓库内的普通文件**：符号链接一律跳过，`git archive` 抽取的树里带链接直接
拒绝——否则一个 `notes.py -> ~/.ssh/id_rsa` 就能把宿主机文件送进模型和公开 issue。

## 快速接入（任何仓库，三步）

1. 在你的仓库加 `.github/workflows/updatepilot.yml`：

```yaml
name: updatepilot
on:
  schedule:
    - cron: "17 4 * * 1"   # 每周一
  workflow_dispatch:

permissions:
  contents: write          # 开分支/推提交——最小集，不要给起 workflow 或 admin
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
| `ecosystems.cargo.allow_major` | `false` | cargo 的 **0.x→0.y 也按 major 处理**（semver §4：0.x 不保证兼容），默认同样不碰 |
| `ecosystems.*.ignore` | `[]` | 这些名字永不升级 |
| `max_prs_per_run` | `5` | 超出进报告（⏸），绝不静默丢弃 |

已支持：`pip`（`requirements*.txt` / `pyproject.toml` 的 `==`）、`npm`（`package.json`
精确 pin）、`cargo`（`Cargo.toml` 的 `[dependencies]` / `[dev-dependencies]` /
`[build-dependencies]` / `[workspace.dependencies]` / `[dependencies.foo]` /
`[target.*.dependencies]`，字符串与内联表两种写法）、`github_actions`（workflow 里的
版本 tag）。

浮动版本（`>=`、`~=）、`path` / `git` / `workspace = true` 的依赖、以及不带完整
`x.y.z` 的写法（`"1"`、`"*"`）都不跟踪——bot 只管能比较、能回写的版本，不替你做范围
决策；SHA 钉住的 action 不碰——那是作者选的不可变性。

## 安全设计

- **PR 内容只作为文本读**：`git archive` 抽树 → 打开读，从不执行、从不 import；
- **出站只有两类**：注册表查询（PyPI / npm / crates.io / GitHub API）与模型端点——后者强制
  https + 主机名、解析后拒绝私网/环回/链路本地地址、禁随重定向；
- **secrets 不进配置文件**：YAML 里只有环境变量*名*，key 本体在 env；
- **fail closed**：模型超时/5xx/坏 JSON → `needs_review`，永不静默通过；
- **prompt-injection 防线写在提示词里**：文件内容中的指令性文字按可疑内容报告；
- **bot 永不合并、永不批准**；
- **审查只读仓内普通文件**：符号链接跳过，抽取的树里带链接直接拒绝——一个
  `notes.py -> ~/.ssh/id_rsa` 否则就会变成模型输入。

## 审查质量怎么量

「审查好不好」曾经只能靠读评论形成印象。现在有一条可重复的度量：

```bash
python -m updatepilot.cli quality            # 确定性半场（无需 key，CI 跑的就是它）
python -m updatepilot.cli quality --with-model   # 真模型半场（需要审查 key）
```

语料在 `tests/corpus/review_quality.jsonl`，每条标了期望结论，并分成两类：

- **确定性**：模型无论答什么都不能是 `safe`（针对审查者的指令、只改锁文件、文件未读全、
  diff 截断、安全关键面被跳过）。CI 用一个「永远答 safe」的桩模型跑真实管线，任何一条
  被判 safe 就让构建失败——测的是代码，不是模型；
- **模型判定**：没有结构特征的后门、外发、鉴权短路。只有真模型能抓到，因此不进 CI 门禁，
  由 `--with-model` 报出非安全召回率与 safe 精确率。

报告分两个口径（`all` / `deterministic`），因为桩模型抓不到后门是桩的问题，不是产品的
问题——混在一起报会得到一个看着像证据、其实在测桩的数字。

## English (short)

UpdatePilot scans your dependency manifests (pip exact pins, npm exact pins, cargo
`Cargo.toml` dependency tables, GitHub Actions version tags), opens update PRs under
your policy — ordered by **blast radius** (the run's PR ceiling spends its slots on the
dependencies the most files reference) and with **upstream release notes attached**
(GitHub-hosted repos, best-effort) — and then runs a **full-file AI
review** on every PR it just opened: complete file contents per group call, a coverage
table, and deterministic downgrade rules (anything not fully read ⇒ `needs_review`,
enforced in code). Egress is bounded to registries + one https-only model endpoint;
secrets live in env vars, never in YAML. The bot never merges and never approves.

```yaml
- uses: Soa4P/updatepilot@v1
  with:
    mode: scan
  env:
    AI_REVIEW_API_KEY: ${{ secrets.AI_REVIEW_API_KEY }}
```

**建议用完整 SHA 钉住 `uses:`**（Action 安全惯例）：仓库页 latest release 附 commit
SHA，或 `gh api repos/Soa4P/updatepilot/tags --jq '.[].commit.sha'` 取用。

## Roadmap

- self-review 累积历史（同一依赖族的历史审查供汇总调用参考）
- 更细的影响面（各生态的静态 import 图，替代"提到它的文件数"这一代理指标）

## License

MIT — 见 [LICENSE](LICENSE)。
