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
      </template>
      <div v-if="state.statusText" class="status-banner">{{ state.statusText }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useStore } from '../store'
import TurnBlock from './TurnBlock.vue'
import UserMessage from './UserMessage.vue'
import { typesetMath } from '../utils/mathjax'

const { state } = useStore()
const chatRef = ref<HTMLElement>()

// ── 自适应加载（窗口化）──────────────────────────────────────────────────────
// 切到长 session 时一次性渲染全部历史会让浏览器卡很久，因此只渲染靠近底部的
// 一个"窗口"：windowStart 指向窗口最早的条目，窗口始终包含末尾（流式输出
// 可见）。初始只渲染底部 INITIAL_VISIBLE 条，fillViewport 增量向上扩窗直到
// 填满视口；用户滚到顶部时再向上扩窗。窗口有 MAX_VISIBLE 上限，超出时从顶
// 部卸载条目（只卸已滚出视口上方的），防止长会话滚动浏览后 DOM 无限膨胀。
const INITIAL_VISIBLE = 2 // 初始从底部渲染的条数（fillViewport 会快速补齐到填满视口）
const FILL_STEP = 2       // fillViewport 每次向上扩窗的条数
const LOAD_MORE = 10      // 滚到顶部时向上扩窗的条数
const MAX_VISIBLE = 20    // 窗口上限：超过后从顶部卸载。取保守值——单条泡泡
                          // 高度不可控（超长 thinking / 大量工具条目），无法按
                          // 视口大小推算，固定条数上限才保证 DOM 有界。
const TOP_THRESHOLD = 64  // 距顶部多少 px 内触发加载更多
const TRIM_SAFE_MARGIN = 48 // 卸载单条的安全余量（覆盖 margin 等测量误差），
                            // 保证卸载后不会拽动当前视口内容

// 窗口最早可见条目的下标；窗口 = chatItems[windowStart .. 末尾]。
const windowStart = ref(0)
const windowSize = computed(() => state.chatItems.length - windowStart.value)
// 正在向上扩窗时挂起滚动处理，避免重入。
let loadingMore = false
// 正在卸载顶部条目（等待 DOM 更新后补偿滚动位置）时挂起重入。
let trimming = false

const visibleItems = computed(() => state.chatItems.slice(windowStart.value))
const hasMoreAbove = computed(() => windowStart.value > 0)
// "活的" thinking 泡泡：当前位于对话最底部、且其下方还没有任何正文/工具/
// 后续内容的那个 thinking。泡泡默认展开全文（见 ThinkingBlock）；live 状态
// 用于两处：手动折叠时 tail-follow 对齐最新几行，以及下方 maybeScrollThinking
// 在限额内流式增长时滚动钉底。
const liveIdx = computed(() => {
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
}

function scrollToBottom() {
  if (chatRef.value) {
    chatRef.value.scrollTop = chatRef.value.scrollHeight
  }
}

// 内容比视口还短却仍有更早的消息时，自动扩窗直到填满视口，避免出现"看得到
// 空白却无法滚动加载"的死区。扩窗发生在窗口顶部，底部锚点不动；每步扩窗后
// 重新钉底并检查是否已溢出。
async function fillViewport() {
  for (let i = 0; i < 50; i++) {
    const el = chatRef.value
    if (!el) return
    if (windowStart.value === 0) return // 已全部加载
    scrollToBottom()
    if (el.scrollHeight > el.clientHeight) return // 已溢出，视口填满
    windowStart.value = Math.max(0, windowStart.value - FILL_STEP)
    await nextTick()
  }
}

function onScroll() {
  if (loadingMore) return
  const el = chatRef.value
  if (!el) return
  if (el.scrollTop <= TOP_THRESHOLD && hasMoreAbove.value) {
    loadMore()
  } else {
    // 向下滚动（或已离开顶部）时顺带卸载超出窗口上限的顶部条目。
    trimExcess()
  }
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
    // 若扩出来的内容仍不足以让视口溢出（极短消息），继续补齐。
    void fillViewport()
    // 窗口可能因此超过上限，尝试卸载顶部条目。
    trimExcess()
  })
}

// 窗口超过 MAX_VISIBLE 时从顶部卸载条目，防止 DOM 无限膨胀。只卸载已完全
// 滚出视口上方的条目：按被卸条目的测量高度（含安全余量）限制卸载量，卸载后
// 用 scrollHeight 差值精确补偿 scrollTop，当前视口内容不跳动。卸载分批进行
// （每批最多 LOAD_MORE 条），用户继续下滚时逐批卸完。
function trimExcess() {
  if (loadingMore || trimming) return
  const el = chatRef.value
  if (!el) return
  const size = windowSize.value
  if (size <= MAX_VISIBLE) return
  const maxTrim = Math.min(size - MAX_VISIBLE, LOAD_MORE)
  const nodes = el.querySelectorAll<HTMLElement>('[data-chat-item]')
  const prevTop = el.scrollTop
  let removed = 0
  let count = 0
  for (let i = 0; i < maxTrim && i < nodes.length; i++) {
    const h = nodes[i].offsetHeight + TRIM_SAFE_MARGIN
    if (removed + h > prevTop) break // 再卸会拽动视口内容，等用户继续下滚
    removed += h
    count++
  }
  if (count === 0) return
  trimming = true
  const prevHeight = el.scrollHeight
  windowStart.value += count
  nextTick(() => {
    trimming = false
    if (el) el.scrollTop = el.scrollHeight - prevHeight + prevTop
  })
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

// init 整体替换 chatItems 后重置窗口并对齐到底部补齐视口（revert 重跑、切
// session、重连都走这条路径）；顶层 item（turn / user / compact / error）
// 追加时也滚到底。泡泡内部流式更新（streamTick）不再强制钉底，方便用户往
// 上翻看历史。
watch(() => state.chatItems, () => {
  resetWindow()
  nextTick(() => { scrollToBottom(); void fillViewport() })
}, { deep: false })
watch(() => state.chatItems.length, () => nextTick(scrollToBottom))

// 流式追加或向上扩窗都可能让窗口超过上限，随时检查并卸载顶部条目。
watch(windowSize, () => {
  if (windowSize.value > MAX_VISIBLE) nextTick(trimExcess)
})

// turn 内部的"新泡泡"首次出现（thinking 从空变非空、正文从空变非空、新增
// 工具调用）时滚到底部；而这些泡泡之后的流式内容增长不改变计数，不滚动，
// 不打扰用户来回翻看。
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
watch(bubbleCount, () => nextTick(scrollToBottom))

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
// 仍不足一屏（新会话的短消息），顺带补齐视口拉进更多历史。
watch(
  () => state.isStreaming,
  (v, prev) => {
    if (prev && !v) nextTick(() => { scrollToBottom(); void fillViewport() })
  },
)
</script>
