<template>
  <div class="login-wrap">
    <div class="login-card">
      <h1>Video2Api</h1>
      <p>默认账号密码：Admin / Admin</p>
      <el-form @submit.prevent>
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" style="width: 100%" @click="doLogin">登录</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '../api'

const router = useRouter()
const username = ref('Admin')
const password = ref('Admin')
const loading = ref(false)

const doLogin = async () => {
  loading.value = true
  try {
    const data = await login(username.value, password.value)
    localStorage.setItem('token', data.access_token)
    localStorage.setItem('user', JSON.stringify(data.user || {}))
    ElMessage.success('登录成功')
    router.push('/sora-accounts')
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  width: 100%;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(820px 420px at 10% -2%, rgba(59, 130, 246, 0.24), transparent 68%),
    radial-gradient(860px 420px at 94% 8%, rgba(20, 184, 166, 0.24), transparent 64%),
    linear-gradient(150deg, #edf7ff 0%, #eef9f7 48%, #f6f9ff 100%);
}

.login-card {
  width: 360px;
  background: linear-gradient(140deg, rgba(255, 255, 255, 0.58) 0%, rgba(255, 255, 255, 0.3) 100%);
  border: 1px solid rgba(255, 255, 255, 0.58);
  border-radius: 16px;
  box-shadow: 0 14px 36px rgba(15, 23, 42, 0.12);
  padding: 24px;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
}

h1 {
  margin: 0;
  font-size: 26px;
  color: #0f172a;
}

p {
  margin: 8px 0 18px;
  color: #64748b;
  font-size: 12px;
}
</style>
