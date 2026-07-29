# VLM 图片输入

## 开关

在 `config.yaml` 中设置：

```yaml
enable_vl: true
```

开启后，前端发送区会出现“📎”图片按钮。后端不会验证模型是否支持视觉，需要用户自行把 `model` 改为视觉模型（如 `gpt-4o`）。

## 前端行为

- 点击按钮可多选图片（`accept="image/*"`）。
- 在输入框里直接粘贴剪切板中的图片也能添加到附件列表。
- 选中图片在输入框下方显示缩略图，可单独删除。
- 发送时把图片转成 base64 `data_url`，随消息一起 POST 到 `/api/sessions/{id}/message`。
- 已发送的消息里，图片显示在文字下方（换行）。
- 点击消息中的图片会弹出深色遮罩层，居中显示大图；点击遮罩背景或按 `Esc` 关闭，右上角有 ✕ 按钮。
- 仅主 session（非 subagent）显示图片按钮；运行中禁用。

## 后端行为

- `/api/sessions/{id}/message` 接收 `message` 和可选的 `images` 数组。
- 图片写入 `sessions/<id>/images/<hash>.ext`，`hash` 是文件内容 SHA256 的前 12 位十六进制字符。
- `UserMessage` 保存 `Image` 元数据，历史文件只引用文件名，不内嵌 base64。
- `History.get_api_messages(image_dir=..., enable_vl=...)` 在构造 LLM 请求时把图片编码为 OpenAI 兼容的 `image_url` content part。
- 当 `enable_vl` 被关闭后，新的图片附件不会被保存，已存在历史中的图片元数据和文件也**不会被删除**，但它们不会出现在发给 LLM 的请求里，从而可以在非视觉模型上继续对话。
- 图片通过 `/api/sessions/{id}/images/{filename}` 读取。

## 相关代码

| 文件 | 说明 |
|------|------|
| `src/trilobite/config.py` | `enable_vl` 默认配置 |
| `src/trilobite/messages.py` | `Image` 类、`UserMessage` 图文支持 |
| `src/trilobite/history.py` | `get_api_messages` 带 `image_dir` 参数 |
| `src/trilobite/agent.py` | `start()` 接收图片、`attach_subscriber` 透传 `enable_vl` |
| `src/trilobite/server.py` | 图片上传、图片读取 endpoint |
| `frontend/src/components/ChatInput.vue` | 图片按钮与上传 |
| `frontend/src/components/UserMessage.vue` | 图片渲染 |
| `frontend/src/store.ts` | `enableVl` 状态与图片元数据透传 |
