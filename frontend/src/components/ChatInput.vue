<template>
  <div class="input-area">
    <button
      v-if="!state.isSubagent"
      class="mode-toggle"
      :class="{ plan: state.planMode }"
      @click="toggleMode"
      :title="state.planMode ? 'Switch to Build mode' : 'Switch to Plan mode'"
    >
      {{ state.planMode ? 'Plan\u00A0' : 'Build' }}
    </button>
    <div v-if="state.isSubagent && state.sealed" class="sealed-notice">
      This subagent has ended (view-only).
    </div>
    <template v-else>
      <div class="input-wrap">
        <div v-if="showCommands" class="command-menu">
          <div
            v-for="c in filteredCommands"
            :key="c.cmd"
            class="command-item"
            @mousedown.prevent="pickCommand(c.cmd)"
          >
            <span class="command-cmd">{{ c.cmd }}</span>
            <span class="command-desc">{{ c.desc }}</span>
          </div>
        </div>
        <textarea
          v-model="message"
          :placeholder="state.isSubagent ? 'Steer the subagent...' : 'Type a message... (type / for commands)'"
          rows="1"
          ref="textareaRef"
          @keydown.enter.exact.prevent="handleSend"
          @keydown.tab.prevent="tabComplete"
        ></textarea>
      </div>
      <button @click="handleSend">Send</button>
      <button @click="stop" :disabled="!state.isStreaming" title="Stop">&#9632;</button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useStore } from '../store'

const { state, sendMessage, stopAgent, interruptSubagent, setMode } = useStore()
const message = ref('')
const textareaRef = ref<HTMLTextAreaElement>()

// Slash commands are purely a client-side autocomplete hint; the text is sent
// as-is and matched by the backend (see server.send_message).
const COMMANDS = [
  { cmd: '/compact', desc: '压缩上下文（手动触发 compaction）' },
]

const filteredCommands = computed(() => {
  const m = message.value.trim()
  if (!m.startsWith('/')) return []
  return COMMANDS.filter((c) => c.cmd.startsWith(m) && c.cmd !== m)
})

const showCommands = computed(() => filteredCommands.value.length > 0)

function pickCommand(cmd: string) {
  message.value = cmd + ' '
  nextTick(() => textareaRef.value?.focus())
}

function tabComplete() {
  if (filteredCommands.value.length > 0) {
    pickCommand(filteredCommands.value[0].cmd)
  }
}

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
  if (!state.currentSession) return
  // A subagent's stop is an interrupt: hard-stop its current work, then it
  // runs one summary turn and exits. The main agent's stop is a plain cancel.
  if (state.isSubagent) {
    await interruptSubagent(state.currentSession)
  } else {
    await stopAgent()
  }
}

async function toggleMode() {
  await setMode(state.planMode ? 'build' : 'plan')
}
</script>
