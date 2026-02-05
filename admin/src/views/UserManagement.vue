<template>
  <div class="users-page">
    <el-card class="glass-card head-card">
      <div class="head-row">
        <div>
          <h3>后台用户管理</h3>
          <p>当前权限模型：单角色 `admin`，仅内置 root 账号（Admin）可管理用户。</p>
        </div>
        <div class="actions">
          <el-tag :type="isRoot ? 'success' : 'warning'">{{ isRoot ? 'root 可操作' : '非 root 只读' }}</el-tag>
          <el-button type="primary" :disabled="!canWrite" @click="openCreateDialog">新增用户</el-button>
          <el-button @click="loadUsers">刷新</el-button>
        </div>
      </div>
    </el-card>

    <el-alert
      v-if="!apiReady"
      class="tip"
      title="用户管理接口暂未接入，当前页面为骨架展示。接入后可直接生效。"
      type="info"
      :closable="false"
      show-icon
    />

    <el-card class="glass-card table-card" v-loading="loading">
      <el-table :data="users" border stripe height="560">
        <el-table-column prop="id" label="ID" width="76" />
        <el-table-column prop="username" label="用户名" min-width="150" />
        <el-table-column prop="role" label="角色" width="96" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'danger'" size="small">
              {{ row.is_active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="last_login_at" label="最近登录" width="170">
          <template #default="{ row }">{{ formatTime(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="updated_at" label="更新时间" width="170">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" fixed="right" width="220">
          <template #default="{ row }">
            <el-button
              size="small"
              :disabled="!canWrite || isCurrentUser(row)"
              @click="toggleUserStatus(row)"
            >
              {{ row.is_active ? '停用' : '启用' }}
            </el-button>
            <el-button
              size="small"
              type="warning"
              :disabled="!canWrite"
              @click="openResetDialog(row)"
            >
              重置密码
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="createDialogVisible" title="新增后台用户" width="520px">
      <el-form :model="createForm" label-width="100px">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" maxlength="32" placeholder="字母数字下划线，2-32 位" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="createForm.password" type="password" show-password maxlength="64" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitCreateUser">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resetDialogVisible" title="重置密码" width="520px">
      <el-form :model="resetForm" label-width="100px">
        <el-form-item label="用户名">
          <el-input v-model="resetForm.username" disabled />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="resetForm.password" type="password" show-password maxlength="64" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitResetPassword">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createAdminUser, listAdminUsers, resetAdminUserPassword, updateAdminUser } from '../api'

const loading = ref(false)
const submitting = ref(false)
const apiReady = ref(true)
const users = ref([])
const createDialogVisible = ref(false)
const resetDialogVisible = ref(false)
const resetUserId = ref(null)

const createForm = ref({
  username: '',
  password: ''
})

const resetForm = ref({
  username: '',
  password: ''
})

const currentUser = computed(() => {
  try {
    return JSON.parse(localStorage.getItem('user') || '{}')
  } catch {
    return {}
  }
})

const isRoot = computed(() => String(currentUser.value?.username || '').toLowerCase() === 'admin')
const canWrite = computed(() => isRoot.value && apiReady.value)

const formatTime = (value) => {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return String(value)
  }
}

const isCurrentUser = (row) => Number(row.id) === Number(currentUser.value?.id)

const fallbackUsers = () => {
  const now = new Date().toISOString()
  users.value = [
    {
      id: currentUser.value?.id || 1,
      username: currentUser.value?.username || 'Admin',
      role: 'admin',
      is_active: true,
      last_login_at: now,
      created_at: now,
      updated_at: now
    }
  ]
}

const loadUsers = async () => {
  loading.value = true
  try {
    const data = await listAdminUsers()
    users.value = Array.isArray(data) ? data : []
    apiReady.value = true
  } catch (error) {
    if (error?.response?.status === 404) {
      apiReady.value = false
      fallbackUsers()
      return
    }
    ElMessage.error(error?.response?.data?.detail || '读取用户失败')
  } finally {
    loading.value = false
  }
}

const openCreateDialog = () => {
  createForm.value = {
    username: '',
    password: ''
  }
  createDialogVisible.value = true
}

const submitCreateUser = async () => {
  const username = createForm.value.username?.trim()
  const password = createForm.value.password?.trim()
  if (!username || username.length < 2) {
    ElMessage.warning('用户名至少 2 位')
    return
  }
  if (!password) {
    ElMessage.warning('请输入初始密码')
    return
  }
  submitting.value = true
  try {
    await createAdminUser({ username, password, role: 'admin' })
    ElMessage.success('用户已创建')
    createDialogVisible.value = false
    await loadUsers()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '创建失败')
  } finally {
    submitting.value = false
  }
}

const openResetDialog = (row) => {
  resetUserId.value = row.id
  resetForm.value = {
    username: row.username,
    password: ''
  }
  resetDialogVisible.value = true
}

const submitResetPassword = async () => {
  if (!resetUserId.value) return
  const password = resetForm.value.password?.trim()
  if (!password) {
    ElMessage.warning('请输入新密码')
    return
  }
  submitting.value = true
  try {
    await resetAdminUserPassword(resetUserId.value, { password })
    ElMessage.success('密码已重置')
    resetDialogVisible.value = false
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '重置失败')
  } finally {
    submitting.value = false
  }
}

const toggleUserStatus = async (row) => {
  try {
    await updateAdminUser(row.id, { is_active: !row.is_active })
    ElMessage.success(`已${row.is_active ? '停用' : '启用'}用户`)
    await loadUsers()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '更新状态失败')
  }
}

onMounted(async () => {
  await loadUsers()
})
</script>

<style scoped>
.users-page {
  padding: 2px;
}

.glass-card {
  border-radius: 16px;
  border: 1px solid rgba(255, 255, 255, 0.52);
  background: linear-gradient(140deg, rgba(255, 255, 255, 0.58) 0%, rgba(255, 255, 255, 0.28) 100%);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
}

.head-card {
  margin-bottom: 12px;
}

.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.head-row h3 {
  margin: 0;
  color: #0f172a;
}

.head-row p {
  margin: 6px 0 0;
  color: #475569;
  font-size: 13px;
}

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.tip {
  margin-bottom: 12px;
}
</style>
