<script setup lang="ts">
import { Document, Files, Notebook, Picture, VideoPlay } from '@element-plus/icons-vue'
import type { Component } from 'vue'

import { formatDateTime } from '../../api'
import {
  dateText,
  KIND_LABEL,
  KIND_TAG,
  plainExcerpt,
  sizeText,
  type Material,
  type MaterialKind,
} from '../../materials'

/** 资料列表（§36.4）：卡片（视频截帧、图片缩略图作封面，文字资料显示正文开头）或表格；点一份打开详情。 */
defineProps<{ items: Material[]; mode: 'grid' | 'table'; loading: boolean }>()
const emit = defineEmits<{ open: [id: string] }>()

const ICON: Record<MaterialKind, Component> = {
  video: VideoPlay,
  document: Document,
  image: Picture,
  text: Notebook,
  other: Files,
}

function format(item: Material): string {
  return item.ext.replace('.', '').toUpperCase()
}
</script>

<template>
  <div v-loading="loading" class="list">
    <el-empty v-if="!items.length && !loading" :image-size="72" description="没有资料" />
    <div v-else-if="mode === 'grid'" class="grid" data-testid="material-grid">
      <button
        v-for="item in items"
        :key="item.id"
        type="button"
        class="card"
        :data-testid="`material-card-${item.name}`"
        @click="emit('open', item.id)"
      >
        <div class="cover" :class="item.kind">
          <img v-if="item.cover_url" :src="item.cover_url" alt="" loading="lazy" class="cover-img" />
          <p v-else-if="item.kind === 'text' && item.excerpt" class="excerpt">{{ plainExcerpt(item.excerpt) }}</p>
          <div v-else class="icon">
            <el-icon :size="34"><component :is="ICON[item.kind]" /></el-icon>
            <span>{{ format(item) }}</span>
          </div>
          <span v-if="item.kind === 'video' && item.cover_url" class="play" aria-hidden="true">
            <el-icon :size="22"><VideoPlay /></el-icon>
          </span>
          <el-tag v-if="item.status === 'uploading'" disable-transitions size="small" type="warning" class="badge">上传没有完成</el-tag>
          <el-tag v-else-if="item.status === 'blocked'" disable-transitions size="small" type="danger" class="badge">已拦截</el-tag>
          <el-tag v-else-if="item.scan_status === 'pending'" disable-transitions size="small" type="info" class="badge">等待扫描</el-tag>
        </div>
        <div class="name" :title="item.name">{{ item.name }}</div>
        <div class="meta">
          <span>{{ KIND_LABEL[item.kind] }} · {{ sizeText(item.size) }}</span>
          <span v-if="item.shares" class="shared" title="有效的分享链接">已分享</span>
        </div>
        <div class="meta">{{ item.created_by_name ?? '—' }} · {{ dateText(item.created_at) }}</div>
      </button>
    </div>
    <el-table
      v-else
      :data="items"
      row-key="id"
      class="table"
      data-testid="material-table"
      @row-click="(row: Material) => emit('open', row.id)"
    >
      <el-table-column label="名称" min-width="240">
        <template #default="{ row }: { row: Material }">
          <div class="row-name" :data-testid="`material-row-${row.name}`">
            <el-icon><component :is="ICON[row.kind]" /></el-icon>
            <span class="title">{{ row.name }}</span>
            <el-tag v-if="row.status === 'uploading'" disable-transitions size="small" type="warning">上传没有完成</el-tag>
            <el-tag v-else-if="row.status === 'blocked'" disable-transitions size="small" type="danger">已拦截</el-tag>
          </div>
          <div v-if="row.tags.length" class="row-tags">
            <el-tag v-for="tag in row.tags" :key="tag" disable-transitions size="small" effect="plain">{{ tag }}</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="100">
        <template #default="{ row }: { row: Material }">
          <el-tag disable-transitions size="small" :type="KIND_TAG[row.kind]" effect="plain">{{ KIND_LABEL[row.kind] }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="文件夹" min-width="140">
        <template #default="{ row }: { row: Material }">
          <span class="muted">{{ row.folder_path || '未归档' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="90" align="right">
        <template #default="{ row }: { row: Material }">{{ sizeText(row.size) }}</template>
      </el-table-column>
      <el-table-column label="上传" width="160">
        <template #default="{ row }: { row: Material }">
          <div>{{ row.created_by_name ?? '—' }}</div>
          <div class="muted">{{ formatDateTime(row.created_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="浏览 / 下载" width="100" align="right">
        <template #default="{ row }: { row: Material }">{{ row.views }} / {{ row.downloads }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.list {
  min-height: 160px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}

.card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding: 0 0 10px;
  overflow: hidden;
  font: inherit;
  color: inherit;
  text-align: left;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
  cursor: pointer;
  transition: box-shadow 0.15s, border-color 0.15s;
}

.card:hover,
.card:focus-visible {
  border-color: var(--el-color-primary-light-5);
  box-shadow: var(--el-box-shadow-light);
  outline: none;
}

.cover {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 120px;
  overflow: hidden;
  background: var(--el-fill-color-light);
}

.cover.video {
  background: #1f2329;
  color: #fff;
}

.cover-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.icon {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.cover.video .icon {
  color: #c9cdd4;
}

.excerpt {
  align-self: stretch;
  margin: 0;
  padding: 10px 12px;
  overflow: hidden;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  word-break: break-word;
}

.play {
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  color: #fff;
  background: rgb(0 0 0 / 45%);
}

.badge {
  position: absolute;
  top: 6px;
  left: 6px;
}

.name {
  margin: 6px 10px 0;
  overflow: hidden;
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  margin: 0 10px;
  overflow: hidden;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.shared {
  color: var(--el-color-success);
}

.table :deep(.el-table__row) {
  cursor: pointer;
}

.row-name {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
