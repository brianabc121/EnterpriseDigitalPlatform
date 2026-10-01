<script setup lang="ts">
/** 首页上的一块内容（§25.15）：标题、右侧的链接或按钮，下面是数字或列表。 */
defineProps<{ title: string; testid?: string }>()
</script>

<template>
  <section class="home-section" :data-testid="testid">
    <header class="head">
      <h3>{{ title }}</h3>
      <span class="extra"><slot name="extra" /></span>
    </header>
    <slot />
  </section>
</template>

<style scoped>
.home-section {
  margin-top: 20px;
}

/* 手机上标题和右侧的按钮放不下时按钮换到下一行。 */
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  margin-bottom: 10px;
}

h3 {
  margin: 0;
  font-size: 15px;
  white-space: nowrap;
}

.extra {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.extra :deep(.el-button + .el-button) {
  margin-left: 0;
}

:slotted(.tiles) {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
}

/* 手机上两列，少滚动。 */
@media (max-width: 480px) {
  :slotted(.tiles) {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
}
</style>
