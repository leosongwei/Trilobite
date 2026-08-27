# VLM 图片输入

## 开关

视觉能力是**每个模型定义**的一部分：在 `config.yaml` 的 `models` 列表里为对应模型设置：

```yaml
models:
  - name: "GPT-4o"
    model: "gpt-4o"
    api_key: "sk-xxx"
    api_url: "https://api.openai.com/v1"
    enable_vl: true
```

开启后，前端发送区会出现"📎"图片按钮。后端不会验证模型是否支持视觉，需要用户自行把 `enable_vl` 配在视觉模型上。图片按钮跟随会话当前模型实际配置的模态：前端从会话的 `model` × 模型定义列表实时派生开关状态，任何改变当前会话模型的操作（本页应用或另一窗口的模型切换经轮询同步）都会立即更新按钮可见性——切到 `enable_vl: false` 的模型后图片按钮消失，切回支持视觉的模型后重新出现。新图片仅在开启时保存；关闭期间历史中已有的图片元数据和文件保留（详见下方后端行为）。

## 前端行为

- 点击按钮可多选图片（`accept="image/*"`）。
- 在输入框里直接粘贴剪切板中的图片也能添加到附件列表。
- 选中图片在输入框下方显示缩略图，可单独删除。
- 发送时把图片转成 base64 `data_url`，随消息一起 POST 到 `/api/sessions/{id}/message`。
- 已发送的消息里，图片显示在文字下方（换行）。
- 编辑重发带图消息时，编辑区展示原图缩略图：可删除任一旧图、可新上传追加；确认后按列表保存（保留的旧图以文件名引用，无需重传字节）。编辑对话框仅在当前模型支持视觉时提供上传入口。
- 点击消息中的图片会弹出深色遮罩层，居中显示大图；点击遮罩背景或按 `Esc` 关闭，右上角有 ✕ 按钮。
- 仅主 session（非 subagent）显示图片按钮；运行中禁用。

## 后端行为

- `/api/sessions/{id}/message` 接收 `message` 和可选的 `images` 数组。
- 图片写入 `sessions/<id>/images/<hash>.ext`，`hash` 是文件内容 SHA256 的前 12 位十六进制字符。
- `UserMessage` 保存 `Image` 元数据，历史文件只引用文件名，不内嵌 base64。
- `History.get_api_messages(image_dir=..., enable_vl=...)` 在构造 LLM 请求时把图片编码为 OpenAI 兼容的 `image_url` content part。
- 当 `enable_vl` 被关闭后（配置里该模型未开启，或会话切换到 `enable_vl: false` 的模型），新的图片附件不会被保存，已存在历史中的图片元数据和文件也**不会被删除**，但它们不会出现在发给 LLM 的请求里，从而可以在非视觉模型上继续对话。
- `read` 工具读到支持的图片文件（PNG / JPEG / GIF / WebP）时，会把图片存入 `sessions/<id>/images/<hash>.ext`，并返回 `<image filename="..." original_name="..." mime="..." modified="..." />` 标记。该图片会被挂到触发这次工具调用的 user 消息上，下轮 LLM 请求时模型就能看到这张图。
- 只有会话当前模型的 `enable_vl: true` 时，系统提示词和 `read` 工具定义才会包含图片相关的说明（切换模型的瞬间，agent 会重建系统提示词并更新历史首条 system 消息）。`enable_vl: false` 时 `read` 只 advertised 为文本读取工具，避免纯文本模型幻觉自己能读图。
- 图片通过 `/api/sessions/{id}/images/{filename}` 读取。
- 编辑重发（`POST /revert`）携带图片载荷：`keep_images` 列出编辑后保留的旧附件文件名（未列出的即被删除），`images` 为新上传的附件（与 `/message` 同形状）。保存同样走内容寻址，同一张图重复上传不占额外空间。

## 相关代码

| 文件 | 说明 |
|------|------|
| `src/trilobite/config.py` | `enable_vl` 默认配置 |
| `src/trilobite/messages.py` | `Image` 类、`UserMessage` 图文支持 |
| `src/trilobite/history.py` | `get_api_messages` 带 `image_dir` 参数 |
| `src/trilobite/agent.py` | `start()`/`revert()` 接收图片、`attach_subscriber` 透传 `enable_vl` |
| `src/trilobite/server.py` | 图片上传、图片读取 endpoint |
| `frontend/src/components/ChatInput.vue` | 图片按钮与上传 |
| `frontend/src/components/UserMessage.vue` | 图片渲染与编辑增减 |
| `frontend/src/store.ts` | 图片元数据透传；`enableVl` 由会话模型 × 模型列表派生 |
