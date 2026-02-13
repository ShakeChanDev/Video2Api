# NopeCHA Extension 反检测策略分析（基于更新日志与扩展文件）

## 文档信息
- 采集时间：`2026-02-12`
- 分析对象：`NopeCHALLC/nopecha-extension`
- 分析范围：
  - 开源归档：`oss-archive-0.3.3`（可读源码）
  - 闭源构建包：`0.5.4`、`0.5.5` 的 `chromium_automation.zip`
- 目标：识别其“反检测”实现方向与版本演进，不讨论实战绕过方法。

## 证据来源
1. 仓库 README（含发布时间线与历史说明）  
   - [README](https://github.com/NopeCHALLC/nopecha-extension)
2. Release Notes（重点版本）  
   - [0.4.9](https://github.com/NopeCHALLC/nopecha-extension/releases/tag/0.4.9)  
   - [0.5.4](https://github.com/NopeCHALLC/nopecha-extension/releases/tag/0.5.4)  
   - [0.5.5](https://github.com/NopeCHALLC/nopecha-extension/releases/tag/0.5.5)
3. 开源版本源码（`oss-archive-0.3.3`）静态检视
4. 关闭源码后发布的扩展包静态检视（`manifest + 混淆脚本关键词/结构`）

## 版本演进（关键节点）

### 2024-05-22（v0.4.9）
- 日志：`Add retry delay to avoid rate limits.`
- 解释：引入重试延时，减少被频率风控触发的概率（节奏控制层）。

### 2025-12-04（v0.5.4）
- 日志：`Updated mouse actions into an undetectable implementation`
- 涉及验证码：`hCaptcha / FunCAPTCHA / reCAPTCHA / Turnstile`（Chromium）
- 解释：重点投入在“输入事件行为层”的仿真。

### 2026-01-23（v0.5.5）
- 日志：`Improved mouse action implementation for all CAPTCHAs`
- 解释：延续 v0.5.4 方向，强调稳定性与全类型覆盖。

## 反检测策略分层（可验证）

### 1. 行为层：事件级输入仿真
- 在闭源构建包中可检到 `PointerEvent`、`MouseEvent`、坐标与路径相关逻辑（混淆后仍可命中关键词）。
- 结论：不是单纯 DOM `.click()`，而是向更接近真实用户输入事件流演进。

证据（0.5.5 包）：
- `/tmp/nopecha-0.5.5-auto/captcha/hcaptcha.js`
- `/tmp/nopecha-0.5.5-auto/captcha/recaptcha.js`
- `/tmp/nopecha-0.5.5-auto/background.js`（含轨迹与可视化调试痕迹）

### 2. 节奏层：延迟与重试抖动
- 配置中长期存在 `*_solve_delay_time` 与 `*_solve_delay`。
- 早期 UI 文案直接写明 “Adds a delay to avoid detection.”
- 结论：通过时间策略降低固定节奏特征。

证据：
- `/tmp/nopecha-0.5.5-auto/manifest.json`（`hcaptcha_solve_delay_time` 等）
- `/tmp/nopecha-oss-0.3.3/popup.html:112`
- `/tmp/nopecha-oss-0.3.3/utils.mjs:149`

### 3. 流程层：可见性与准备态门控
- 开源版本中存在 reCAPTCHA `iframe` 可见性检查，仅在可见 frame 上推进流程。
- 结论：降低“未展示即操作”的异常行为特征。

证据：
- `/tmp/nopecha-oss-0.3.3/recaptcha.js:336`

### 4. 请求层：hCaptcha 请求链路与 motion 数据处理
- 开源版本 `hcaptcha_hook.js` 会在 `checkcaptcha` 请求前处理 `motionData`。
- 闭源包（0.5.5）仍可检到 `XMLHttpRequest`、`setRequestHeader`、`getcaptcha` 等链路关键词。
- 结论：其反检测不仅在前端点击层，也包括请求/行为数据对齐层。

证据：
- `/tmp/nopecha-oss-0.3.3/hcaptcha_hook.js:733`
- `/tmp/nopecha-0.5.5-auto/captcha/hook.js`

### 5. 作用域层：按站点与类型控制
- 提供 `disabled_hosts`，可关闭指定站点自动化。
- 结论：减少全局扫射式行为，降低异常覆盖面。

证据：
- `/tmp/nopecha-0.5.5-auto/manifest.json:144`
- `/tmp/nopecha-oss-0.3.3/utils.mjs:155`

### 6. 注入面收敛（0.5.4 -> 0.5.5）
- `manifest` 差异显示：0.5.5 去掉了 hCaptcha/recaptcha/turnstile 的通用 `eventhook.js` 注入，新增 hCaptcha challenge 场景定向 `captcha/hook.js`（`world: MAIN`）。
- 推断：注入面更精细化，减少不必要脚本暴露，同时提高对 challenge 上下文的控制力。
- 该条为“基于 manifest 差异的推断”。

证据：
- `/tmp/nopecha-0.5.4-auto/manifest.json`
- `/tmp/nopecha-0.5.5-auto/manifest.json`

## 差异快照（0.5.4 vs 0.5.5）
- 两个包都保留：
  - `PointerEvent` / `MouseEvent` 命中
  - `solve_delay` 配置
  - `disabled_hosts` 配置
- 主要变化：
  - `eventhook.js` 从通用注入中移除
  - 新增 hCaptcha challenge 场景 `captcha/hook.js` 注入

## 结论
该项目的反检测策略可归纳为：  
`行为仿真（鼠标事件） + 节奏控制（延迟/重试） + 挑战流程门控（可见性/ready） + 请求与运动数据处理（hCaptcha hook） + 作用域控制（disabled hosts）`。

从可验证证据看，它并非“单点伪装”，而是多层协同；其中 `0.5.4` 与 `0.5.5` 的主升级方向集中在鼠标行为不可检测性与稳定性。

## 面向 Video2Api 的可借鉴点（进一步分析）

以下是基于你当前实现与 NopeCHA 思路的对照结论，按“可落地价值/风险”排序。

### A. 可直接借鉴（优先）

#### 1) 建立“事件级动作层”，替换大量 DOM 直点
- 可借鉴点：NopeCHA 在 `0.5.4/0.5.5` 明确升级鼠标行为实现，核心是更接近真实输入事件序列，而不是单一 `node.click()`。
- 你当前现状（建议改造位置）：
  - `app/services/ixbrowser/sora_publish_workflow.py:2028`
  - `app/services/ixbrowser/sora_publish_workflow.py:2097`
  - `app/services/ixbrowser/sora_publish_workflow.py:2160`
  - `app/services/ixbrowser/sora_publish_workflow.py:2424`
  - `app/services/ixbrowser/sora_publish_workflow.py:2488`
- 借鉴方式：
  - 抽一层统一动作方法（如 `_human_click(locator_or_element)`），内部使用 `page.mouse.move + down + up`，并加入小幅随机轨迹与停顿。
  - 对关键按钮优先使用 Playwright 原生点击链路（可见、可交互校验 + 重试），最后才回退 DOM click。
- 预期收益：降低“脚本直点”特征，提高 UI 变化时鲁棒性。

#### 2) 固定等待改为“抖动区间 + 状态门控”
- 可借鉴点：NopeCHA 从早期就有 `solve_delay` 与 retry 节奏控制，重点不是“更快”，而是“非机械固定节奏”。
- 你当前现状（固定 800/1200/1500ms 很多）：
  - `app/services/ixbrowser/sora_publish_workflow.py:2136`
  - `app/services/ixbrowser/sora_publish_workflow.py:2458`
  - `app/services/ixbrowser/sora_generation_workflow.py:83`
- 借鉴方式：
  - 增加统一延时函数（如 `_wait_jitter(base_ms, ratio=0.25)`），将固定等待改为区间抖动。
  - 优先“等状态”而不是“纯 sleep”：可见性、enabled、DOM ready、请求完成信号。
- 预期收益：降低节奏指纹，减少无效等待，提升吞吐稳定性。

#### 3) 输入链路尽量走真实输入（键盘/输入事件序列）
- 可借鉴点：NopeCHA 关注事件链路完整性；同理，文本输入也应避免过于“程序化”赋值。
- 你当前现状（直接改 `value/textContent`）：
  - `app/services/ixbrowser/sora_publish_workflow.py:2219`
  - `app/services/ixbrowser/sora_publish_workflow.py:2225`
  - `app/services/ixbrowser/sora_publish_workflow.py:2291`
  - `app/services/ixbrowser/sora_publish_workflow.py:2295`
- 借鉴方式：
  - 首选 `locator.fill` 或 `click + keyboard.type`（可加轻微打字间隔抖动）。
  - 仅在控件特殊（contenteditable/富文本异常）时回退 `evaluate` 赋值。
- 预期收益：输入行为更接近真实交互，减少前端风控误判概率。

### B. 条件借鉴（建议灰度）

#### 4) 注入面收敛：按 stage / host / frame 精细注入
- 可借鉴点：NopeCHA `0.5.5` 相比 `0.5.4` 收紧了通用注入范围，改成更有针对性的注入。
- 你当前现状：
  - 基础脚本：`app/services/ixbrowser/stealth_scripts.py`
  - 统一注入入口：`app/services/ixbrowser/browser_prep.py`
  - 备注：已移除 create 阶段移动脚本与 Playwright UA 覆盖，统一使用指纹浏览器 Profile 的 UA/指纹配置。
- 借鉴方式：
  - 维持“兜底脚本 + 插件”的结构，但增加 host 白名单（例如仅 `sora.chatgpt.com`）。
  - 对高风险属性覆盖（如 `platform/hardwareConcurrency`）改为按阶段/场景可配置开关。
- 预期收益：减少不必要脚本暴露与兼容性副作用。

#### 5) 增加“作用域开关”避免全局扫射
- 可借鉴点：NopeCHA 有 `disabled_hosts`，可按站点关闭自动化。
- 你当前现状：已有 `blocking_mode` 等配置化框架（UA 不再由 Playwright 覆盖），扩展点清晰。
  - `app/core/config.py`
  - `.env.example`
- 借鉴方式：
  - 新增如 `PLAYWRIGHT_STEALTH_DISABLED_HOSTS`、`PLAYWRIGHT_HUMAN_ACTION_ENABLED_HOSTS`。
  - 在 `_prepare_sora_page` 里按 host 动态决定是否注入脚本、是否启用“人类动作层”。
- 预期收益：降低误伤面，便于线上快速回退。

#### 6) 利用你已有 CF 事件做闭环自适应
- 可借鉴点：NopeCHA 的演进体现了“挑战反馈 -> 行为策略迭代”。
- 你当前现状：已具备 CF 侦测与去重上报。
  - `app/services/ixbrowser/browser_prep.py:181`
  - `app/services/ixbrowser/browser_prep.py:205`
  - `app/services/ixbrowser/browser_prep.py:255`
- 借鉴方式：
  - 记录“触发挑战前最后 N 个动作元数据”（动作类型、等待区间、阶段、host），形成可观测样本。
  - 基于统计结果动态调节：例如某阶段挑战率高，则扩大抖动区间或降低并发。
- 预期收益：从“静态规则”升级为“反馈驱动调参”。

### C. 不建议直接借鉴（高风险/高维护）

#### 7) 不建议做验证码请求数据伪造/重写
- NopeCHA 在 hCaptcha 链路里有 `hook` 与 `motionData` 处理痕迹，但该方向高风险、强耦合、维护成本高，且合规风险显著。
- 对你项目建议：坚持“浏览器真实上下文 + 合法交互 + 节奏/作用域优化”，避免进入协议对抗层。

#### 8) 不建议过度资源拦截
- 你的 `light` 模式（仅 `media`）已经是相对稳妥折中：
  - `app/services/ixbrowser/browser_prep.py:107`
- 不建议默认走激进拦截，以免破坏页面行为并产生异常特征。

## 建议落地顺序（最小风险）
1. 先做动作层抽象：把 `node.click()` 的主要路径收口到统一 helper。  
2. 再做等待抖动：用统一 jitter 函数替换固定 800/1200/1500ms。  
3. 最后做 host 作用域配置 + CF 反馈调参闭环。  

这样可以在不改动核心业务流程的前提下，逐步提升“行为自然度 + 稳定性 + 可回退性”。

## 局限性说明
1. `0.5.x` 为闭源混淆构建，无法做完整语义级源码审计。
2. 本文结论是静态分析结论，不等价于“对任意检测系统都有效”。
3. 未进行真实站点在线对抗测试；结果不应外推为“绝对不可检测”。

## 复现实验步骤（静态分析）
```bash
# 1) 下载构建包
curl -L -o /tmp/nopecha-0.5.4-chromium_automation.zip \
  https://github.com/NopeCHALLC/nopecha-extension/releases/download/0.5.4/chromium_automation.zip
curl -L -o /tmp/nopecha-0.5.5-chromium_automation.zip \
  https://github.com/NopeCHALLC/nopecha-extension/releases/download/0.5.5/chromium_automation.zip

# 2) 解包
mkdir -p /tmp/nopecha-0.5.4-auto /tmp/nopecha-0.5.5-auto
unzip -q /tmp/nopecha-0.5.4-chromium_automation.zip -d /tmp/nopecha-0.5.4-auto
unzip -q /tmp/nopecha-0.5.5-chromium_automation.zip -d /tmp/nopecha-0.5.5-auto

# 3) 核对 manifest 变化
diff -u /tmp/nopecha-0.5.4-auto/manifest.json /tmp/nopecha-0.5.5-auto/manifest.json

# 4) 关键词体检（混淆代码快速定位）
rg -n -o "PointerEvent|MouseEvent|solve_delay|disabled_hosts|getcaptcha|XMLHttpRequest|setRequestHeader" \
  /tmp/nopecha-0.5.5-auto/manifest.json \
  /tmp/nopecha-0.5.5-auto/captcha/*.js \
  /tmp/nopecha-0.5.5-auto/background.js
```
