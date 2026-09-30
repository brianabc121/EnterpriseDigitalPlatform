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

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

h3 {
  margin: 0;
  font-size: 15px;
}

.extra {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

:slotted(.tiles) {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
}
</style>
