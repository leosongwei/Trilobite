<template>
  <div class="tool-entry">
    <template v-if="isTask">
      <div class="tool-action">
        [task: {{ taskCount }} subagent{{ taskCount === 1 ? '' : 's' }}]<span v-if="tool.status === 'running'"> running...</span>
      </div>
      <div class="subagent-tree">
        <div
          v-for="c in subagents"
          :key="c.session"
          class="subagent-node"
          @click="openChild(c.session)"
          :title="`Open subagent session: ${c.description || c.session}`"
        >
          <span class="subagent-type" :class="c.type">{{ c.type }}</span>
          <span class="subagent-desc">{{ c.description }}</span>
          <span class="subagent-state" :class="c.state">{{ stateLabel(c.state) }}</span>
        </div>
      </div>
    </template>
    <div v-else-if="isRead">
      <div class="tool-action toggle-header" @click="readOpen = !readOpen">
        <span class="ms ms-expand" :class="{ open: readOpen }"></span>
        [{{ label }}]<span v-if="tool.status === 'running'"> running...</span>
      </div>
      <pre v-if="readOpen" class="tool-result">{{ displayContent }}</pre>
    </div>
    <template v-else-if="isBash">
      <div class="tool-action bash-action toggle-header" @click="toggleCollapsed">
        <span v-if="collapsible" class="ms ms-expand" :class="{ open: expanded }"></span>
        [<span v-if="bashDescription" class="ms ms-build ms-fill"></span>bash: {{ bashLabel }}]<span v-if="tool.status === 'running'"> running...</span>
      </div>
      <pre ref="outputPre" class="tool-result">{{ collapsedContent }}</pre>
    </template>
    <template v-else-if="isSearch">
      <div class="tool-action toggle-header" @click="toggleCollapsed">
        <span v-if="collapsible" class="ms ms-expand" :class="{ open: expanded }"></span>
        [{{ label }}]<span v-if="tool.status === 'running'"> running...</span>
      </div>
      <pre ref="outputPre" class="tool-result">{{ collapsedContent }}</pre>
    </template>
    <template v-else-if="isSleep">
      <div class="tool-action">
        [{{ label }}]<span v-if="tool.status === 'running'"> sleeping...</span>
      </div>
      <pre v-if="tool.status !== 'running'" class="tool-result">{{ displayContent }}</pre>
    </template>
    <template v-else>
      <div class="tool-action">
        [{{ label }}]<span v-if="tool.status === 'running'"> running...</span>
      </div>
      <DiffView v-if="tool.diff" :rows="tool.diff" />
      <pre v-else-if="tool.diffPrev" class="tool-result">{{ tool.diffCurrent ?? tool.diffPrev }}</pre>
      <pre ref="outputPre" v-else class="tool-result">{{ displayContent }}</pre>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { ToolDisplay } from '../types'
import { useStore } from '../store'
import DiffView from './DiffView.vue'

const props = defineProps<{ tool: ToolDisplay; latest?: boolean }>()
const { selectSession } = useStore()

const outputPre = ref<HTMLPreElement | null>(null)

// Auto-scroll the bash output box to the bottom as new lines stream in.
// Only when the user is already near the bottom -- otherwise respect their
// scroll position (e.g. reading earlier output).
watch(
  () => props.tool.liveOutput,
  () => {
    const el = outputPre.value
    if (props.tool.status !== 'running' || !el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    if (nearBottom) {
      nextTick(() => {
        if (outputPre.value) {
          outputPre.value.scrollTop = outputPre.value.scrollHeight
        }
      })
    }
  },
)

const isRead = computed(() => props.tool.name === 'read')
const isTask = computed(() => props.tool.name === 'task')
const isBash = computed(() => props.tool.name === 'bash')
const isSearch = computed(() => props.tool.name === 'grep' || props.tool.name === 'glob')
const isSleep = computed(() => props.tool.name === 'sleep_until')

// Model-supplied purpose of a bash call (required param), shown on the first
// line of the bash block so the user can tell what it is for at a glance.
const bashDescription = computed(() => {
  if (props.tool.name !== 'bash') return ''
  const d = props.tool.startArgs?.description
  return typeof d === 'string' ? d : ''
})

const command = computed(() => {
  const c = props.tool.startArgs?.command
  return typeof c === 'string' ? c : ''
})

// Two-line bash header: description first, then the command indented under
// it. Newline + indent live in the string (rendered via .bash-action's
// white-space: pre-wrap) so the model's exact wording is preserved.
const bashLabel = computed(() => {
  const desc = bashDescription.value
  if (desc) return `${desc}\n    cmd: ${command.value}`
  return command.value
})

const subagents = computed(() => props.tool.subagents ?? [])

const taskCount = computed(() => {
  const tasks = props.tool.startArgs?.tasks
  return Array.isArray(tasks) ? tasks.length : subagents.value.length
})

function stateLabel(s: string): string {
  if (s === 'running') return 'running'
  if (s === 'completed') return 'done'
  if (s === 'interrupted') return 'interrupted'
  if (s === 'error') return 'error'
  return s
}

function openChild(session: string) {
  selectSession(session)
}

const label = computed(() => {
  const args = props.tool.startArgs
  if (!args) return props.tool.name
  if (props.tool.name === 'read' && args.filename) {
    return `read: ${args.filename}`
  }
  if (props.tool.name === 'edit' && args.filename) {
    return `edit: ${args.filename}`
  }
  if (props.tool.name === 'write' && args.filename) {
    return `write: ${args.filename}`
  }
  if (props.tool.name === 'glob' && args.pattern) {
    return args.path ? `glob: ${args.pattern} in ${args.path}` : `glob: ${args.pattern}`
  }
  if (props.tool.name === 'grep' && args.pattern) {
    let s = `grep: ${args.pattern}`
    if (args.glob) s += ` (${args.glob})`
    if (args.path) s += ` in ${args.path}`
    return s
  }
  return props.tool.name
})

const displayContent = computed(() => {
  if (props.tool.status === 'done') {
    return props.tool.result ?? ''
  }
  if (props.tool.status === 'streaming') {
    return props.tool.args || '...'
  }
  // running: show live streamed output (bash) if available
  if (props.tool.liveOutput) {
    return props.tool.liveOutput
  }
  return props.tool.args || 'running...'
})

// Non-latest turns collapse bash / grep / glob output to the last 3 lines so
// old tool results don't eat the whole viewport; the latest bubble keeps the
// full streaming output. The header always stays visible, and clicking it
// (when collapsible) expands the full output on demand.
const expanded = ref(false)
const readOpen = ref(false)

const collapsible = computed(() => {
  if (props.latest) return false
  return displayContent.value.split('\n').length > 3
})

function toggleCollapsed() {
  if (collapsible.value) expanded.value = !expanded.value
}

const collapsedContent = computed(() => {
  const full = displayContent.value
  if (props.latest || expanded.value) return full
  const lines = full.split('\n')
  if (lines.length <= 3) return full
  return lines.slice(-3).join('\n')
})
</script>
