<template>
  <div class="chat" ref="chatRef" @scroll="onScroll">
    <template v-if="!state.currentSession">
      <div class="empty-state">Select or create a session</div>
    </template>
    <template v-else>
      <div v-if="hasMoreAbove" class="load-more-hint">滚动到顶部加载更早的消息…</div>
      <template v-for="(item, idx) in visibleItems" :key="state.currentSession + '-' + (windowStart + idx)">
        <UserMessage v-if="item.kind === 'user'" :item="item" />
        <TurnBlock
          v-else-if="item.kind === 'turn'"
          :turn="item"
          :streaming="state.isStreaming && idx === visibleItems.length - 1"
        />
        <div v-else-if="item.kind === 'error'" class="message error">{{ item.content }}</div>
        <div v-else-if="item.kind === 'compact'" class="compact-divider" />
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
// 一个"窗口"。窗口从底部往上数 `renderCount` 条；用户滚到顶部时再往上扩窗。
// 流式输出持续向底部追加，窗口始终包含末尾，所以最新内容永远可见。
const INITIAL_VISIBLE = 10
const LOAD_MORE = 10
const TOP_THRESHOLD = 64 // 距顶部多少 px 内触发加载更多

// 从底部算起渲染多少条。初始只渲染最后 INITIAL_VISIBLE 条。
const renderCount = ref(INITIAL_VISIBLE)
// 正在向上扩窗时挂起滚动处理，避免重入。
let loadingMore = false

const effectiveRender = computed(() => Math.min(renderCount.value, state.chatItems.length))
const windowStart = computed(() => state.chatItems.length - effectiveRender.value)
const visibleItems = computed(() => state.chatItems.slice(windowStart.value))
const hasMoreAbove = computed(() => effectiveRender.value < state.chatItems.length)

function resetWindow() {
  renderCount.value = INITIAL_VISIBLE
}

function scrollToBottom() {
  if (chatRef.value) {
    chatRef.value.scrollTop = chatRef.value.scrollHeight
  }
}

// 内容比视口还短却仍有更早的消息时，自动扩窗直到填满视口，避免出现"看得到
// 空白却无法滚动加载"的死区。仅在非流式时跑（流式时窗口钉在底部即可）。
async function fillViewport() {
  if (state.isStreaming) return
  for (let i = 0; i < 50; i++) {
    const el = chatRef.value
    if (!el) return
    if (effectiveRender.value >= state.chatItems.length) return
    if (el.scrollHeight > el.clientHeight) return // 已溢出，视口填满
    const before = effectiveRender.value
    renderCount.value += LOAD_MORE
    await nextTick()
    if (effectiveRender.value === before) return
  }
}

function onScroll() {
  if (loadingMore) return
  const el = chatRef.value
  if (!el) return
  if (el.scrollTop <= TOP_THRESHOLD && hasMoreAbove.value) {
    loadMore()
  }
}

function loadMore() {
  const el = chatRef.value
  if (!el) return
  loadingMore = true
  // 扩窗后新内容会插到顶部，保持用户当前视觉位置不跳。
  const prevHeight = el.scrollHeight
  const prevTop = el.scrollTop
  renderCount.value += LOAD_MORE
  nextTick(() => {
    if (el) el.scrollTop = el.scrollHeight - prevHeight + prevTop
    loadingMore = false
    // 若扩出来的内容仍不足以让视口溢出（极短消息），继续补齐。
    void fillViewport()
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
      await typesetMath(chatRef.value)
    }
  })
}

// 切 session 时重置窗口。
watch(() => state.currentSession, () => resetWindow())

// chatItems is replaced on session switch; streamTick bumps during streaming.
// 窗口扩大（loadMore / fillViewport）会引入新的 DOM 节点，也需要 typeset。
watch(() => state.chatItems, () => scheduleTypeset(), { deep: false })
watch(() => state.streamTick, () => scheduleTypeset())
watch(effectiveRender, () => scheduleTypeset())

// init 整体替换 chatItems 后对齐到底部并补齐视口；新消息追加时钉在底部。
watch(() => state.chatItems, () => nextTick(() => { scrollToBottom(); void fillViewport() }), { deep: false })
watch(() => state.chatItems.length, () => nextTick(scrollToBottom))
watch(() => state.streamTick, () => nextTick(scrollToBottom))
</script>
