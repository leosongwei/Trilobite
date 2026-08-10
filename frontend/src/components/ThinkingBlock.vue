<template>
  <div class="thinking">
    <div class="thinking-header" @click="toggle">
      <span class="thinking-toggle ms ms-expand" :class="{ open }"></span>
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

// 默认折叠：body 有 max-height 上限（overflow: hidden），但高度随内容渐进
// 增长——内容 1 行时框只有 1 行高，2 行时 2 行高，……直到封顶于 max-height
// 后不再变高（超出部分靠 transform 把尾部对齐到框底，类似 tail -f，见
// stickToBottom）。ChatView 配合在框高度每次增长（封顶前）滚动钉底，封顶后
// 不再滚动。用户手动展开后显示全文。
const open = ref(false)
const bodyRef = ref<HTMLElement | null>(null)
const contentRef = ref<HTMLElement | null>(null)

const PREVIEW_LINES = 3
const PREVIEW_MAX_CHARS = 300 // 超长单行兜底，约 3 行

// 老泡泡（下方已有正文/工具/后续内容）折叠时只把最后几行写进 DOM，避免整段
// 思考塞进 DOM 造成渲染压力；"活的"泡泡（当前位于最底部、下方还没有任何
// 正文/工具/后续内容）保留全文，配合 transform 尾部对齐实时跟随流式输出。
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
