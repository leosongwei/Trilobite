<template>
  <div class="user-message">
    <template v-if="!editing">
      <button class="user-pencil" @click="startEdit" title="编辑并重发">✎</button>
      <span class="message user">{{ item.content }}</span>
    </template>
    <div v-else class="user-edit-wrap">
      <textarea
        ref="taRef"
        v-model="draft"
        class="user-edit-input"
        rows="2"
        @keydown.esc="cancelEdit"
      ></textarea>
      <div class="user-edit-actions">
        <button class="user-edit-btn" @click="confirmEdit" title="确认">✓</button>
        <button class="user-edit-btn" @click="cancelEdit" title="取消">✗</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import type { UserItem } from '../types'
import { useStore } from '../store'

const props = defineProps<{ item: UserItem }>()
const { revert } = useStore()

const editing = ref(false)
const draft = ref('')
const taRef = ref<HTMLTextAreaElement | null>(null)

function startEdit() {
  draft.value = props.item.content
  editing.value = true
  nextTick(() => taRef.value?.focus())
}

function cancelEdit() {
  editing.value = false
}

async function confirmEdit() {
  const seq = props.item.userSeq
  const text = draft.value
  editing.value = false
  if (seq !== undefined && text.trim()) {
    await revert(seq, text)
  }
}
</script>
