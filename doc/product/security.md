# 访问安全（token auth）

Web 服务器（`-s` 模式）用访问 token 做基本防护（类似 Jupyter Notebook），防止局域网内其他人直接访问会话数据。token **首次启动时生成**，持久化到 `~/.config/trilobite/access_token.txt`；之后的启动**读取已有 token 复用**（访问链接保持不变），只有文件不存在时才重新生成。启动时打印版本横幅、带 token 的访问链接、以及单独一行的 key：

```
Trilobite 1.1.7
Trilobite web UI: http://127.0.0.1:2345/?token=<token>
Access key: <token>
Access key saved to ~/.config/trilobite/access_token.txt
```

`Trilobite <version>` 版本横幅与 CLI 模式共用 `src/trilobite/version.py` 的 `get_version()`（读 `importlib.metadata` 的 `trilobite-code` 包版本）。

## 认证流程

两种用户路径：

1. **直接打开链接**：页面加载后前端检测到 URL 中的 `?token=`，立即 POST `/api/auth/login` 换取 HttpOnly 会话 cookie，并用 `history.replaceState` 把 token 从地址栏移除。之后所有请求带 cookie。
2. **无链接打开**：`GET /api/auth/status` 返回未认证，前端展示 key 输入弹框；输入正确后同一 login 端点换 cookie。

`POST /api/auth/login` 校验通过后设置 `trilobite_token` cookie（HttpOnly + SameSite=strict，会话级过期）。token 校验失败返回 401。

## 保护范围

- 除 `/api/auth/status`、`/api/auth/login` 外的所有 `/api/*` 请求（含 SSE `/stream`、图片 serve）由 HTTP 中间件校验 cookie，未认证返回 401。
- 静态资源（编译后的前端 bundle）不设防：登录弹框本身需要能渲染，前端代码不含敏感数据。
- 前端收到任意 API 401 时触发 `trilobite:unauthorized` 事件，重新显示 key 弹框（例如服务器重启后 cookie 失效）。

## 其他

- token 持久化在 `access_token.txt`，重启后不变，已派发的 cookie 继续有效；删除该文件即可重置 token。
- CLI 模式（`-t`/`-c`）不走网络，不生成 token。
- 明文 HTTP 传输；如需加密请自行在外部套 HTTPS 反向代理。
