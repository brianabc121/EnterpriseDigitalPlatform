<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <el-result icon="info" :title="route.meta.title" sub-title="该模块将在后续阶段上线" />
</template>
