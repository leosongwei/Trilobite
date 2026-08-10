<template>
  <div class="chat" ref="chatRef" @scroll="onScroll">
    <template v-if="!state.currentSession">
      <div class="empty-state">Select or create a session</div>
    </template>
    <template v-else>
      <div v-if="hasMoreAbove" class="load-more-hint">滚动到顶部加载更早的消息…</div>
      <template v-for="(item, idx) in visibleItems" :key="item">
        <UserMessage v-if="item.kind === 'user'" data-chat-item :item="item" />
        <TurnBlock
          v-else-if="item.kind === 'turn'"
          data-chat-item
          :turn="item"
          :streaming="state.isStreaming && idx === visibleItems.length - 1"
          :live="idx === liveIdx"
        />
        <div v-else-if="item.kind === 'error'" data-chat-item class="message error">{{ item.content }}</div>
        <div v-else-if="item.kind === 'compact'" data-chat-item class="compact-divider" />
        <div v-else-if="item.kind === 'divider'" data-chat-item class="run-divider">{{ item.text }}</div>
      </template>
      <div v-if="state.statusText" class="status-banner">{{ state.statusText }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useStore } from '../store'
import TurnBlock from './TurnBlock.vue'
import UserMessage from './UserMessage.vue'
import { typesetMath } from '../utils/mathjax'

const { state } = useStore()
const chatRef = ref<HTMLElement>()

// ── 自适应加载（窗口化）──────────────────────────────────────────────────────
// 切到长 session 时一次性渲染全部历史会让浏览器卡很久，因此只渲染一个
// "窗口"：windowStart / windowEnd 分别是窗口最早/最晚条目的下标（半开区间，
// 窗口 = chatItems[windowStart .. windowEnd)）。窗口通常包含末尾（流式输出
// 可见）；用户滚到顶部时向上扩窗加载旧消息，旧消息保留在窗口顶部不被卸载。
// DOM 有界性由卸载底部已滚出视口下方的条目保证（trimExcess）——用户向上翻
// 历史时旧消息一直可看，滚回底部时窗口重新包含末尾并钉底。
const INITIAL_VISIBLE = 10 // 初始从底部渲染的条数：切 session/启动只显示最后
                          // 几条，往上滚动才加载更早的；内容不足一屏时由
                          // fillViewport 补齐填满视口（避免无法滚动的死区）
const FILL_STEP = 2       // fillViewport 每次向上扩窗的条数
const LOAD_MORE = 10      // 滚到顶部时向上扩窗的条数
const MAX_FILL = 30       // fillViewport 扩窗的条数硬上限。不依赖 scrollHeight
                          // 测量——滚动容器测量一旦异常（高度不受约束/内容高度
                          // 未更新），"视口已满"判断会失效并一路扩到全部渲染，
                          // 切 session 时把整个历史一次性挂进 DOM。条数上限
                          // 保证无论测量如何，初始窗口始终有界。
const MAX_VISIBLE = 40    // 窗口上限：超过后从底部卸载已滚出视口的条目。单条
                          // 泡泡高度不可控（超长 thinking / 大量工具条目），
                          // 固定条数上限才保证 DOM 有界。
const TOP_THRESHOLD = 64    // 距顶部多少 px 内触发加载更多
const BOTTOM_THRESHOLD = 64 // 距底部多少 px 内视为"钉底"（跟随流式滚动 /
                            // 窗口重新包含末尾）
const TRIM_SAFE_MARGIN = 48 // 卸载条目的安全余量（覆盖 margin 等测量误差），
                            // 保证不卸载视口内或即将滚入视口的内容

// 窗口最早/最晚可见条目的下标；窗口 = chatItems[windowStart .. windowEnd)。
// 通常 windowEnd === chatItems.length（窗口包含末尾，流式输出可见）；用户向
// 上翻历史时底部条目可能被卸载（windowEnd < length），滚回底部后重新包含末尾。
const windowStart = ref(0)
const windowEnd = ref(0)
const windowSize = computed(() => windowEnd.value - windowStart.value)
// 正在向上扩窗时挂起滚动处理，避免重入。
let loadingMore = false

const visibleItems = computed(() =>
  state.chatItems.slice(windowStart.value, windowEnd.value),
)
const hasMoreAbove = computed(() => windowStart.value > 0)
// "活的" thinking 泡泡：当前位于对话最底部、且其下方还没有任何正文/工具/
// 后续内容的那个 thinking。泡泡默认展开全文（见 ThinkingBlock）；live 状态
// 用于两处：手动折叠时 tail-follow 对齐最新几行，以及下方 maybeScrollThinking
// 在限额内流式增长时滚动钉底。窗口不含末尾（用户正在翻历史）时不存在 live 泡泡。
const liveIdx = computed(() => {
  if (windowEnd.value < state.chatItems.length) return -1
  const items = visibleItems.value
  if (items.length === 0) return -1
  const last = items[items.length - 1]
  if (last.kind === 'turn' && last.thinking && !last.text && last.tools.length === 0) {
    return items.length - 1
  }
  return -1
})

function resetWindow() {
  windowStart.value = Math.max(0, state.chatItems.length - INITIAL_VISIBLE)
  windowEnd.value = state.chatItems.length
}

function scrollToBottom() {
  if (chatRef.value) {
    chatRef.value.scrollTop = chatRef.value.scrollHeight
  }
}

// 内容比视口还短却仍有更早的消息时，自动扩窗直到填满视口，避免出现"看得到
// 空白却无法滚动加载"的死区。扩窗受 MAX_FILL 条数硬上限约束（不依赖滚动测量，
// 见常量注释），视口已满时也提前返回——不打扰正在翻历史的用户；扩窗发生在
// 窗口顶部，只有用户原本钉底时扩窗后才重新钉底。
async function fillViewport() {
  for (let i = 0; i < 50; i++) {
    const el = chatRef.value
    if (!el) return
    if (windowStart.value === 0) return // 已全部加载
    if (windowSize.value >= MAX_FILL) return // 条数硬上限，防止测量失效时全量渲染
    if (el.scrollHeight > el.clientHeight) return // 已溢出，视口填满
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD
    windowStart.value = Math.max(0, windowStart.value - FILL_STEP)
    await nextTick()
    if (atBottom) scrollToBottom()
  }
}

function onScroll() {
  if (loadingMore) return
  const el = chatRef.value
  if (!el) return
  if (el.scrollTop <= TOP_THRESHOLD && hasMoreAbove.value) {
    loadMore()
    return
  }
  // 滚回底部附近：窗口重新包含末尾（之前向上翻历史可能卸载了底部条目），
  // 重新钉底对齐最新内容。
  if (el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD) {
    if (windowEnd.value < state.chatItems.length) {
      resetWindow()
      nextTick(() => { scrollToBottom(); void fillViewport() })
    }
    return
  }
  // 向下滚动（或已离开顶部）时顺带卸载超出窗口上限的底部条目。
  trimExcess()
}

function loadMore() {
  const el = chatRef.value
  if (!el) return
  loadingMore = true
  // 扩窗后新内容会插到顶部，保持用户当前视觉位置不跳。
  const prevHeight = el.scrollHeight
  const prevTop = el.scrollTop
  windowStart.value = Math.max(0, windowStart.value - LOAD_MORE)
  nextTick(() => {
    loadingMore = false
    if (el) el.scrollTop = el.scrollHeight - prevHeight + prevTop
    // 若扩出来的内容仍不足以让视口溢出（极短消息），继续补齐；视口已满时
    // fillViewport 直接返回，不会把正在翻历史的用户拽到底部。
    void fillViewport()
    // 窗口可能因此超过上限，尝试卸载底部条目。
    trimExcess()
  })
}

// 窗口超过 MAX_VISIBLE 时从底部卸载条目，防止 DOM 无限膨胀。只卸载已完全
// 滚出视口下方的条目——用户向上翻历史加载的旧消息位于窗口顶部，保留不卸载，
// 滚动浏览体验与正常聊天一致。卸载底部内容不影响当前视口（scrollTop 不变），
// 无需滚动补偿。流式输出期间不卸载，避免卸掉正在输出的泡泡；卸载分批进行
// （每批最多 LOAD_MORE 条），用户继续滚动时逐批卸完。
function trimExcess() {
  if (loadingMore || state.isStreaming) return
  const el = chatRef.value
  if (!el) return
  // 钉底中不卸载——用户正看着最新内容（可能是高度不足安全余量的短消息）。
  if (el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD) return
  const size = windowSize.value
  if (size <= MAX_VISIBLE) return
  const maxTrim = Math.min(size - MAX_VISIBLE, LOAD_MORE)
  const nodes = el.querySelectorAll<HTMLElement>('[data-chat-item]')
  const containerBottom = el.getBoundingClientRect().bottom
  let count = 0
  // 从后往前数：完全位于视口下方的条目才卸载。
  for (let i = nodes.length - 1; i >= 0 && count < maxTrim; i--) {
    const rect = nodes[i].getBoundingClientRect()
    if (rect.top < containerBottom - TRIM_SAFE_MARGIN) break // 该条已进入视口，停止
    count++
  }
  if (count === 0) return
  windowEnd.value -= count
}

// Typeset math in the chat container after content changes.
let typesetTimer: ReturnType<typeof setTimeout> | null = null
function scheduleTypeset() {
  if (typesetTimer) clearTimeout(typesetTimer)
  // 流式输出时 v-html 会随每个 delta 重新设置 innerHTML，把 MathJax 刚渲染
  // 好的 <mjx-container> 覆盖回原始 TeX 文本，而 MathJax 内部仍保留着已被
  // 移除节点的状态，导致后续 typeset 静默失败--公式"闪一下就消失"。
  // 因此流式期间跳过渲染，等流结束后（isStreaming 翻回 false）再统一 typeset。
  if (state.isStreaming) return
  typesetTimer = setTimeout(async () => {
    typesetTimer = null
    await nextTick()
    if (chatRef.value) {
      // 仅对 assistant 正文（.markdown-body）启用 MathJax。整个 chat 容器内还有
      // 工具调用小标题、思考块、用户消息等纯文本，它们的 `$`（如 bash 的 $HOME、
      // grep 的行尾锚点 $）会被 MathJax 误判为内联公式分隔符而错乱渲染。
      const bodies = Array.from(
        chatRef.value.querySelectorAll<HTMLElement>('.markdown-body'),
      )
      if (bodies.length) await typesetMath(bodies)
    }
  })
}

// 切 session 时重置窗口。
watch(() => state.currentSession, () => {
  resetWindow()
  lastThinkingScrollHeight = 0
})

// chatItems is replaced on session switch; streamTick bumps during streaming.
// 窗口扩大（loadMore / fillViewport）会引入新的 DOM 节点，也需要 typeset。
watch(() => state.chatItems, () => scheduleTypeset(), { deep: false })
watch(() => state.streamTick, () => scheduleTypeset())
watch(windowStart, () => scheduleTypeset())
watch(windowEnd, () => scheduleTypeset())

// init 整体替换 chatItems 后重置窗口并对齐到底部补齐视口（revert 重跑、切
// session、重连都走这条路径）；顶层 item（turn / user / compact / error）
// 追加时也滚到底。泡泡内部流式更新（streamTick）不再强制钉底，方便用户往
// 上翻看历史。
watch(() => state.chatItems, () => {
  resetWindow()
  nextTick(() => { scrollToBottom(); void fillViewport() })
}, { deep: false })
watch(() => state.chatItems.length, () => {
  // 窗口包含末尾时新追加的消息进入窗口；用户钉底时跟随滚动，翻历史时不打扰。
  if (windowEnd.value === state.chatItems.length - 1) {
    windowEnd.value = state.chatItems.length
    const el = chatRef.value
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD) {
      nextTick(scrollToBottom)
    }
  }
})

// 流式追加或向上扩窗都可能让窗口超过上限，随时检查并卸载底部条目。
watch(windowSize, () => {
  if (windowSize.value > MAX_VISIBLE) nextTick(trimExcess)
})

// turn 内部的"新泡泡"首次出现（thinking 从空变非空、正文从空变非空、新增
// 工具调用）时滚到底部；而这些泡泡之后的流式内容增长不改变计数，不滚动，
// 不打扰用户来回翻看。窗口不含末尾（翻历史中）或用户不在底部时不滚动。
const bubbleCount = computed(() => {
  let n = 0
  for (const it of state.chatItems) {
    if (it.kind === 'turn') {
      if (it.thinking) n++
      if (it.text) n++
      n += it.tools.length
    }
  }
  return n
})
watch(bubbleCount, () => {
  const el = chatRef.value
  if (!el) return
  if (windowEnd.value < state.chatItems.length) return
  if (el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD) {
    nextTick(scrollToBottom)
  }
})

// thinking 泡泡流式增长：折叠框高度随内容渐进增高（内容 1 行框 1 行高、2 行
// 框 2 行高，……封顶于 CSS max-height 后不再变高）。每次框高度增长都滚到底
// ——一两行的短泡泡始终完整可见；封顶后高度不再变化，也就不再滚动，长思考
// 不打扰用户翻看历史（框内超出部分由 ThinkingBlock 的 transform 尾部对齐，
// 最新内容始终可见）。用户手动展开（.expanded）后不自动滚动。
let lastThinkingScrollHeight = 0

// 新 thinking 泡泡出现时重置高度记录，避免与上一个泡泡残留高度比较。
watch(liveIdx, (v) => {
  if (v !== -1) lastThinkingScrollHeight = 0
})

function maybeScrollThinking() {
  const el = chatRef.value
  const items = visibleItems.value
  if (!el || items.length === 0) return
  if (windowEnd.value < state.chatItems.length) return // 窗口不含末尾（翻历史中）
  const last = items[items.length - 1]
  // 只处理底部"活的" thinking 泡泡（下方无正文/工具）。
  if (last.kind !== 'turn' || !last.thinking || last.text || last.tools.length > 0) return
  const bodies = el.querySelectorAll<HTMLElement>('.thinking-body')
  const body = bodies.length ? bodies[bodies.length - 1] : null
  if (!body || body.classList.contains('expanded')) return
  const h = body.clientHeight
  if (h === lastThinkingScrollHeight) return // 高度未变（同一行内增长或已达 max-height 封顶）
  lastThinkingScrollHeight = h
  scrollToBottom()
}

// 每个流式 delta 后检查一次（nextTick 等 DOM 更新后再测量）。
watch(() => state.streamTick, () => nextTick(maybeScrollThinking))

// 纯文字输出结束（run 完成，isStreaming 翻回 false）时滚一次底：流式期间
// 每个 delta 都不钉底（见上），只在输出全部结束后滚到底展示完整结果；若内容
// 仍不足一屏（新会话的短消息），顺带补齐视口拉进更多历史。窗口不含末尾或
// 用户不在底部（正在翻历史）时不打扰。
watch(
  () => state.isStreaming,
  (v, prev) => {
    if (prev && !v) {
      const el = chatRef.value
      if (!el) return
      if (windowEnd.value < state.chatItems.length) return
      if (el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD) {
        nextTick(() => { scrollToBottom(); void fillViewport() })
      }
    }
  },
)

// 组件可能以非空 chatItems 重新挂载（打开文件管理器时 v-if 卸载对话区，
// 关闭后重建 ChatView），此时 chatItems 不再变化、上面的 watch 不会触发，
// 窗口停留在 0,0 导致对话空白——挂载时初始化窗口并对齐底部。
onMounted(() => {
  resetWindow()
  nextTick(() => { scrollToBottom(); void fillViewport() })
})
</script>
