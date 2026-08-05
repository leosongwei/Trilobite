---
name: vue-component-debug
description: Debug Vue components (windowing/scroll/interaction/timing logic) by mounting the real component in vitest + jsdom with mocked store and layout.
---

# Vue Component Debug (vitest + jsdom)

## When to use

调试 `frontend/src` 下 Vue 组件的交互 / 滚动 / 窗口化 / 时序逻辑，纯代码推理
无法确定根因时。典型场景：`ChatView.vue` 的自适应加载（窗口化）——依赖
`scrollHeight` / `clientHeight` / `getBoundingClientRect` 测量的滚动钉底、
`loadMore` 扩窗、`trimExcess` 卸载等。做法是加载**真实组件**，mock store 与
DOM 布局测量，模拟 SSE 事件流，断言 DOM 状态。

## Prerequisites

调试依赖已加入 `frontend/package.json` 的 devDependencies：`vitest`、
`jsdom`、`@vue/test-utils`（如缺失：`cd frontend && npm i -D vitest jsdom @vue/test-utils`）。
下文路径均相对仓库根。

## Procedure（临时文件，不进 git）

1. 写临时配置 `frontend/vitest.config.ts`（模板见下）。
2. 写临时测试 `frontend/debug-<name>.test.ts`（模板见下）。
3. 运行：`cd frontend && npx vitest run debug-<name>.test.ts`
   （`vitest.config.ts` 会被自动发现；测试文件放在 frontend 根目录即可，
   `tsconfig.json` 只 include `src/**`，不会干扰 `vue-tsc` 类型检查）。
4. 调试完成**删除两个临时文件**，`git status` 确认无残留。

## vitest.config.ts 模板

```ts
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': new URL('./src', import.meta.url).pathname },
  },
  test: { environment: 'jsdom' },
})
```

## 测试文件模板

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'

// ── mock store：模块级 reactive state，组件与测试共享 ──
// 必须用 vi.hoisted（vi.mock 会被提升，不能引用外部变量）。
const state = vi.hoisted(() => {
  const { reactive } = require('vue')
  return reactive({
    sessions: [],
    currentSession: null,
    chatItems: [],
    isStreaming: false,
    tokenCount: 0,
    maxTokens: 0,
    statusText: null,
    streamTick: 0,
    fsRefreshTick: 0,
    planMode: false,
    additionalDirs: [],
    pendingRequests: [],
    isSubagent: false,
    sealed: false,
    subagentType: null,
    subagentDescription: '',
    enableVl: false,
  })
})
vi.mock('@/store', () => ({ useStore: () => ({ state }) }))
vi.mock('@/utils/mathjax', () => ({ typesetMath: vi.fn(async () => {}) }))

import ChatView from '@/components/ChatView.vue'

// ── 布局测量 mock：jsdom 不执行真实布局，必须手动模拟 ──
// itemH：每条 [data-chat-item] 的高度；viewport：.chat 容器视口高度；
// broken：模拟"测量失效"（scrollHeight 恒等于 clientHeight，永不溢出）。
function setupLayout({ itemH = 40, viewport = 800, broken = false } = {}) {
  const proto = window.HTMLElement.prototype
  Object.defineProperty(proto, 'clientHeight', {
    configurable: true,
    get() {
      if (this.classList?.contains('chat')) return viewport
      return 0
    },
  })
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true,
    get() {
      if (this.classList?.contains('chat')) {
        if (broken) return viewport
        return this.querySelectorAll('[data-chat-item]').length * itemH
      }
      return 0
    },
  })
  Object.defineProperty(proto, 'offsetHeight', {
    configurable: true,
    get() { return this.matches?.('[data-chat-item]') ? itemH : 0 },
  })
  Object.defineProperty(proto, 'getBoundingClientRect', {
    configurable: true,
    value() {
      if (this.classList?.contains('chat')) {
        return { top: 0, bottom: viewport, left: 0, right: 100, width: 100, height: viewport, x: 0, y: 0, toJSON() {} }
      }
      const container = this.closest?.('.chat')
      const nodes = container ? Array.from(container.querySelectorAll('[data-chat-item]')) : []
      const idx = nodes.indexOf(this)
      const top = idx * itemH
      return { top, bottom: top + itemH, left: 0, right: 100, width: 100, height: itemH, x: 0, y: top, toJSON() {} }
    },
  })
}

// ── 模拟前端事件流 ──
function selectSession(id: string) {
  state.currentSession = id
  state.chatItems = [] // store.selectSession 里先清空
}
function sseInit(nTurns: number) {
  // 构造 ChatItem[]（与 store.parseHistory 的输出同构；user/turn 交错）
  const items: any[] = []
  for (let i = 0; i < nTurns; i++) {
    items.push({ kind: 'user', content: `user ${i}`, images: [], userSeq: i })
    items.push({ kind: 'turn', thinking: '', text: `reply ${i}`, tools: [] })
  }
  state.chatItems = items
}
function itemCount(wrapper: any) {
  return wrapper.findAll('[data-chat-item]').length
}
// fillViewport 是 async 循环（多轮 nextTick），等它跑完再断言
async function settle() {
  await new Promise((r) => setTimeout(r, 50))
}

beforeEach(() => {
  state.currentSession = null
  state.chatItems = []
  state.isStreaming = false
  state.streamTick = 0
})

describe('ChatView windowing', () => {
  it('healthy layout: bottom window + pinned to bottom', async () => {
    setupLayout({ itemH: 40, viewport: 800 })
    const wrapper = mount(ChatView)
    selectSession('s1')
    sseInit(200)
    await settle()
    expect(itemCount(wrapper)).toBeLessThan(100) // 窗口化，不全量渲染
    const chat = wrapper.find('.chat').element as HTMLElement
    expect(chat.scrollTop).toBeGreaterThan(0) // 钉底
    wrapper.unmount()
  })

  it('broken measurement: window stays bounded', async () => {
    setupLayout({ itemH: 40, viewport: 800, broken: true })
    const wrapper = mount(ChatView)
    selectSession('s1')
    sseInit(200)
    await settle()
    expect(itemCount(wrapper)).toBeLessThan(100)
    wrapper.unmount()
  })

  it('scroll to top loads older messages without jumping to bottom', async () => {
    setupLayout({ itemH: 40, viewport: 800 })
    const wrapper = mount(ChatView)
    selectSession('s1')
    sseInit(50)
    await settle()
    const chat = wrapper.find('.chat').element as HTMLElement
    chat.scrollTop = 0
    chat.dispatchEvent(new window.Event('scroll'))
    await settle()
    const bottom = chat.scrollHeight - chat.clientHeight
    expect(chat.scrollTop).toBeLessThan(bottom) // 未跳底（跳底会等于 bottom）
    wrapper.unmount()
  })
})
```

## 调试要点

- **jsdom 不执行真实布局**：`scrollHeight` / `clientHeight` / `offsetHeight` /
  `getBoundingClientRect` 默认返回 0 / 空矩形，必须按上表 mock。`broken` 模式
  模拟"测量失效"（`scrollHeight` 恒等于 `clientHeight`）——`ChatView` 的
  `fillViewport` 曾因此把整个历史一次性渲染出来（issue #60 的根因）。
- **store 是模块级 reactive 对象**：用 `vi.hoisted` 创建并 `vi.mock('@/store')`，
  否则 `vi.mock` 提升后无法引用外部变量。
- **Vue watch 默认 `flush: 'pre'`**：同一 tick 内多次 state 变更会合并到渲染前
  一次性触发；`fillViewport` 这类 async 循环（每步 `await nextTick`）需要
  `settle()`（等 50ms）后再断言。
- **触发滚动**：直接设 `el.scrollTop` 并 `dispatchEvent(new window.Event('scroll'))`。
- **其它组件依赖**：按需 `vi.mock`（如 `@/utils/mathjax` 的 `typesetMath`）；
  子组件（TurnBlock/UserMessage 等）会被真实加载，若它们依赖 markdown/marked
  等真实依赖则无需 mock。
- **npm audit 提示**：vitest/jsdom 带来的 audit 告警可忽略，devDependencies
  不进运行时。
