<template>
  <div class="user-message">
    <template v-if="!editing">
      <button class="user-pencil" @click="startEdit" title="编辑并重发"><span class="ms ms-edit-square"></span></button>
      <div class="user-content">
        <span class="message user">{{ item.content }}</span>
        <div v-if="item.images?.length" class="user-images">
          <img
            v-for="img in item.images"
            :key="img.filename"
            class="user-image"
            :src="imageUrl(img.filename)"
            :title="img.original_name + (img.date ? ' · ' + img.date : '')"
            @click="openLightbox(imageUrl(img.filename))"
          />
        </div>
      </div>
      <div v-if="lightboxImage" class="lightbox" @click.self="closeLightbox">
        <button class="lightbox-close" @click="closeLightbox" title="关闭"><span class="ms ms-close"></span></button>
        <img class="lightbox-img" :src="lightboxImage" @click.stop />
      </div>
    </template>
    <div v-else class="user-edit-wrap">
      <textarea
        ref="taRef"
        v-model="draft"
        class="user-edit-input"
        rows="2"
        @keydown.esc="cancelEdit"
        @paste="onEditPaste"
      ></textarea>
      <div v-if="draftImages.length || draftNew.length || enableVl" class="edit-images">
        <div v-for="(img, idx) in draftImages" :key="'kept-' + img.filename" class="edit-image-thumb">
          <img :src="imageUrl(img.filename)" :title="img.original_name" />
          <button class="image-remove" @click="removeKept(idx)" title="Remove"><span class="ms ms-close"></span></button>
        </div>
        <div v-for="(img, idx) in draftNew" :key="'new-' + idx" class="edit-image-thumb">
          <img :src="img.preview_url" :title="img.original_name" />
          <button class="image-remove" @click="removeNew(idx)" title="Remove"><span class="ms ms-close"></span></button>
        </div>
        <button v-if="enableVl" class="edit-image-add" @click="imageInput?.click()" title="Add image"><span class="ms ms-attach-file"></span></button>
      </div>
      <div class="user-edit-actions">
        <button class="user-edit-btn" @click="confirmEdit" title="确认"><span class="ms ms-check"></span></button>
        <button class="user-edit-btn" @click="cancelEdit" title="取消"><span class="ms ms-close"></span></button>
      </div>
      <input
        ref="imageInput"
        type="file"
        accept="image/*"
        multiple
        style="display: none"
        @change="onImageSelect"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import type { ImageMeta, UserItem } from '../types'
import { useStore } from '../store'
import { readFileAsDataURL } from '../utils/images'

const props = defineProps<{ item: UserItem }>()
const { state, enableVl, revert } = useStore()
const sessionId = state.currentSession

const editing = ref(false)
const draft = ref('')
const taRef = ref<HTMLTextAreaElement | null>(null)

// Draft attachments while editing: kept ones reference the original message's
// stored files (removal just drops them from the list), new ones ride as data
// URLs and are uploaded on confirm.
interface NewImage {
  mime_type: string
  original_name: string
  preview_url: string
  data_url: string
}

const draftImages = ref<ImageMeta[]>([])
const draftNew = ref<NewImage[]>([])
const imageInput = ref<HTMLInputElement | null>(null)
const lightboxImage = ref<string | null>(null)

function imageUrl(filename: string): string {
  return `/api/sessions/${sessionId}/images/${filename}`
}

function openLightbox(url: string) {
  lightboxImage.value = url
  window.addEventListener('keydown', onKeydown)
}

function closeLightbox() {
  lightboxImage.value = null
  window.removeEventListener('keydown', onKeydown)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    closeLightbox()
  }
}

function startEdit() {
  draft.value = props.item.content
  draftImages.value = (props.item.images ?? []).map((img) => ({ ...img }))
  clearDraftNew()
  editing.value = true
  nextTick(() => taRef.value?.focus())
}

function clearDraftNew() {
  for (const img of draftNew.value) URL.revokeObjectURL(img.preview_url)
  draftNew.value = []
}

function cancelEdit() {
  editing.value = false
  clearDraftNew()
}

function removeKept(idx: number) {
  draftImages.value.splice(idx, 1)
}

function removeNew(idx: number) {
  const img = draftNew.value[idx]
  if (img) URL.revokeObjectURL(img.preview_url)
  draftNew.value.splice(idx, 1)
}

async function addDraftNew(file: File) {
  const data_url = await readFileAsDataURL(file)
  draftNew.value.push({
    mime_type: file.type,
    original_name: file.name,
    preview_url: URL.createObjectURL(file),
    data_url,
  })
}

async function onImageSelect(event: Event) {
  const target = event.target as HTMLInputElement
  for (const file of Array.from(target.files || [])) {
    await addDraftNew(file)
  }
  target.value = ''
}

// Paste clipboard images into the edit's attachment list (screenshots land
// here too); plain-text paste passes through untouched.
async function onEditPaste(event: ClipboardEvent) {
  if (!enableVl.value) return
  const items = event.clipboardData?.items
  if (!items) return
  let hasImage = false
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      hasImage = true
      const file = item.getAsFile()
      if (file) await addDraftNew(file)
    }
  }
  if (hasImage) event.preventDefault()
}

async function confirmEdit() {
  const id = props.item.id
  const text = draft.value
  editing.value = false
  const newImages = [...draftNew.value]
  clearDraftNew()
  if (id !== undefined && text.trim()) {
    await revert(id, text, {
      keepImages: draftImages.value.map((img) => img.filename),
      newImages: newImages.map(({ mime_type, data_url, original_name }) => ({ mime_type, data_url, original_name })),
    })
  }
}
</script>

<style scoped>
.user-content {
  display: block;
  flex: 1;
}
.user-images {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}
.user-image {
  width: 120px;
  height: 120px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid #3c3c3c;
  cursor: pointer;
}

.edit-images {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.edit-image-thumb {
  position: relative;
  width: 64px;
  height: 64px;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #3c3c3c;
}
.edit-image-thumb img {
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
.edit-image-add {
  width: 64px;
  height: 64px;
  border-radius: 6px;
  border: 1px dashed #3c3c3c;
  background: transparent;
  color: #999;
  cursor: pointer;
  font-size: 16px;
}

.lightbox {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
}

.lightbox-close {
  position: absolute;
  top: 16px;
  right: 16px;
  background: transparent;
  border: none;
  color: #ffffff;
  font-size: 24px;
  cursor: pointer;
  line-height: 1;
}

.lightbox-img {
  max-width: 90vw;
  max-height: 90vh;
  object-fit: contain;
  border-radius: 6px;
}
</style>
