<template>
  <div class="thinking">
    <div class="thinking-header" @click="toggle">
      <span class="thinking-toggle">{{ open ? '▾' : '▸' }}</span>
      <span class="thinking-label">thinking</span>
    </div>
    <div ref="bodyRef" class="thinking-body" :class="{ expanded: open }">
      <span ref="contentRef" class="thinking-content">{{ displayContent }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'

const props = defineProps<{ content: string; live?: boolean }>()

// 默认完整展开显示全文（thinking 时泡泡随流式输出自然增高；ChatView 只在
// 约 3 行限额内随高度增高滚动钉底，超过后不强行锁定底部）。只有用户手动
// 折叠时才进入预览模式：老泡泡只把最后几行写进 DOM，避免整段思考塞进 DOM
// 造成渲染压力；"活的"泡泡（当前位于最底部、下方还没有任何正文/工具/后续
// 内容）则保留全文并用 transform 把尾部对齐到窗口底部，实时跟随流式输出
// （类似 tail -f）。
const open = ref(true)
const bodyRef = ref<HTMLElement | null>(null)
const contentRef = ref<HTMLElement | null>(null)

const PREVIEW_LINES = 3
const PREVIEW_MAX_CHARS = 300 // 超长单行兜底，约 3 行

const displayContent = computed(() => {
  if (props.live || open.value) return props.content
  const lines = props.content.split('\n')
  let preview =
    lines.length <= PREVIEW_LINES ? props.content : lines.slice(-PREVIEW_LINES).join('\n')
  if (preview.length > PREVIEW_MAX_CHARS) {
    preview = '…' + preview.slice(-PREVIEW_MAX_CHARS)
  }
  return preview
})

function toggle() {
  open.value = !open.value
  const content = contentRef.value
  if (!content) return
  if (open.value) {
    content.style.transform = ''
  } else if (props.live) {
    nextTick(stickToBottom)
  }
}

// live 泡泡折叠时把全文往上偏移，露出最新的几行（不可手动滚动）。
function stickToBottom() {
  const body = bodyRef.value
  const content = contentRef.value
  if (!body || !content) return
  const style = getComputedStyle(body)
  const vpad = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
  const overflow = content.scrollHeight - (body.clientHeight - vpad)
  content.style.transform = overflow > 0 ? `translateY(${-overflow}px)` : ''
}

onMounted(() => {
  if (props.live && !open.value) stickToBottom()
})

// live -> old：思考阶段结束（下方出现正文/工具/后续内容）。保持当前展开
// 状态（默认展开全文），只清掉手动折叠时可能残留的 transform tail。
watch(
  () => props.live,
  (live, prev) => {
    if (prev && !live) {
      const content = contentRef.value
      if (content) content.style.transform = ''
    }
  },
)

watch(
  () => props.content,
  async () => {
    if (!props.live || open.value) return
    await nextTick()
    stickToBottom()
  },
)
</script>
