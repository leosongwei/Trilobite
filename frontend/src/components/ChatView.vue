<template>
  <div class="chat" ref="chatRef">
    <template v-if="!state.currentSession">
      <div class="empty-state">Select or create a session</div>
    </template>
    <template v-else>
      <template v-for="(item, idx) in state.chatItems" :key="idx">
        <div v-if="item.kind === 'user'" class="message user">{{ item.content }}</div>
        <TurnBlock
          v-else-if="item.kind === 'turn'"
          :turn="item"
          :streaming="state.isStreaming && idx === state.chatItems.length - 1"
        />
        <div v-else-if="item.kind === 'error'" class="message error">{{ item.content }}</div>
      </template>
      <div v-if="state.statusText" class="status-banner">{{ state.statusText }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { useStore } from '../store'
import TurnBlock from './TurnBlock.vue'

const { state } = useStore()
const chatRef = ref<HTMLElement>()

function scrollToBottom() {
  if (chatRef.value) {
    chatRef.value.scrollTop = chatRef.value.scrollHeight
  }
}

watch(() => state.chatItems.length, () => nextTick(scrollToBottom))
watch(() => state.streamTick, () => nextTick(scrollToBottom))
</script>
