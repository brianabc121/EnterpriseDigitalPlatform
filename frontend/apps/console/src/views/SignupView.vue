<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const formRef = ref<FormInstance>()
const options = ref<Schemas['SignupOptions'] | null>(null)
const loading = ref(false)
const form = reactive({
  companyName: '',
  tenantCode: '',
  displayName: '',
  username: 'admin',
  password: '',
  contact: '',
  agree: false,
})
const rules: FormRules = {
  companyName: [{ required: true, min: 2, message: '请输入企业名称', trigger: 'blur' }],
  tenantCode: [
    {
      required: true,
      pattern: /^[a-z][a-z0-9-]{2,31}$/,
      message: '小写字母开头，3–32 位，可以包含数字和 -',
      trigger: 'blur',
    },
  ],
  displayName: [{ required: true, message: '请输入您的姓名', trigger: 'blur' }],
  username: [
    { required: true, pattern: /^[A-Za-z0-9_.-]{3,64}$/, message: '3 位以上字母或数字', trigger: 'blur' },
  ],
  password: [{ required: true, min: 8, message: '密码至少 8 位', trigger: 'blur' }],
}

onMounted(async () => {
  const { data } = await api.GET('/api/v1/signup')
  options.value = data ?? { enabled: false, plan_name: null, trial_days: null }
})

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  if (!form.agree) {
    ElMessage.warning('请先阅读并同意服务协议与隐私政策')
    return
  }
  loading.value = true
  const { data, error } = await api.POST('/api/v1/signup', {
    body: {
      company_name: form.companyName.trim(),
      tenant_code: form.tenantCode.trim(),
      admin_username: form.username.trim(),
      admin_display_name: form.displayName.trim(),
      password: form.password,
      contact: form.contact.trim() || null,
      agree: form.agree,
    },
  })
  if (!data) {
    loading.value = false
    ElMessage.error(errorMessage(error, '注册失败'))
    return
  }
  try {
    await auth.login(data.tenant_code, data.username, form.password)
    ElMessage.success(`注册成功，企业代码 ${data.tenant_code}，员工登录时需要填写`)
    await router.replace(auth.home)
  } catch {
    await router.replace({ name: 'login', query: { tenant: data.tenant_code, username: data.username } })
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="signup">
    <el-card class="card" shadow="never">
      <h1 class="title">免费试用 EDP 智能客服</h1>
      <template v-if="options && !options.enabled">
        <el-result icon="info" title="暂未开放自助注册" sub-title="请联系平台开通企业账号" />
      </template>
      <template v-else-if="options">
        <p class="subtitle">
          注册后即可开始试用{{ options.plan_name ? `「${options.plan_name}」` : '' }}{{
            options.trial_days ? `，${options.trial_days} 天内免费` : ''
          }}。
        </p>
        <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
          <el-form-item label="企业名称" prop="companyName">
            <el-input v-model="form.companyName" data-testid="signup-company" />
          </el-form-item>
          <el-form-item label="企业代码" prop="tenantCode">
            <el-input v-model="form.tenantCode" placeholder="员工登录时填写，如 acme" data-testid="signup-code" />
          </el-form-item>
          <el-form-item label="您的姓名" prop="displayName">
            <el-input v-model="form.displayName" data-testid="signup-name" />
          </el-form-item>
          <el-form-item label="管理员账号" prop="username">
            <el-input v-model="form.username" data-testid="signup-username" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" type="password" show-password data-testid="signup-password" />
          </el-form-item>
          <el-form-item label="联系电话或邮箱">
            <el-input v-model="form.contact" data-testid="signup-contact" />
          </el-form-item>
          <el-checkbox v-model="form.agree" data-testid="signup-agree">
            我已阅读并同意服务协议与隐私政策
          </el-checkbox>
          <el-button
            type="primary"
            native-type="submit"
            :loading="loading"
            class="submit"
            data-testid="signup-submit"
          >
            注册并开始试用
          </el-button>
        </el-form>
      </template>
      <p class="back"><router-link to="/login">已有账号？登录</router-link></p>
    </el-card>
  </div>
</template>

<style scoped>
.signup {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 24px 0;
}

.card {
  width: 420px;
  padding: 8px 12px;
}

.title {
  margin: 0;
  font-size: 20px;
  color: var(--el-color-primary);
}

.subtitle {
  margin: 4px 0 16px;
  color: var(--el-text-color-secondary);
}

.submit {
  width: 100%;
  margin-top: 12px;
}

.back {
  margin: 16px 0 0;
  text-align: center;
  font-size: 13px;
}
</style>
