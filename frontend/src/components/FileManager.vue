<template>
  <div class="fm">
    <div class="fm-topbar">
      <button class="fm-back" @click="tryClose">&#8592; 对话</button>
      <span class="fm-filepath" :title="selectedFile?.path">{{ selectedFile ? selectedFile.path : '未选择文件' }}</span>
      <span v-if="savedTick" class="fm-saved">已保存</span>
      <div class="fm-actions">
        <template v-if="view === 'diff' && selectedFile && !deleted">
          <span class="fm-vs">HEAD vs</span>
          <select
            v-if="rootIsGit"
            :value="base"
            class="fm-base"
            title="对比分支"
            @change="emit('update:base', ($event.target as HTMLSelectElement).value)"
          >
            <option v-for="b in branches" :key="b" :value="b">{{ b }}</option>
          </select>
        </template>
        <div class="fm-tabs">
          <button :class="{ active: view === 'view' }" @click="switchView('view')">查看</button>
          <button :class="{ active: view === 'diff' }" :disabled="!rootIsGit || deleted" @click="switchView('diff')">Diff</button>
          <button :class="{ active: view === 'edit' }" :disabled="deleted" @click="switchView('edit')">编辑</button>
        </div>
        <template v-if="view === 'edit' && selectedFile && !deleted">
          <button class="fm-primary" :disabled="saving || !dirty" @click="save">保存</button>
          <button class="fm-cancel" @click="cancelEdit">取消</button>
        </template>
      </div>
    </div>
    <div class="fm-body">
      <div class="fm-content">
        <div v-if="error" class="fm-error">{{ error }}</div>
        <div v-if="!selectedFile" class="fm-empty">从左侧选择文件查看、对比或编辑</div>
        <template v-else-if="deleted">
          <div class="fm-empty">文件已删除，无法查看</div>
        </template>
        <template v-else-if="view === 'view'">
          <div v-if="loading" class="fm-empty">loading&#8230;</div>
          <pre v-else class="fm-code"><code v-html="highlighted"></code></pre>
        </template>
        <template v-else-if="view === 'diff'">
          <div v-if="diffLoading" class="fm-empty">loading&#8230;</div>
          <DiffView v-else :rows="diffRows" />
        </template>
        <template v-else>
          <textarea
            ref="editorRef"
            v-model="editContent"
            class="fm-editor"
            spellcheck="false"
            @keydown.ctrl.s.prevent="save"
            @keydown.meta.s.prevent="save"
            @keydown.esc="cancelEdit"
          ></textarea>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import hljs from 'highlight.js'
import 'highlight.js/styles/vs2015.css'
import { getFileContent, getFileDiff, saveFile } from '../api'
import type { DiffRow } from '../types'
import DiffView from './DiffView.vue'
import type { OpenFilePayload } from './FileTree.vue'

export interface RootInfo {
  isGit: boolean
  branches: string[]
  currentBranch: string
}

// The file to open is handed in from the sidebar tree; root info (branches,
// git-ness) is collected there too and passed down for the diff UI. The base
// branch is shared with the sidebar tree so its highlighting follows the
// branch selector here.
const props = defineProps<{
  sessionId: string
  file: OpenFilePayload | null
  rootInfo: Record<string, RootInfo>
  base: string
}>()
const emit = defineEmits<{
  close: []
  'file-saved': [dir: string]
  'update:base': [b: string]
}>()

const editorRef = ref<HTMLTextAreaElement | null>(null)
const selectedFile = ref<OpenFilePayload | null>(null)
// Diff is the default view: changed files are highlighted vs the base branch.
const view = ref<'view' | 'diff' | 'edit'>('diff')
const content = ref('')
const editContent = ref('')
const diffRows = ref<DiffRow[]>([])
const branches = ref<string[]>([])
const rootIsGit = ref(false)
const loading = ref(false)
const diffLoading = ref(false)
const saving = ref(false)
const error = ref('')
const savedTick = ref(false)

const deleted = computed(() => selectedFile.value?.status === 'deleted')
const dirty = computed(() => view.value === 'edit' && editContent.value !== content.value)

// Open whatever file the sidebar tree hands over; switching files keeps the
// current view mode.
watch(
  () => props.file,
  (f) => {
    if (f) openFile(f)
  },
  { immediate: true },
)

// ── syntax highlighting (view mode) ──

const LANG_BY_EXT: Record<string, string> = {
  py: 'python', js: 'javascript', jsx: 'javascript', mjs: 'javascript',
  ts: 'typescript', tsx: 'typescript', vue: 'xml', html: 'xml', xml: 'xml', svg: 'xml',
  json: 'json', jsonc: 'json', md: 'markdown', css: 'css', scss: 'scss',
  sh: 'bash', bash: 'bash', zsh: 'bash', yaml: 'yaml', yml: 'yaml',
  toml: 'ini', ini: 'ini', sql: 'sql', go: 'go', rs: 'rust', java: 'java',
  c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', cs: 'csharp', rb: 'ruby',
  php: 'php', swift: 'swift', kt: 'kotlin', dockerfile: 'dockerfile',
  lua: 'lua', r: 'r', dart: 'dart', scala: 'scala', perl: 'perl',
}

function detectLang(name: string): string | null {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  return LANG_BY_EXT[ext] ?? null
}

const highlighted = computed(() => {
  const lang = detectLang(selectedFile.value?.name ?? '')
  if (lang && hljs.getLanguage(lang)) {
    return hljs.highlight(content.value, { language: lang }).value
  }
  return hljs.highlightAuto(content.value).value
})

// ── file operations ──

function rootFor(path: string): RootInfo | undefined {
  let best: string | null = null
  for (const p of Object.keys(props.rootInfo)) {
    if (path.startsWith(p) && (best === null || p.length > best.length)) best = p
  }
  return best ? props.rootInfo[best] : undefined
}

// Apply the git state of the open file's workspace root. Called when the file
// opens and again when root info arrives late (the sidebar tree may still be
// loading when a file is clicked).
function refreshGitState() {
  if (!selectedFile.value) return
  const info = rootFor(selectedFile.value.path)
  if (!info) return
  rootIsGit.value = info.isGit
  branches.value = info.branches
  if (rootIsGit.value && !branches.value.includes(props.base)) {
    emit('update:base', info.currentBranch || branches.value[0] || 'master')
  }
  if (!rootIsGit.value && view.value === 'diff') {
    view.value = 'view'
  }
}

watch(() => props.rootInfo, refreshGitState, { deep: true })

async function openFile(f: OpenFilePayload) {
  if (view.value === 'edit' && dirty.value && !confirm('放弃未保存的修改？')) return
  selectedFile.value = f
  error.value = ''
  diffRows.value = []
  savedTick.value = false
  if (f.status === 'deleted') {
    // A deleted file has no content or diff to show.
    if (view.value !== 'view') view.value = 'view'
    return
  }
  // Keep the current view mode across file switches; only fall back when the
  // new file cannot support it.
  const prevBase = props.base
  refreshGitState()
  const baseChanged = props.base !== prevBase
  if (view.value === 'diff') {
    // Load the content too so switching to view/edit later shows it
    // instantly; when the base changed, the base watcher reloads the diff.
    const tasks: Promise<void>[] = [loadContent(f.path)]
    if (!baseChanged) tasks.push(loadDiff())
    await Promise.all(tasks)
  } else {
    await loadContent(f.path)
  }
}

async function loadContent(path: string) {
  loading.value = true
  try {
    const res = await getFileContent(props.sessionId, path)
    content.value = res.content
    editContent.value = res.content
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function switchView(v: 'view' | 'diff' | 'edit') {
  if (v === view.value) return
  if (view.value === 'edit' && dirty.value && !confirm('放弃未保存的修改？')) return
  view.value = v
  if (v === 'edit') {
    nextTick(() => editorRef.value?.focus())
  } else if (v === 'diff') {
    loadDiff()
  }
}

async function loadDiff() {
  if (!selectedFile.value) return
  diffLoading.value = true
  error.value = ''
  try {
    const res = await getFileDiff(props.sessionId, selectedFile.value.path, props.base)
    diffRows.value = res.rows
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    diffRows.value = []
  } finally {
    diffLoading.value = false
  }
}

watch(() => props.base, () => {
  if (view.value === 'diff') loadDiff()
})

async function save() {
  if (!selectedFile.value) return
  saving.value = true
  error.value = ''
  try {
    await saveFile(props.sessionId, selectedFile.value.path, editContent.value)
    content.value = editContent.value
    diffRows.value = []
    view.value = 'view'
    savedTick.value = true
    setTimeout(() => { savedTick.value = false }, 2500)
    emit('file-saved', dirname(selectedFile.value.path))
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

function cancelEdit() {
  if (dirty.value && !confirm('放弃未保存的修改？')) return
  editContent.value = content.value
  view.value = 'view'
}

function dirname(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx > 0 ? path.slice(0, idx) : path
}

function tryClose() {
  if (view.value === 'edit' && dirty.value && !confirm('放弃未保存的修改？')) return
  emit('close')
}
</script>

<style scoped>
.fm {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  flex: 1;
}
.fm-topbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #252526;
  border-bottom: 1px solid #3c3c3c;
  flex-shrink: 0;
}
.fm-back {
  background: #3a3d3e;
  color: #cccccc;
  border: none;
  border-radius: 3px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 12px;
  flex-shrink: 0;
}
.fm-back:hover {
  background: #4a4d4e;
}
.fm-filepath {
  font-size: 12px;
  color: #cccccc;
  font-family: ui-monospace, 'Cascadia Code', monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fm-saved {
  font-size: 11px;
  color: #3fb950;
  flex-shrink: 0;
}
.fm-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  flex-shrink: 0;
}
.fm-vs {
  font-size: 12px;
  color: #858585;
  white-space: nowrap;
}
.fm-base {
  background: #3a3d3e;
  color: #cccccc;
  border: 1px solid #4a4d4e;
  border-radius: 3px;
  padding: 3px 10px;
  cursor: pointer;
  font-size: 12px;
  font-family: inherit;
}
.fm-base:hover {
  background: #4a4d4e;
}
/* View tabs: a joined segmented control, the active view stands out. */
.fm-tabs {
  display: flex;
  border: 1px solid #4a4d4e;
  border-radius: 3px;
  overflow: hidden;
}
.fm-tabs button {
  background: #2d2d2d;
  color: #cccccc;
  border: none;
  border-left: 1px solid #4a4d4e;
  padding: 3px 12px;
  cursor: pointer;
  font-size: 12px;
  font-family: inherit;
}
.fm-tabs button:first-child {
  border-left: none;
}
.fm-tabs button:hover:not(:disabled):not(.active) {
  background: #3a3d3e;
}
.fm-tabs button.active {
  background: #0e639c;
  color: #ffffff;
  font-weight: 600;
}
.fm-tabs button:disabled {
  opacity: 0.5;
  cursor: default;
}
.fm-actions .fm-primary {
  background: #0e639c;
  border: 1px solid #0e639c;
  color: #ffffff;
  border-radius: 3px;
  padding: 3px 10px;
  cursor: pointer;
  font-size: 12px;
  font-family: inherit;
}
.fm-actions .fm-primary:hover:not(:disabled) {
  background: #1177bb;
}
.fm-actions .fm-primary:disabled {
  opacity: 0.5;
  cursor: default;
}
.fm-actions .fm-cancel {
  background: #3a3d3e;
  color: #cccccc;
  border: 1px solid #4a4d4e;
  border-radius: 3px;
  padding: 3px 10px;
  cursor: pointer;
  font-size: 12px;
  font-family: inherit;
}
.fm-actions .fm-cancel:hover {
  background: #4a4d4e;
}
.fm-body {
  flex: 1;
  min-height: 0;
}
.fm-content {
  height: 100%;
  overflow: auto;
  background: #1e1e1e;
  min-width: 0;
}
.fm-empty {
  padding: 24px 16px;
  color: #6e7681;
  font-size: 13px;
}
.fm-error {
  padding: 8px 16px;
  background: rgba(244, 71, 71, 0.08);
  color: #f14c4c;
  font-size: 12px;
  border-bottom: 1px solid rgba(244, 71, 71, 0.2);
}
.fm-code {
  margin: 0;
  padding: 12px 16px;
  font-size: 13px;
  line-height: 1.6;
  tab-size: 4;
}
.fm-code code {
  font-family: ui-monospace, 'Cascadia Code', monospace;
  background: transparent;
}
.fm-editor {
  width: 100%;
  height: 100%;
  box-sizing: border-box;
  background: #1e1e1e;
  color: #d4d4d4;
  border: none;
  outline: none;
  resize: none;
  padding: 12px 16px;
  font-family: ui-monospace, 'Cascadia Code', monospace;
  font-size: 13px;
  line-height: 1.6;
  tab-size: 4;
}
</style>
