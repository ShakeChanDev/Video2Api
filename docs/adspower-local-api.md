# AdsPower Local API（项目内摘要）

来源（以官方为准）：
- [AdsPower Local API（中文）](https://localapi-doc-zh.adspower.net/)

最后同步时间：2026-02-13

本文目标：把 AdsPower Local API 的核心调用约定与常用接口，整理成项目内可离线阅读的速查文档（不做整页复制，完整字段以官方为准）。

## 基本约定（来自官方“API 概览”）

- 默认访问地址：
  - `http://local.adspower.net:50325/`
  - `http://localhost:50325/`
- 地址获取：官方说明可从客户端生成的 `local_api` 文件读取（示例路径：Linux `~/.config/adspower_global/cwd_global/source/local_api`）。
- 参数：官方说明“API 的参数均为字符串类型”；`POST` body 为 JSON；非必填参数可不传。
- 频控：官方说明“所有接口每秒最多请求 1 次”（全局）。
- 安全校验（如开启）：`Authorization: Bearer <API_KEY>`（Bearer Token 模式）。

## 通用响应形态（常见）

多数接口以类似结构返回（不同接口可能略有差异）：

```json
{ "code": 0, "msg": "success", "data": { } }
```

失败常见：

```json
{ "code": -1, "msg": "failed", "data": { } }
```

## 接口速查（按功能分组）

说明：
- 下面仅列出“路径与关键入参/出参”，用于工程落地对照；细节（字段枚举、更多可选项、返回完整结构）请回看官方页面。
- v1/v2 并存时，建议项目内固定选一套，并在封装层做兼容。

### 1) 浏览器（启动/关闭/状态）

v1（Query 参数为主）：

| 功能 | 方法 | 路径 | 关键入参（节选） | 关键出参（节选） |
| --- | --- | --- | --- | --- |
| 启动浏览器 | GET | `/api/v1/browser/start` | `user_id`（必填）；可选 `serial_number`、`launch_args`、`headless` 等 | `data.ws.selenium`（host:port）、`data.ws.puppeteer`（ws url）、`data.debug_port`、`data.webdriver` |
| 关闭浏览器 | GET | `/api/v1/browser/stop` | `user_id`（必填） | - |
| 检查启动状态（当前设备） | GET | `/api/v1/browser/active` | `user_id`（必填） | - |
| 检查启动状态（跨设备） | POST | `/api/v1/browser/cloud-active` | `user_ids`（必填） | - |

v2（Body JSON 为主）：

| 功能 | 方法 | 路径 | 关键入参（节选） |
| --- | --- | --- | --- |
| 启动浏览器V2 | POST | `/api/v2/browser-profile/start` | `profile_id`（必填）；可选 `profile_no`、`launch_args`、`headless`、`cdp_mask`、`delete_cache` 等 |
| 关闭浏览器V2 | POST | `/api/v2/browser-profile/stop` | 通常与启动一致（以官方为准） |
| 检查启动状态V2（当前设备） | GET | `/api/v2/browser-profile/active` | `profile_id` / `profile_no`（可选，通常二选一） |

### 2) 环境（新建/更新/查询/删除/清缓存/cookies/分享）

v1：

| 功能 | 方法 | 路径 | 关键入参（节选） |
| --- | --- | --- | --- |
| 新建环境 | POST | `/api/v1/user/create` | `group_id`（必填）、`fingerprint_config`（必填）；可选 `name`、`platform`、`username/password`、`cookie`、代理/指纹等 |
| 更新环境 | POST | `/api/v1/user/update` | `user_id`（必填）+ 其他需要更新的字段 |
| 查询环境 | GET | `/api/v1/user/list` | 支持分页/分组/关键字等过滤（以官方为准） |
| 删除环境 | POST | `/api/v1/user/delete` | `user_ids`（必填，数组） |
| 移动分组 | POST | `/api/v1/user/regroup` | `user_ids`（必填）、`group_id`（必填） |
| 清除缓存 | POST | `/api/v1/user/delete-cache` | 官方文档为准（不同版本字段可能不同） |

v2：

| 功能 | 方法 | 路径 | 关键入参（节选） |
| --- | --- | --- | --- |
| 新建环境V2 | POST | `/api/v2/browser-profile/create` | `group_id`（必填）；可选 `name/remark/platform/username/password/fakey/cookie`、代理、指纹等 |
| 更新环境V2 | POST | `/api/v2/browser-profile/update` | 通常包含 `profile_id`（必填）+ 更新字段（以官方为准） |
| 查询环境V2 | POST | `/api/v2/browser-profile/list` | 支持分页/过滤（以官方为准） |
| 删除环境V2 | POST | `/api/v2/browser-profile/delete` | `profile_id`（必填，官方文档示例为“数组格式”） |
| 清除缓存V2 | POST | `/api/v2/browser-profile/delete-cache` | `profile_id`（必填）、`type`（必填，清除类型枚举） |
| 查询 cookies | GET | `/api/v2/browser-profile/cookies` | `profile_id` / `profile_no`（通常二选一） |
| 分享环境 | POST | `/api/v2/browser-profile/share` | `profile_id`（必填）、`receiver`（必填）；可选 `content`、`share_type` |

### 3) 分组（Group）

| 功能 | 方法 | 路径 | 关键入参（节选） |
| --- | --- | --- | --- |
| 创建分组 | POST | `/api/v1/group/create` | `group_name`（必填） |
| 修改分组 | POST | `/api/v1/group/update` | `group_id`（必填）、`group_name`（必填） |
| 查询分组 | GET | `/api/v1/group/list` | 支持分页/关键字等（以官方为准） |

### 4) 代理（Proxy，v2）

| 功能 | 方法 | 路径 | 备注 |
| --- | --- | --- | --- |
| 创建代理 | POST | `/api/v2/proxy-list/create` | 代理字段（类型/IP/端口/账号密码/备注/标签等）以官方为准 |
| 更新代理 | POST | `/api/v2/proxy-list/update` | 同上 |
| 查询代理 | POST | `/api/v2/proxy-list/list` | 同上 |
| 删除代理 | POST | `/api/v2/proxy-list/delete` | 同上 |

