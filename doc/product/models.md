# 多模型配置与会话切换

## 概述

Trilobite 支持在 `config.yaml` 中**预定义多个模型**，每个模型是一份独立的 API 配置（模型名、地址、密钥、视觉能力、上下文/输出上限、压缩阈值、请求体附加字段）。会话可以随时切换主模型，切换从**下一次发送**开始生效（当前正在生成的 run 不受影响）。

## 配置

`config.yaml` 新增三个字段：

```yaml
models:
  - name: "DeepSeek"                    # 用户在界面上看到的模型名字
    model: "deepseek-chat"              # 模型在 API 中的名字
    api_key: "sk-xxx"                   # 密钥；缺省回退到顶层 api_key
    api_url: "https://api.deepseek.com/v1"  # 地址；缺省回退到顶层 api_url
    enable_vl: false                    # 视觉模型开关，默认 false
    max_context: 400000                 # 上下文窗口，默认 400k
    max_tokens: 65536                   # 单次输出上限，默认 64k
    compaction_trigger_ratio: 0.7       # 压缩阈值，默认 0.7
    extra_body:                         # 原样合并进请求体的附加字段，默认无
      reasoning_effort: max
default_model: "DeepSeek"               # 默认模型（models 中的 name）
default_vl_model: ""                    # 预留字段，暂未使用
```

要点：

- 字段缺省规则：`enable_vl=false`、`max_context=400k`、`max_tokens=64k`、`compaction_trigger_ratio=0.7`；`api_key`/`api_url` 缺省时回退到顶层同名字段。
- `models` 列表为空/不存在时回退到**旧式顶层配置**（`model`/`api_key`/`api_url`/`max_context_tokens`/`max_tokens`/`compaction_trigger_ratio`/`enable_vl`），合成单个模型（name = 顶层 `model` 值），老配置无需改动即可继续使用。旧式顶层的 `reasoning_effort` 在该回退路径下会转成模型的 `extra_body`，保持原有的思考模式行为（注意：旧的流式请求还会额外注入 `thinking: {"type": "enabled"}`，新实现不再自动注入，如需完整保持原行为，在 `extra_body` 里显式加上该字段）。
- `default_model` 未配置或指向未知模型时，取 `models` 列表第一项。
- `extra_body` 用于思考模式等厂商特有字段（如 `{"reasoning_effort": "max"}`），agent 将其**原样合并**进 chat completions 请求体，不再内置注入任何思考字段。

## 会话模型选择

- 每个主 session 的 `session.json` 持久化 `model` 字段（当前选择的模型 name）。新建 session 时写入 `default_model`；重启/重连后按该字段恢复。
- subagent 派生时**继承父 session 的模型**；定时（scheduled）agent 固定使用默认模型。
- CLI 模式：`-t` 新建 session 使用默认模型，`-c` 续接时恢复 session 已保存的模型。

### UI

主 session 的侧边栏信息区（Session / cwd / project 一行）显示当前 `model:` 与一个 **Model Conf** 按钮。点击弹出模型配置弹窗：

- 列出 `models` 中所有模型（显示 name、API 模型名、api_url、VLM 徽章、上下文/输出上限/压缩阈值），当前模型带 ✓ 标记。
- 选中后点 Apply：调用 `PUT /api/sessions/{name}/model` 持久化并立即更新 agent 的运行参数。
- 当前生成结束后，下一次点击 send 时信息发给新模型。运行中切换也不影响正在进行的生成；agent 在下一次 LLM 请求时读取新的模型参数。

### 后端行为

- `GET /api/models` 返回模型定义列表（前端形状，**不含 api_key / extra_body**）。
- `PUT /api/sessions/{name}/model` 校验模型名并持久化到 `session.json`；unknown 模型返回 400。
- `GET /api/sessions` 与 `/api/sessions/{name}/info` 携带会话当前模型名。
- 切换模型时 agent 同步更新：API 地址（httpx 客户端 base_url）、密钥、`enable_vl`、`max_context`/`max_tokens`/`compaction_trigger_ratio`、`extra_body`。
- 切换导致 `enable_vl` 变化时，agent 重建系统提示词（VLM 说明块）并更新历史首条 system 消息——切换模型本身已使 provider 缓存失效，改写 API 前缀无额外代价。
- 图片附件在 `/api/sessions/{name}/message` 端按**当前模型的 `enable_vl`** 决定是否保存（`enable_vl: false` 时丢弃本次附件，历史中已有的图片保留）。

## 相关代码

| 文件 | 说明 |
|------|------|
| `src/trilobite/config.py` | `Model` 定义、`load_models`/`get_model`/`get_default_model_name` |
| `src/trilobite/agent.py` | `apply_model`、`_build_system_prompt`、模型参数生效与请求体构造 |
| `src/trilobite/server.py` | `/api/models`、`PUT /api/sessions/{name}/model`、session 信息携带模型名 |
| `frontend/src/components/SessionSidebar.vue` | 侧边栏 model 行 + Model Conf 弹窗 |
| `frontend/src/store.ts` / `api.ts` | 模型列表加载、`selectModel` 动作 |