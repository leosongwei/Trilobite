<template>
  <div class="thinking">
    <div ref="bodyRef" class="thinking-body" :class="{ expanded: open }">
      <span ref="contentRef" class="thinking-content">{{ content }}</span>
    </div>
    <div class="thinking-header" @click="toggle">
      <span class="thinking-toggle">{{ open ? '▾' : '▸' }}</span>
      <span class="thinking-label">thinking</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'

const props = defineProps<{ content: string }>()

const open = ref(false)
const bodyRef = ref<HTMLElement | null>(null)
const contentRef = ref<HTMLElement | null>(null)

function toggle() {
  open.value = !open.value
  const content = contentRef.value
  if (!content) return
  if (open.value) {
    content.style.transform = ''
  } else {
    nextTick(stickToBottom)
  }
}

// Collapsed: keep the latest ~3 lines visible (tail-aligned) without letting
// the user scroll. Expanded: show everything.
function stickToBottom() {
  const body = bodyRef.value
  const content = contentRef.value
  if (!body || !content) return
  const style = getComputedStyle(body)
  const vpad = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
  const overflow = content.scrollHeight - (body.clientHeight - vpad)
  content.style.transform = overflow > 0 ? `translateY(${-overflow}px)` : ''
}

watch(
  () => props.content,
  async () => {
    if (open.value) return
    await nextTick()
    stickToBottom()
  },
)
</script>
