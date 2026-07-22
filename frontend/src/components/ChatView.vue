<template>
  <div class="chat" ref="chatRef">
    <template v-if="!state.currentSession">
      <div class="empty-state">Select or create a session</div>
    </template>
    <template v-else>
      <template v-for="(item, idx) in state.chatItems" :key="state.currentSession + '-' + idx">
        <UserMessage v-if="item.kind === 'user'" :item="item" />
        <TurnBlock
          v-else-if="item.kind === 'turn'"
          :turn="item"
          :streaming="state.isStreaming && idx === state.chatItems.length - 1"
        />
        <div v-else-if="item.kind === 'error'" class="message error">{{ item.content }}</div>
        <div v-else-if="item.kind === 'compact'" class="compact-divider" />
      </template>
      <div v-if="state.statusText" class="status-banner">{{ state.statusText }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { useStore } from '../store'
import TurnBlock from './TurnBlock.vue'
import UserMessage from './UserMessage.vue'
import { typesetMath } from '../utils/mathjax'

const { state } = useStore()
const chatRef = ref<HTMLElement>()

function scrollToBottom() {
  if (chatRef.value) {
    chatRef.value.scrollTop = chatRef.value.scrollHeight
  }
}

// Typeset math in the chat container after content changes.
let typesetTimer: ReturnType<typeof setTimeout> | null = null
function scheduleTypeset() {
  if (typesetTimer) clearTimeout(typesetTimer)
  // 流式输出时 v-html 会随每个 delta 重新设置 innerHTML，把 MathJax 刚渲染
  // 好的 <mjx-container> 覆盖回原始 TeX 文本，而 MathJax 内部仍保留着已被
  // 移除节点的状态，导致后续 typeset 静默失败——公式"闪一下就消失"。
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

// chatItems is replaced on session switch; streamTick bumps during streaming
watch(() => state.chatItems, () => scheduleTypeset(), { deep: false })
watch(() => state.streamTick, () => scheduleTypeset())

watch(() => state.chatItems.length, () => nextTick(scrollToBottom))
watch(() => state.streamTick, () => nextTick(scrollToBottom))
</script>
