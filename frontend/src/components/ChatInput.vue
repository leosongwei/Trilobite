<template>
  <div class="input-area">
    <button
      v-if="!state.isSubagent && !state.isScheduled"
      class="mode-toggle"
      :class="{ plan: state.planMode }"
      @click="toggleMode"
      :title="state.planMode ? 'Switch to Build mode' : 'Switch to Plan mode'"
    >
      {{ state.planMode ? 'Plan\u00A0' : 'Build' }}
    </button>
    <template v-if="state.isScheduled">
      <!-- Scheduled agents are unattended: no input box, interrupt only. -->
      <div class="sealed-notice">Scheduled agent — runs on its cron schedule (view-only).</div>
      <button @click="stop" :disabled="!state.isStreaming" title="Stop"><span class="ms ms-stop ms-fill"></span></button>
    </template>
    <div v-else-if="state.isSubagent && state.sealed" class="sealed-notice">
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
          @paste="onPaste"
        ></textarea>
        <div v-if="pendingImages.length" class="image-previews">
          <div v-for="(img, idx) in pendingImages" :key="idx" class="image-preview">
            <img :src="img.preview_url" :title="img.original_name" />
            <button class="image-remove" @click="removeImage(idx)" title="Remove"><span class="ms ms-close"></span></button>
          </div>
        </div>
      </div>
      <input
        ref="imageInput"
        type="file"
        accept="image/*"
        multiple
        style="display: none"
        @change="onImageSelect"
      />
      <button
        v-if="state.enableVl && !state.isSubagent"
        @click="imageInput?.click()"
        title="Add image"
        :disabled="state.isStreaming"
      >
        <span class="ms ms-attach-file"></span>
      </button>
      <button @click="handleSend">Send</button>
      <button @click="stop" :disabled="!state.isStreaming" title="Stop"><span class="ms ms-stop ms-fill"></span></button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useStore } from '../store'
import type { ImageAttachment } from '../api'

const { state, sendMessage, stopAgent, interruptSubagent, setMode } = useStore()
const message = ref('')
const textareaRef = ref<HTMLTextAreaElement>()
const imageInput = ref<HTMLInputElement>()

interface PendingImage {
  file: File
  mime_type: string
  original_name: string
  preview_url: string
  data_url: string
}

const pendingImages = ref<PendingImage[]>([])

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

function readFileAsDataURL(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

async function addImageFile(file: File) {
  const data_url = await readFileAsDataURL(file)
  pendingImages.value.push({
    file,
    mime_type: file.type,
    original_name: file.name,
    preview_url: URL.createObjectURL(file),
    data_url,
  })
}

async function onImageSelect(event: Event) {
  const target = event.target as HTMLInputElement
  const files = Array.from(target.files || [])
  for (const file of files) {
    await addImageFile(file)
  }
  target.value = ''
}

async function onPaste(event: ClipboardEvent) {
  if (!state.enableVl || state.isStreaming || state.isSubagent) return
  const items = event.clipboardData?.items
  if (!items) return
  let hasImage = false
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      hasImage = true
      const file = item.getAsFile()
      if (file) await addImageFile(file)
    }
  }
  if (hasImage) {
    event.preventDefault()
  }
}

function removeImage(idx: number) {
  const img = pendingImages.value[idx]
  if (img) URL.revokeObjectURL(img.preview_url)
  pendingImages.value.splice(idx, 1)
}

async function handleSend() {
  const msg = message.value.trim()
  if (!msg && !pendingImages.value.length) return
  const attachments: ImageAttachment[] = pendingImages.value.map((img) => ({
    mime_type: img.mime_type,
    data_url: img.data_url,
    original_name: img.original_name,
  }))
  const previewUrls = pendingImages.value.map((img) => img.preview_url)
  message.value = ''
  pendingImages.value = []
  previewUrls.forEach((url) => URL.revokeObjectURL(url))
  await nextTick()
  autoResize()
  await sendMessage(msg, attachments)
}

async function stop() {
  if (!state.currentSession) return
  // A subagent's / scheduled agent's stop is an interrupt: hard-stop its
  // current work, then it runs one summary turn and exits. The main agent's
  // stop is a plain cancel.
  if (state.isSubagent || state.isScheduled) {
    await interruptSubagent(state.currentSession)
  } else {
    await stopAgent()
  }
}

async function toggleMode() {
  await setMode(state.planMode ? 'build' : 'plan')
}
</script>

<style scoped>
.image-previews {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}
.image-preview {
  position: relative;
  width: 64px;
  height: 64px;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #3c3c3c;
}
.image-preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.image-remove {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 18px;
  height: 18px;
  line-height: 16px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.7);
  color: #fff;
  cursor: pointer;
  font-size: 12px;
}
</style>
