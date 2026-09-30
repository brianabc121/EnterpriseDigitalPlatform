import { type Schemas } from '@edp/api-client'
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api } from '../api'

/** 知识空间和分类（知识库、条目编辑、导入、渠道设置共用），变更后调用 load 刷新。 */
export const useKbSpacesStore = defineStore('kbSpaces', () => {
  const spaces = ref<Schemas['KbSpaceOut'][]>([])
  const unassigned = ref(0)
  const loaded = ref(false)

  async function load(): Promise<void> {
    const { data } = await api.GET('/api/v1/kb/spaces')
    spaces.value = data?.items ?? []
    unassigned.value = data?.unassigned ?? 0
    loaded.value = true
  }

  async function ensure(): Promise<void> {
    if (!loaded.value) await load()
  }

  return { spaces, unassigned, loaded, load, ensure }
})
