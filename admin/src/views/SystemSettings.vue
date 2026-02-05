<template>
  <div class="settings-page">
    <el-alert
      v-if="!apiReady"
      class="tip"
      title="系统设置接口暂未接入，当前为本地预览模式（保存到浏览器本地）。"
      type="warning"
      :closable="false"
      show-icon
    />

    <el-row :gutter="12">
      <el-col :span="14">
        <el-card class="glass-card" v-loading="loading">
          <template #header>
            <div class="card-title">系统参数（保存后立即生效）</div>
          </template>
          <el-form :model="systemForm" label-width="170px">
            <el-form-item label="ixBrowser API Base">
              <el-input v-model="systemForm.ixbrowser_api_base" />
            </el-form-item>
            <el-form-item label="请求超时（ms）">
              <el-input-number v-model="systemForm.request_timeout_ms" :min="1000" :max="120000" />
            </el-form-item>
            <el-form-item label="任务轮询间隔（秒）">
              <el-input-number v-model="systemForm.generate_poll_interval_sec" :min="3" :max="60" />
            </el-form-item>
            <el-form-item label="任务最大等待（分钟）">
              <el-input-number v-model="systemForm.generate_max_minutes" :min="1" :max="60" />
            </el-form-item>
            <el-form-item label="审计日志保留">
              <el-tag size="small" type="success">30 天</el-tag>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card class="glass-card" v-loading="loading">
          <template #header>
            <div class="card-title">定时扫描（每日固定时刻）</div>
          </template>
          <el-form :model="schedulerForm" label-width="120px">
            <el-form-item label="启用定时">
              <el-switch v-model="schedulerForm.enabled" />
            </el-form-item>
            <el-form-item label="执行时刻">
              <el-input
                v-model="schedulerForm.times"
                placeholder="例如：09:00,13:30,21:10"
              />
              <div class="inline-tip">24 小时制，多个时刻用英文逗号分隔。</div>
            </el-form-item>
            <el-form-item label="时区">
              <el-input v-model="schedulerForm.timezone" />
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="glass-card save-card">
      <div class="save-row">
        <span class="save-desc">配置修改后立即生效</span>
        <div class="save-actions">
          <el-button @click="loadAll">重置</el-button>
          <el-button type="primary" :loading="saving" @click="saveAll">保存设置</el-button>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getScanSchedulerConfig, getSystemSettings, updateScanSchedulerConfig, updateSystemSettings } from '../api'

const loading = ref(false)
const saving = ref(false)
const apiReady = ref(true)

const defaultSystemForm = {
  ixbrowser_api_base: 'http://127.0.0.1:53200',
  request_timeout_ms: 30000,
  generate_poll_interval_sec: 6,
  generate_max_minutes: 12
}

const defaultSchedulerForm = {
  enabled: false,
  times: '09:00,13:30,21:00',
  timezone: 'Asia/Shanghai'
}

const systemForm = ref({ ...defaultSystemForm })
const schedulerForm = ref({ ...defaultSchedulerForm })

const loadFromLocal = () => {
  try {
    const systemRaw = localStorage.getItem('admin_system_settings')
    const schedulerRaw = localStorage.getItem('admin_scheduler_settings')
    systemForm.value = systemRaw ? { ...defaultSystemForm, ...JSON.parse(systemRaw) } : { ...defaultSystemForm }
    schedulerForm.value = schedulerRaw ? { ...defaultSchedulerForm, ...JSON.parse(schedulerRaw) } : { ...defaultSchedulerForm }
  } catch {
    systemForm.value = { ...defaultSystemForm }
    schedulerForm.value = { ...defaultSchedulerForm }
  }
}

const loadAll = async () => {
  loading.value = true
  try {
    const [systemData, schedulerData] = await Promise.all([
      getSystemSettings(),
      getScanSchedulerConfig()
    ])
    systemForm.value = { ...defaultSystemForm, ...(systemData || {}) }
    schedulerForm.value = { ...defaultSchedulerForm, ...(schedulerData || {}) }
    apiReady.value = true
  } catch (error) {
    if (error?.response?.status === 404) {
      apiReady.value = false
      loadFromLocal()
      return
    }
    ElMessage.error(error?.response?.data?.detail || '读取系统设置失败')
  } finally {
    loading.value = false
  }
}

const validateTimes = (value) => {
  const times = String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  if (times.length === 0) {
    return false
  }
  const pattern = /^([01]\d|2[0-3]):[0-5]\d$/
  return times.every((item) => pattern.test(item))
}

const saveAll = async () => {
  if (!validateTimes(schedulerForm.value.times)) {
    ElMessage.warning('执行时刻格式不正确，请输入 HH:mm 并用逗号分隔')
    return
  }
  saving.value = true
  try {
    if (apiReady.value) {
      await Promise.all([
        updateSystemSettings(systemForm.value),
        updateScanSchedulerConfig(schedulerForm.value)
      ])
    } else {
      localStorage.setItem('admin_system_settings', JSON.stringify(systemForm.value))
      localStorage.setItem('admin_scheduler_settings', JSON.stringify(schedulerForm.value))
    }
    ElMessage.success('系统设置已保存并生效')
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await loadAll()
})
</script>

<style scoped>
.settings-page {
  padding: 2px;
}

.tip {
  margin-bottom: 12px;
}

.glass-card {
  border-radius: 16px;
  border: 1px solid rgba(255, 255, 255, 0.52);
  background: linear-gradient(140deg, rgba(255, 255, 255, 0.58) 0%, rgba(255, 255, 255, 0.28) 100%);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
}

.card-title {
  font-weight: 700;
  color: #0f172a;
}

.inline-tip {
  margin-top: 6px;
  font-size: 12px;
  color: #475569;
}

.save-card {
  margin-top: 12px;
}

.save-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.save-desc {
  color: #0f172a;
  font-weight: 600;
}

.save-actions {
  display: flex;
  gap: 10px;
}
</style>
