<script setup lang="ts">
import { computed } from 'vue'

import { linkify } from './format'

/** 文本里的 http(s) 链接可以点击（新窗口打开），其余原样显示。 */
const props = defineProps<{ text: string }>()
const segments = computed(() => linkify(props.text))
</script>

<template>
  <span class="edp-text"
    ><template v-for="(s, i) in segments" :key="i"
      ><a
        v-if="s.kind === 'link'"
        :href="s.href"
        target="_blank"
        rel="noopener noreferrer nofollow"
        class="edp-link"
        >{{ s.text }}</a
      ><template v-else>{{ s.text }}</template></template
    ></span
  >
</template>

<style scoped>
.edp-text {
  white-space: pre-wrap;
  word-break: break-word;
}

.edp-link {
  color: inherit;
  text-decoration: underline;
  word-break: break-all;
}
</style>
