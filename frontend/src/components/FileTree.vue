<template>
  <div class="file-tree">
    <template v-for="node in displayNodes" :key="node.path">
      <div class="tree-row dir" :class="{ changed: node.changed }" @click="toggle(node)">
        <span class="tree-arrow" :class="{ open: node.expanded }">&#9656;</span>
        <span class="tree-icon">&#128193;</span>
        <span class="tree-name" :title="node.path">{{ node.name }}</span>
      </div>
      <div v-if="node.expanded" class="tree-children">
        <div v-if="node.loading" class="tree-hint">loading&#8230;</div>
        <div v-else-if="node.error" class="tree-hint tree-error">{{ node.error }}</div>
        <template v-else>
          <FileTree
            v-if="node.subdirs.length"
            :nodes="node.subdirs"
            :session-id="sessionId"
            :base="base"
            @open-file="(f) => emit('open-file', f)"
            @root-info="(info) => emit('root-info', info)"
          />
          <div
            v-for="f in node.files"
            :key="node.path + '/' + f.name"
            class="tree-row file"
            :class="{ deleted: f.status === 'deleted', changed: f.status && f.status !== 'clean' }"
            @click="openFile(node, f)"
          >
            <!-- Empty arrow keeps file names aligned with directory names. -->
            <span class="tree-arrow"></span>
            <span class="tree-icon">&#128196;</span>
            <span class="tree-name" :title="node.path + '/' + f.name">{{ f.name }}</span>
            <span
              v-if="f.status && f.status !== 'clean'"
              class="tree-badge"
              :class="f.status"
              :title="f.status"
            >{{ badgeChar(f.status) }}</span>
          </div>
          <div v-if="node.truncated" class="tree-hint">directory too large, truncated</div>
        </template>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getFileList } from '../api'
import type { FsEntry, FsListing } from '../api'

export interface FsRoot {
  path: string
  name: string
}

export interface DirNode {
  path: string
  name: string
  expanded: boolean
  loading: boolean
  loaded: boolean
  error: string | null
  files: FsEntry[]
  subdirs: DirNode[]
  truncated: boolean
  changed: boolean
  isGit: boolean
  branches: string[]
  currentBranch: string
}

export interface OpenFilePayload {
  path: string
  name: string
  status?: string
}

const props = defineProps<{
  sessionId: string
  roots?: FsRoot[]
  nodes?: DirNode[]
  // Diff mode: when set, listings report change status vs this base branch
  // instead of vs HEAD, and changed entries are highlighted in the tree.
  base?: string | null
}>()
const emit = defineEmits<{
  'open-file': [file: OpenFilePayload]
  'root-info': [info: { path: string; isGit: boolean; branches: string[]; currentBranch: string }]
}>()

const rootNodes = ref<DirNode[]>([])

// Top-level component receives workspace roots; recursive children receive
// already-built DirNodes for subdirectories.
const displayNodes = computed<DirNode[]>(() => (props.roots ? rootNodes.value : props.nodes ?? []))

function badgeChar(status: string): string {
  return status === 'untracked' ? 'U' : status[0].toUpperCase()
}

function makeNode(path: string, name: string): DirNode {
  return {
    path,
    name,
    expanded: false,
    loading: false,
    loaded: false,
    error: null,
    files: [],
    subdirs: [],
    truncated: false,
    changed: false,
    isGit: false,
    branches: [],
    currentBranch: '',
  }
}

async function loadNode(node: DirNode, recursive = false) {
  node.loading = true
  node.error = null
  try {
    const listing: FsListing = await getFileList(props.sessionId, node.path, props.base)
    node.isGit = listing.is_git_repo
    node.branches = listing.branches
    node.currentBranch = listing.current_branch
    node.truncated = listing.truncated
    node.files = listing.entries.filter((e) => !e.is_dir)
    // A directory's own diff-mode status lives in its parent's listing: the
    // parent stamps it on the child node below. Rebuild subdirs but keep
    // existing nodes so expanded/loaded state survives reloads (e.g. base
    // switches); brand-new directories are created fresh.
    const oldSubs = node.subdirs
    node.subdirs = listing.entries
      .filter((e) => e.is_dir)
      .map((e) => {
        const existing = oldSubs.find((s) => s.path === `${node.path}/${e.name}`)
        const sub = existing ?? makeNode(`${node.path}/${e.name}`, e.name)
        sub.changed = e.status != null && e.status !== 'clean'
        return sub
      })
    node.loaded = true
    emit('root-info', {
      path: node.path,
      isGit: node.isGit,
      branches: node.branches,
      currentBranch: node.currentBranch,
    })
    if (recursive) {
      for (const sub of node.subdirs) {
        if (sub.loaded) loadNode(sub, true)
      }
    }
  } catch (err) {
    node.error = err instanceof Error ? err.message : String(err)
  } finally {
    node.loading = false
  }
}

// Diff-mode base switch: reload every loaded directory (keeping expansion
// state) so the highlighting follows the selected branch.
watch(
  () => props.base,
  () => reloadAll(rootNodes.value),
)

function reloadAll(nodes: DirNode[]) {
  for (const n of nodes) {
    if (n.loaded) loadNode(n, true)
  }
}

function toggle(node: DirNode) {
  node.expanded = !node.expanded
  if (node.expanded && !node.loaded) loadNode(node)
}

function openFile(node: DirNode, f: FsEntry) {
  emit('open-file', { path: `${node.path}/${f.name}`, name: f.name, status: f.status })
}

// Reload a directory's listing (e.g. after saving a file) so git badges and
// new files show up. Returns the root info for the path's workspace root.
function reloadDir(path: string) {
  const node = findNode(path, rootNodes.value)
  if (node && node.loaded) loadNode(node)
}

function findNode(path: string, nodes: DirNode[]): DirNode | null {
  for (const n of nodes) {
    if (n.path === path) return n
    const found = findNode(path, n.subdirs)
    if (found) return found
  }
  return null
}

// Workspace roots: rebuild the tree whenever they change (session switch,
// additional dirs). The root starts expanded so the tree opens on content.
watch(
  () => props.roots,
  (roots) => {
    if (!roots) {
      rootNodes.value = []
      return
    }
    rootNodes.value = roots.map((r) => makeNode(r.path, r.name))
    for (const node of rootNodes.value) {
      node.expanded = true
      loadNode(node)
    }
  },
  { immediate: true },
)

defineExpose({ reloadDir, findNode })
</script>

<style scoped>
.file-tree {
  font-size: 13px;
  user-select: none;
}
.tree-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 6px;
  border-radius: 3px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
}
.tree-row:hover {
  background: #2a2d2e;
}
.tree-row.dir {
  color: #d4d4d4;
}
.tree-arrow {
  display: inline-block;
  width: 10px;
  font-size: 10px;
  color: #858585;
  transition: transform 0.1s;
  flex-shrink: 0;
}
.tree-arrow.open {
  transform: rotate(90deg);
}
.tree-icon {
  font-size: 12px;
  flex-shrink: 0;
}
.tree-name {
  overflow: hidden;
  text-overflow: ellipsis;
}
/* Each nesting level indents by 16px; directory and file rows share the same
   level inside tree-children so their names align. */
.tree-children {
  padding-left: 16px;
}
.tree-row.file {
  color: #cccccc;
}
/* Diff mode: entries with changes vs the base branch get a yellow name;
   directories whose subtree contains a change are marked the same way. */
.tree-row.changed .tree-name {
  color: #d29922;
}
/* Deleted files (in the base branch, gone from the worktree): red + strike. */
.tree-row.file.deleted .tree-name {
  color: #f14c4c;
  text-decoration: line-through;
}
.tree-hint {
  padding: 3px 10px;
  font-size: 11px;
  color: #6e7681;
}
.tree-error {
  color: #f14c4c;
}
.tree-badge {
  font-size: 9px;
  font-weight: 700;
  padding: 1px 4px;
  border-radius: 3px;
  margin-left: auto;
  flex-shrink: 0;
  background: #3a3d3e;
  color: #cccccc;
}
.tree-badge.modified { background: #4a3a1f; color: #d29922; }
.tree-badge.added { background: #1f3a2e; color: #3fb950; }
.tree-badge.untracked { background: #1f3a5f; color: #9cdcfe; }
.tree-badge.deleted { background: #3a2f3a; color: #b18c8c; }
</style>
