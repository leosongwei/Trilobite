我们来造一个简单的coding agent。


# 架构

python后端 - web前端

# 技术栈

## 后端

* python3.12
* uv + pyproject.toml
* openai sdk
* fastapi
* 流式输出
* Agent自身要抽象成单个的对象，好实现多实例

## 前端

* 不要用webpack，用import maps `<script type="importmap">`
* 先作为fastapi的静态文件提供出来

# 行为

## 配置

* 允许配置模型的名字，API KEY，API URL
* 用yaml格式
* 路径：
  * 预备一个 `/config_example` 文件夹。
  * 启动时在项目根目录里面创建一个 `/config`文件夹。

## 交互流程

* 用户启动端
* 用户访问网页端，创建session，指定工作目录（session的working directory从这里来）
* 创建session时，把session的信息序列化到session文件夹`/config/sessions/<session_name>/history.json`
* 每一次用户请求或者API返回都把对话历史全量序列化到session文件夹
* 超过上下文时，自动compact历史

## 主循环

1. 用户输入
2. 将系统提示词，agents.md，用户首次输入发送到API
3. 如果返回结果中有工具调用，则执行工具调用然后再次请求（工具调用、思考流程参见： https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode ）
4. 检查用户有无新请求，如果有也一并加入请求（steering）
5. 循环3，直到返回的结果中没有工具调用
6. 等待进一步输入

## 工具

* 文件读取工具： read <filename> <limit_lines: 默认50> <start_line: 默认0> <limit_chars: 默认10k>
* 写入工具：write filename <old_str> <new_str>
  * 逻辑：如果没有old_str，或者有多于1个old_str，则报错退出
  * 记得处理空文件的情况
* bash工具：默认工作目录为模型session的工作目录，用bash执行命令
* todo_read：列出当前所有todo，文本返回，已经搞定的事情标记为DONE
```
TODO:
* 事情a - DONE
* 事情b
* 事情c
```
* todo_write：新todo，例子：`["事情a", "事情b"，"事情c"]`，返回todo_read的结果
* todo_done：标记相应的todo为done：`["事情b"，"事情c"]`，返回todo_read的结果


