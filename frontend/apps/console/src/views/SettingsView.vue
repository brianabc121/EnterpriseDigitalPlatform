<script setup lang="ts">
import { ref } from 'vue'

import ChannelsTab from '../components/settings/ChannelsTab.vue'

const tab = ref('channels')
</script>

<template>
  <div>
    <div class="page-header">
      <h2>设置</h2>
    </div>
    <el-tabs v-model="tab" data-testid="settings-tabs">
      <el-tab-pane label="接入渠道" name="channels" lazy>
        <ChannelsTab />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}
</style>
