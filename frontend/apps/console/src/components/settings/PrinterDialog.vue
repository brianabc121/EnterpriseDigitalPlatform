<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import {
  BRANDS,
  brandInfo,
  USE_LABEL,
  type Printer,
  type PrinterBrand,
  type PrinterIn,
  type PrinterUse,
} from '../../printing'

/**
 * 添加或修改云打印机（设计文档 §29.4）：选厂商后显示填写指引；保存时后端先在厂商那里添加打印机并查
 * 一次状态，账号、密钥或编号不对就保存不了。密钥加密保存、不回显，修改时留空表示不改。
 */
const props = defineProps<{ modelValue: boolean; printer: Printer | null }>()
const emit = defineEmits<{
  'update:modelValue': [open: boolean]
  saved: [printer: Printer]
}>()

const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})
const editing = computed(() => props.printer !== null)
const saving = ref(false)

const form = reactive({
  brand: 'xpyun' as PrinterBrand,
  name: '',
  account: '',
  key: '',
  sn: '',
  deviceKey: '',
  uses: ['order', 'requisition'] as PrinterUse[],
  copies: 1,
  enabled: true,
})
const brand = computed(() => brandInfo(form.brand))

watch(open, (value) => {
  if (!value) return
  const p = props.printer
  Object.assign(form, {
    brand: p?.brand ?? 'xpyun',
    name: p?.name ?? '',
    account: p?.account ?? '',
    key: '',
    sn: p?.sn ?? '',
    deviceKey: p?.device_key ?? '',
    uses: p ? [...p.uses] : ['order', 'requisition'],
    copies: p?.copies ?? 1,
    enabled: p?.enabled ?? true,
  })
})

function payload(): PrinterIn | null {
  if (!form.name.trim()) {
    ElMessage.warning('请填写打印机名称')
    return null
  }
  if (!form.account.trim()) {
    ElMessage.warning(`请填写${brand.value.accountLabel}`)
    return null
  }
  if (!editing.value && !form.key.trim()) {
    ElMessage.warning(`请填写${brand.value.keyLabel}`)
    return null
  }
  if (!/^[A-Za-z0-9_-]+$/.test(form.sn.trim())) {
    ElMessage.warning('请填写打印机编号（字母和数字）')
    return null
  }
  if (brand.value.needsDeviceKey && !form.deviceKey.trim()) {
    ElMessage.warning('请填写机身标签上的 KEY')
    return null
  }
  return {
    name: form.name.trim(),
    brand: form.brand,
    account: form.account.trim(),
    key: form.key.trim() || null,
    sn: form.sn.trim(),
    device_key: brand.value.needsDeviceKey ? form.deviceKey.trim() : '',
    uses: form.uses,
    copies: form.copies,
    enabled: form.enabled,
  }
}

async function save(): Promise<void> {
  const body = payload()
  if (!body || saving.value) return
  saving.value = true
  const { data, error } = props.printer
    ? await api.PUT('/api/v1/print/printers/{printer_id}', {
        params: { path: { printer_id: props.printer.id } },
        body,
      })
    : await api.POST('/api/v1/print/printers', { body })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(props.printer ? '已保存' : '打印机已接入，可以先打一张测试页')
  emit('saved', data)
  open.value = false
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="editing ? '修改打印机' : '添加打印机'"
    width="600px"
    destroy-on-close
    data-testid="printer-dialog"
  >
    <el-form label-width="110px" @submit.prevent>
      <el-form-item label="厂商">
        <el-select v-model="form.brand" :disabled="editing" data-testid="printer-brand">
          <el-option v-for="b in BRANDS" :key="b.value" :label="b.label" :value="b.value" />
        </el-select>
      </el-form-item>
      <el-alert
        type="info"
        :closable="false"
        class="help"
        data-testid="printer-help"
        :title="brand.help"
      />
      <el-form-item label="名称" required>
        <el-input
          v-model="form.name"
          maxlength="64"
          placeholder="例如：车间打印机"
          data-testid="printer-name"
        />
      </el-form-item>
      <el-form-item :label="brand.accountLabel" required>
        <el-input v-model="form.account" maxlength="128" data-testid="printer-account" />
      </el-form-item>
      <el-form-item :label="brand.keyLabel" :required="!editing">
        <el-input
          v-model="form.key"
          type="password"
          show-password
          autocomplete="new-password"
          maxlength="128"
          :placeholder="editing ? '不修改请留空' : ''"
          data-testid="printer-key"
        />
      </el-form-item>
      <el-form-item label="打印机编号" required>
        <el-input
          v-model="form.sn"
          maxlength="64"
          :placeholder="brand.snHint"
          data-testid="printer-sn"
        />
      </el-form-item>
      <el-form-item v-if="brand.needsDeviceKey" label="打印机 KEY" required>
        <el-input
          v-model="form.deviceKey"
          maxlength="64"
          placeholder="机身标签上的 KEY"
          data-testid="printer-device-key"
        />
      </el-form-item>
      <el-form-item label="自动打印">
        <el-checkbox-group v-model="form.uses">
          <el-checkbox
            v-for="(label, use) in USE_LABEL"
            :key="use"
            :value="use"
            :data-testid="`printer-use-${use}`"
          >
            {{ label }}
          </el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item label="份数">
        <el-input-number v-model="form.copies" :min="1" :max="3" data-testid="printer-copies" />
      </el-form-item>
      <el-form-item v-if="editing" label="启用">
        <el-switch v-model="form.enabled" data-testid="printer-enabled" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="printer-save" @click="save">
        {{ editing ? '保存' : '验证并保存' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.help {
  margin: -6px 0 14px 110px;
  width: auto;
}
</style>
