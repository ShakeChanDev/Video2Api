# Sora 非付费额度机制（项目约定）

## 适用范围
- 本文基于 OpenAI Help Center 在 `2026-02-12` 可见的公开规则整理。
- 仅讨论非付费部分：套餐内 included usage 与活动赠送 video gens。
- 不讨论购买 credits 的规则与费率。
- 官方规则可能变更，最终以账号内 `Settings -> Usage` 与官方帮助中心为准。

## 机制总览（非付费）
- 计量单位为 `video gens`（可理解为“生成次数权重单位”）。
- `Sora App / Sora 2` 使用滚动 `24` 小时窗口，不是自然日清零。
- 每次提交会立刻占用额度；当某次提交滑出最近 24 小时窗口，对应额度自动释放。
- 官方可能根据系统负载与风控策略动态限制生成能力，`Unlimited access` 不等于无条件无限制。

## 消耗权重（按官方 Usage limits）
- `10s` 视频：计 `1`
- `15s` 视频：计 `2`
- `25s` 视频：计 `4`

## 非付费额度来源
- 套餐内 included usage（主来源）。
- 邀请活动赠送 video gens（限时、限地区、限资格）：每个有效邀请 `+10`，最多 `100`（10 人）。

## 关于减少查询请求（目标 `1-30` 次/天）

### 总体原则
- 不做常驻时间轮询；默认改为“事件驱动 + 本地账本 + 稀疏对账”。
- `nf/check` 仅用于校准，不作为高频实时源。
- 优先保证“相对准确 + 可解释”，不追求秒级强一致。

### 本地账本（主数据源）
- 每次任务提交时，本地立即按权重扣减估算余额：`10s=-1`、`15s=-2`、`25s=-4`。
- 为每笔扣减记录时间戳；超过 `24h` 自动回补对应额度（滑动窗口回补）。
- 界面展示“估算余额 + 上次校准时间 + 数据状态”。

### 触发 Sora 查询的时机（仅这些）
- 每日首次查询：每天第一次打开页面或第一次发起任务前，校准 `1` 次。
- 周期对账：每 `8-12h` 校准 `1` 次（建议每天 `1-2` 次）。
- 低余额保护：当估算余额 `<=2` 且即将提交新任务时，提交前再校准 `1` 次。
- 异常修正：出现 `429/5xx/网络异常/会话异常` 时才触发校准。
- 手动刷新：允许，但设置冷却与每日上限。

### 请求预算（单 profile）
- 固定预算：`1` 次/天（每日首次）。
- 对账预算：`1-2` 次/天。
- 低余额保护预算：`0-10` 次/天（只在低余额且要提交时触发）。
- 异常预算：`0-3` 次/天。
- 手动刷新预算：`0-3` 次/天（可配置为默认关闭或仅管理员可用）。
- 日总预算上限：硬性限制 `30` 次/天；超过后仅返回本地估算值与提示。

### 控流与退避
- 请求最小间隔：同一 `profile` 建议 `>= 60s`。
- 并发去重：同一时刻多个调用方只发 `1` 个查询，其余复用结果。
- 失败退避：`30s -> 60s -> 120s -> 300s`，并加入 `±20%` 抖动。
- 退避期间不追加新查询，继续使用本地账本与最后一次成功校准值。

### 准确性边界
- 若所有生成都经由本系统，通常可维持“接近实时的相对准确”。
- 若用户在系统外直接使用 Sora，会产生漂移；在下一次对账时纠正。
- 当触发日上限（`30`）后，允许短时误差，次日或下一次预算窗口再校准。

## 官方参考（核对日期：`2026-02-12`）
- [Creating videos with Sora](https://help.openai.com/en/articles/12460853-creating-videos-on-the-sora-app)
- [Using Credits for Flexible Usage in ChatGPT (Free/Go/Plus/Pro) & Sora](https://help.openai.com/en/articles/12642688-using-credits-for-flexible-usage-in-chatgpt-freegopluspro-sora)
- [Invite friends to Sora and earn video generations](https://help.openai.com/en/articles/12934544-invite-friends-to-sora-and-earn-video-generations)
- [Generating videos on Sora](https://help.openai.com/en/articles/9957612)
- [Sora - Billing FAQ](https://help.openai.com/en/articles/10245774)
