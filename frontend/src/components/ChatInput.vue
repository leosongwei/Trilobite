<template>
  <div class="input-area">
    <button
      class="mode-toggle"
      :class="{ plan: state.planMode }"
      @click="toggleMode"
      :title="state.planMode ? 'Switch to Build mode' : 'Switch to Plan mode'"
    >
      {{ state.planMode ? 'Plan' : 'Build' }}
    </button>
    <textarea
      v-model="message"
      placeholder="Type a message..."
      rows="1"
      ref="textareaRef"
      @keydown.enter.exact.prevent="handleSend"
    ></textarea>
    <button @click="handleSend">Send</button>
    <button @click="stop" :disabled="!state.isStreaming" title="Stop">&#9632;</button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { useStore } from '../store'

const { state, sendMessage, stopAgent, setMode } = useStore()
const message = ref('')
const textareaRef = ref<HTMLTextAreaElement>()

function autoResize() {
  const el = textareaRef.value
  if (el) {
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 120) + 'px'
  }
}

watch(message, () => nextTick(autoResize))

async function handleSend() {
  const msg = message.value.trim()
  if (!msg) return
  message.value = ''
  await nextTick()
  autoResize()
  await sendMessage(msg)
}

async function stop() {
  await stopAgent()
}

async function toggleMode() {
  await setMode(state.planMode ? 'build' : 'plan')
}
</script>
