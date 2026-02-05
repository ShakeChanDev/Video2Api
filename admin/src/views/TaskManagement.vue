<template>
  <div class="tasks-page">
    <el-card class="glass-card filter-card" v-loading="loading">
      <div class="filter-row">
        <el-select v-model="selectedGroupTitle" class="w-180" @change="loadJobs">
          <el-option
            v-for="group in groups"
            :key="group.id"
            :label="`${group.title} (ID:${group.id})`"
            :value="group.title"
          />
        </el-select>

        <el-select v-model="taskType" class="w-140">
          <el-option label="全部类型" value="all" />
          <el-option label="视频任务" value="video" />
          <el-option label="图片任务（预留）" value="image" />
        </el-select>

        <el-select v-model="statusFilter" class="w-140">
          <el-option label="全部状态" value="all" />
          <el-option label="排队中" value="queued" />
          <el-option label="运行中" value="running" />
          <el-option label="成功" value="completed" />
          <el-option label="失败" value="failed" />
        </el-select>

        <el-input v-model="keyword" class="w-260" clearable placeholder="搜索 Job/任务ID/Prompt/窗口ID" />

        <el-button @click="loadJobs">刷新</el-button>
        <el-button type="primary" @click="openCreateDialog">新建任务</el-button>
      </div>
    </el-card>

    <el-card class="glass-card table-card" v-loading="loading">
      <template #header>
        <div class="table-head">
          <span>任务列表（统一入口，图片已预留）</span>
          <el-tag type="info" size="small">自动重试策略：提交失败最多重试 1 次</el-tag>
        </div>
      </template>

      <el-table :data="filteredJobs" border stripe height="560">
        <el-table-column prop="job_id" label="Job" width="86" />
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.task_type === 'video' ? 'success' : 'info'">
              {{ row.task_type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="group_title" label="分组" width="120" />
        <el-table-column prop="profile_id" label="窗口" width="86" />
        <el-table-column prop="duration" label="时长" width="80" />
        <el-table-column prop="aspect_ratio" label="比例" width="95" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="160">
          <template #default="{ row }">
            <el-progress
              :percentage="row.progress"
              :status="progressStatus(row)"
              :stroke-width="10"
              :text-inside="true"
            />
          </template>
        </el-table-column>
        <el-table-column label="发布状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="publishStatusType(row.publish_status)">
              {{ row.publish_status || 'queued' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="submit_attempts" label="提交重试" width="96" />
        <el-table-column prop="poll_attempts" label="轮询次数" width="96" />
        <el-table-column prop="task_id" label="任务ID" min-width="150" />
        <el-table-column label="发布链接" min-width="160">
          <template #default="{ row }">
            <a v-if="row.publish_url" :href="row.publish_url" target="_blank" class="task-link">打开</a>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="任务链接" width="100">
          <template #default="{ row }">
            <a v-if="row.task_url" :href="row.task_url" target="_blank" class="task-link">打开</a>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="error" label="错误" min-width="180">
          <template #default="{ row }">{{ row.error || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" fixed="right" width="220">
          <template #default="{ row }">
            <el-button size="small" @click="openDetail(row)">详情</el-button>
            <el-button
              size="small"
              type="warning"
              :disabled="row.task_type !== 'video' || row.status !== 'failed'"
              @click="retryJob(row)"
            >
              重试
            </el-button>
            <el-button
              size="small"
              type="primary"
              :disabled="row.status !== 'completed' || (row.publish_status === 'completed' && row.publish_url)"
              @click="retryPublish(row)"
            >
              发布重试
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="createDialogVisible" title="新建任务" width="620px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="任务类型">
          <el-select v-model="createForm.task_type" style="width: 100%">
            <el-option label="视频任务" value="video" />
            <el-option label="图片任务（预留）" value="image" />
          </el-select>
        </el-form-item>
        <el-form-item label="分组">
          <el-select v-model="createForm.group_title" style="width: 100%">
            <el-option
              v-for="group in groups"
              :key="group.id"
              :label="`${group.title} (ID:${group.id})`"
              :value="group.title"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="窗口ID">
          <el-input-number v-model="createForm.profile_id" style="width: 100%" :min="1" />
        </el-form-item>
        <el-form-item label="时长">
          <el-select v-model="createForm.duration" style="width: 100%">
            <el-option label="10秒" value="10s" />
            <el-option label="15秒" value="15s" />
            <el-option label="25秒" value="25s" />
          </el-select>
        </el-form-item>
        <el-form-item label="比例">
          <el-select v-model="createForm.aspect_ratio" style="width: 100%">
            <el-option label="横屏 landscape" value="landscape" />
            <el-option label="竖屏 portrait" value="portrait" />
          </el-select>
        </el-form-item>
        <el-form-item label="提示词">
          <el-input
            v-model="createForm.prompt"
            type="textarea"
            :rows="4"
            placeholder="请输入提示词"
            maxlength="4000"
            show-word-limit
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitTask">创建</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailDrawerVisible" title="任务详情" size="620px" direction="rtl">
      <pre class="detail-json">{{ detailText }}</pre>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createIxBrowserSoraGenerateJob,
  getIxBrowserGroupWindows,
  listIxBrowserSoraGenerateJobs,
  publishIxBrowserSoraGenerateJob
} from '../api'

const loading = ref(false)
const submitting = ref(false)
const createDialogVisible = ref(false)
const detailDrawerVisible = ref(false)
const detailText = ref('')
const groups = ref([])
const jobs = ref([])
const selectedGroupTitle = ref('Sora')
const taskType = ref('all')
const statusFilter = ref('all')
const keyword = ref('')
let pollingTimer = null

const createForm = ref({
  task_type: 'video',
  group_title: 'Sora',
  profile_id: null,
  prompt: '',
  duration: '10s',
  aspect_ratio: 'landscape'
})

const filteredJobs = computed(() => {
  return jobs.value.filter((job) => {
    const byType = taskType.value === 'all' || job.task_type === taskType.value
    const byStatus = statusFilter.value === 'all' || job.status === statusFilter.value
    const q = keyword.value.trim().toLowerCase()
    if (!q) {
      return byType && byStatus
    }
    const joined = [
      job.job_id,
      job.profile_id,
      job.task_id || '',
      job.prompt || '',
      job.error || ''
    ]
      .join(' ')
      .toLowerCase()
    return byType && byStatus && joined.includes(q)
  })
})

const formatTime = (value) => {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return String(value)
  }
}

const statusType = (status) => {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'running') return 'warning'
  return 'info'
}

const publishStatusType = (status) => {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'running') return 'warning'
  return 'info'
}

const progressStatus = (row) => {
  if (row.status === 'completed') return 'success'
  if (row.status === 'failed') return 'exception'
  return ''
}

const hasRunningJobs = computed(() => jobs.value.some((item) => item.status === 'queued' || item.status === 'running'))

const stopPolling = () => {
  if (pollingTimer) {
    clearInterval(pollingTimer)
    pollingTimer = null
  }
}

const startPollingIfNeeded = () => {
  if (!hasRunningJobs.value) {
    stopPolling()
    return
  }
  if (pollingTimer) {
    return
  }
  pollingTimer = setInterval(() => {
    loadJobs(false)
  }, 8000)
}

const normalizeProgress = (item) => {
  const raw = item?.progress
  if (typeof raw === 'number' && !Number.isNaN(raw)) {
    return Math.min(100, Math.max(0, Math.round(raw)))
  }
  if (item?.status === 'completed') return 100
  return 0
}

const normalizeJobs = (data) => {
  return (Array.isArray(data) ? data : []).map((item) => ({
    ...item,
    progress: normalizeProgress(item),
    publish_status: item?.publish_status || 'queued',
    task_type: 'video'
  }))
}

const loadGroups = async () => {
  try {
    const data = await getIxBrowserGroupWindows()
    groups.value = Array.isArray(data) ? data : []
    const exists = groups.value.some((group) => group.title === selectedGroupTitle.value)
    if (!exists && groups.value.length > 0) {
      selectedGroupTitle.value = groups.value[0].title
    }
    createForm.value.group_title = selectedGroupTitle.value
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '获取分组失败')
  }
}

const loadJobs = async (withLoading = true) => {
  if (withLoading) {
    loading.value = true
  }
  try {
    const data = await listIxBrowserSoraGenerateJobs({
      group_title: selectedGroupTitle.value || 'Sora',
      limit: 100
    })
    jobs.value = normalizeJobs(data)
    startPollingIfNeeded()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '获取任务失败')
    stopPolling()
  } finally {
    if (withLoading) {
      loading.value = false
    }
  }
}

const openCreateDialog = () => {
  createForm.value = {
    task_type: 'video',
    group_title: selectedGroupTitle.value || 'Sora',
    profile_id: null,
    prompt: '',
    duration: '10s',
    aspect_ratio: 'landscape'
  }
  createDialogVisible.value = true
}

const submitTask = async () => {
  if (!createForm.value.profile_id) {
    ElMessage.warning('请输入窗口 ID')
    return
  }
  if (createForm.value.task_type === 'image') {
    ElMessage.info('图片任务创建接口预留中，当前请先使用视频任务')
    return
  }
  const prompt = createForm.value.prompt?.trim()
  if (!prompt) {
    ElMessage.warning('请输入提示词')
    return
  }
  submitting.value = true
  try {
    await createIxBrowserSoraGenerateJob({
      profile_id: createForm.value.profile_id,
      prompt,
      duration: createForm.value.duration,
      aspect_ratio: createForm.value.aspect_ratio
    })
    ElMessage.success('任务已创建')
    createDialogVisible.value = false
    await loadJobs()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '创建任务失败')
  } finally {
    submitting.value = false
  }
}

const retryJob = async (row) => {
  if (!row.prompt || !row.profile_id) {
    ElMessage.warning('缺少重试参数，无法发起重试')
    return
  }
  submitting.value = true
  try {
    await createIxBrowserSoraGenerateJob({
      profile_id: row.profile_id,
      prompt: row.prompt,
      duration: row.duration || '10s',
      aspect_ratio: row.aspect_ratio || 'landscape'
    })
    ElMessage.success(`Job #${row.job_id} 已发起重试`)
    await loadJobs()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '重试失败')
  } finally {
    submitting.value = false
  }
}

const retryPublish = async (row) => {
  if (!row?.job_id) {
    ElMessage.warning('缺少任务 ID')
    return
  }
  submitting.value = true
  try {
    await publishIxBrowserSoraGenerateJob(row.job_id)
    ElMessage.success(`Job #${row.job_id} 已触发发布`)
    await loadJobs()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '发布失败')
  } finally {
    submitting.value = false
  }
}

const openDetail = (row) => {
  detailText.value = JSON.stringify(row, null, 2)
  detailDrawerVisible.value = true
}

onMounted(async () => {
  await loadGroups()
  await loadJobs()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<style scoped>
.tasks-page {
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

.filter-card {
  margin-bottom: 12px;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.w-140 {
  width: 140px;
}

.w-180 {
  width: 180px;
}

.w-260 {
  width: 260px;
}

.table-card :deep(.el-card__body) {
  padding-top: 8px;
}

.table-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.task-link {
  color: #0b5ad8;
  text-decoration: none;
}

.task-link:hover {
  text-decoration: underline;
}

.detail-json {
  margin: 0;
  padding: 12px;
  border-radius: 10px;
  background: #0f172a;
  color: #e2e8f0;
  max-height: calc(100vh - 170px);
  overflow: auto;
}

.table-card :deep(.el-progress__text) {
  font-size: 11px;
}
</style>
