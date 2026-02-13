# ixBrowser Local API（项目内摘要）

来源（以官方为准）：
- [ixBrowser Local API v2（中文）](https://www.ixbrowser.com/doc/v2/local-api/cn)

最后同步时间：2026-02-13

本文目标：把我们在 `Video2Api` 里实际会用到的 ixBrowser 本地接口，整理成「可离线阅读的速查文档」。不做整页搬运；更完整/最新的字段与说明请回看官方文档。

## 基本约定

- Base URL：`IXBROWSER_API_BASE`（项目配置项 `ixbrowser_api_base`）
  - 默认：`http://127.0.0.1:53200`
- 传输：HTTP `POST` + `application/json`
- 通用响应结构：
  - `error.code == 0`：成功
  - `error.code != 0`：失败（通常配合 `error.message`）
  - 成功数据在 `data`

安全提醒：
- `profile-list`/`profile-open` 等返回中可能包含账号、密码、2FA、cookie、代理等敏感信息。禁止把这些内容写入日志、截图、PR 或对话。

## 我们项目使用到的接口（v2）

下面的“请求示例”以项目当前实现为准（可对照代码），字段名按官方文档口径。

### 1) 分组列表

- `POST /api/v2/group-list`
- 用途：分页获取分组（前端展示 + 扫描入口）。
- 请求示例：

```json
{ "page": 1, "limit": 200, "title": "" }
```

- 关键响应字段：
  - `data.total`
  - `data.data[]`：`{ id, title }`

对应实现：
- `app/services/ixbrowser/groups.py`

### 2) 窗口（环境）列表

- `POST /api/v2/profile-list`
- 用途：分页获取全部窗口（用于按 group 聚合、以及缓存代理绑定信息）。
- 请求示例：

```json
{
  "profile_id": 0,
  "name": "",
  "group_id": 0,
  "tag_id": 0,
  "page": 1,
  "limit": 200
}
```

- 关键响应字段（节选）：
  - `data.total`
  - `data.data[]`：
    - `profile_id`, `name`
    - `group_id`, `group_name`
    - `proxy_mode`, `proxy_id`, `proxy_type`, `proxy_ip`, `proxy_port`
    - `real_ip`

对应实现：
- `app/services/ixbrowser/groups.py`

### 3) 打开窗口并获取调试地址（ws / debugging_address）

- `POST /api/v2/profile-open`
- 用途：打开指定窗口，拿到 `ws` 或 `debugging_address`，后续使用 Playwright CDP 连接。
- 项目侧请求示例（节选）：

```json
{
  "profile_id": 1234,
  "args": ["--disable-extension-welcome-page"],
  "load_extensions": true,
  "load_profile_info_page": false,
  "cookies_backup": true,
  "cookie": ""
}
```

- 可选字段（项目里会用到）：
  - `headless: true|false`：不同版本支持差异较大；项目会在 headless 失败时降级为普通打开（见 `profiles.py`）。
- 关键响应字段（节选）：
  - `data.ws`：形如 `ws://127.0.0.1:<port>/devtools/browser/<id>`（优先使用）
  - `data.debugging_address` / `data.debugging_port`：可用于拼出 `http://<debugging_address>` 再 CDP 连接
  - `data.pid`：浏览器进程 id
  - `data.webdriver`：webdriver 路径（如有）

常见异常与处理（项目现状）：
- `111003`（已打开/状态已打开）：先尝试附着已开窗口；若拿不到调试地址则执行“关闭后重开”；必要时做 `open-state-reset` 兜底。
- `2012`（云备份中）：在静默打开场景会触发降级策略（避免阻断流程）。
- `2007`（窗口不存在）：直接失败（通常是窗口 id 录入/分组不一致）。

对应实现：
- `app/services/ixbrowser/profiles.py`

### 4) 查询当前“可连接”的已打开窗口（优先用 native-client）

- `POST /api/v2/native-client-profile-opened-list`
  - 用途：获取当前机器真正已打开的窗口列表，并包含 `ws/debugging_address`。
  - 请求示例：`{}`
  - 关键响应字段：`data[]` 中包含 `profile_id`, `ws`, `debugging_address`, `debugging_port`, `open_time` 等。

- `POST /api/v2/profile-opened-list`
  - 备注：官方示例中该接口返回 `profile_id/last_opened_time` 等“历史信息”，在部分版本不包含 `ws/port`，不能用于“判断是否可连接”。

对应实现：
- `app/services/ixbrowser/profiles.py`

### 5) 关闭窗口

- `POST /api/v2/profile-close`
  - 请求示例：`{ "profile_id": 1234 }`
  - 常见错误：`1009`（未找到进程）。项目按“已关闭”处理，并尝试 `close-in-batches` 兜底（兼容状态不一致）。

- `POST /api/v2/profile-close-in-batches`
  - 请求示例：`{ "profile_id": [1234, 5678] }`

对应实现：
- `app/services/ixbrowser/profiles.py`

### 6) 重置打开状态（兜底）

- `POST /api/v2/profile-open-state-reset`
- 用途：修复“窗口已关闭但状态仍显示已打开”等卡死状态（仅兜底使用）。
- 请求示例：`{ "profile_id": 1234 }`

对应实现：
- `app/services/ixbrowser/profiles.py`

### 7) 代理列表 / 增删改

- `POST /api/v2/proxy-list`
  - 请求示例：

```json
{ "page": 1, "limit": 200, "id": 0, "type": 0, "proxy_ip": "", "tag_id": 0 }
```

- `POST /api/v2/proxy-create` / `POST /api/v2/proxy-update`
  - 常用字段：`proxy_type`, `proxy_ip`, `proxy_port`, `proxy_user`, `proxy_password`, `tag`, `note`
  - 返回：`data` 通常为代理 id（创建）或成功标记（更新）

- `POST /api/v2/proxy-delete`
  - 请求示例：`{ "id": 123 }`

对应实现：
- `app/services/ixbrowser/proxies.py`

### 8) 给窗口绑定“自定义代理”

- `POST /api/v2/profile-update-proxy-for-custom-proxy`
- 项目侧请求示例（核心字段）：

```json
{
  "profile_id": 1234,
  "proxy_info": {
    "proxy_mode": 2,
    "proxy_check_line": "global_line",
    "proxy_type": "http",
    "proxy_ip": "1.2.3.4",
    "proxy_port": "8080",
    "proxy_user": "",
    "proxy_password": ""
  }
}
```

备注：
- 官方文档中明确 `proxy_mode=2` 表示自定义代理（custom proxy）。

对应实现：
- `app/services/ixbrowser/proxies.py`

## 错误码速记（与项目相关的节选）

以官方错误码表为基准，这里只列出项目里会“显式处理/容易踩坑”的：

- `1009`：未找到进程（关闭窗口时常见，可能是状态与本地进程短暂不一致）
- `111003`：窗口已打开/被标记为已打开（需要附着/重试/重置状态）
- `2007`：窗口不存在
- `2012`：窗口正在云备份，稍后再试
- `1008`：官方表述为“权限受限”；但实测在并发/繁忙时也可能出现。项目当前对 `1008` 做了指数退避重试（见 `app/services/ixbrowser_service.py` 的 `_post`）。

## 官方接口索引（v2，未必都在本项目使用）

下面仅列出官方文档中出现的路径，方便检索（具体字段请回看官方页面）：

```text
/api/v2/empty-recycle-bin
/api/v2/gateway-list
/api/v2/gateway-switch
/api/v2/group-create
/api/v2/group-delete
/api/v2/group-list
/api/v2/group-update
/api/v2/native-client-profile-opened-list
/api/v2/profile-clear-cache
/api/v2/profile-clear-cache-and-cookies
/api/v2/profile-close
/api/v2/profile-close-in-batches
/api/v2/profile-copy
/api/v2/profile-create
/api/v2/profile-delete
/api/v2/profile-get-cookies
/api/v2/profile-list
/api/v2/profile-open
/api/v2/profile-open-state-reset
/api/v2/profile-open-with-random-fingerprint
/api/v2/profile-opened-list
/api/v2/profile-opened-list-arrange-tile
/api/v2/profile-random-fingerprint-configuration
/api/v2/profile-transfer-cancel
/api/v2/profile-transfer-code-create
/api/v2/profile-transfer-code-import
/api/v2/profile-transfer-record-list
/api/v2/profile-update
/api/v2/profile-update-cookies
/api/v2/profile-update-groups-in-batches
/api/v2/profile-update-proxy-for-api-extraction
/api/v2/profile-update-proxy-for-custom-proxy
/api/v2/profile-update-proxy-for-purchased-traffic-package
/api/v2/profile-update-proxy-to-purchased-mode
/api/v2/proxy-create
/api/v2/proxy-delete
/api/v2/proxy-list
/api/v2/proxy-tag-create
/api/v2/proxy-tag-delete
/api/v2/proxy-tag-list
/api/v2/proxy-tag-update
/api/v2/proxy-update
/api/v2/tag-create
/api/v2/tag-delete
/api/v2/tag-list
/api/v2/tag-update
/api/v2/traffic-package-list
```

