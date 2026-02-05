<template>
  <div class="ix-page">
    <section class="command-bar">
      <div class="command-left">
        <div class="command-title">Sora 账号管理</div>
        <div class="command-meta">
          <div class="meta-item">
            <span class="meta-label">分组</span>
            <el-select v-model="selectedGroupTitle" size="large" class="group-select" @change="onGroupChange">
              <el-option
                v-for="group in groups"
                :key="group.id"
                :label="`${group.title} (ID:${group.id})`"
                :value="group.title"
              />
            </el-select>
          </div>
          <div class="meta-info">
            <span>Run</span>
            <strong>{{ currentRunId }}</strong>
          </div>
          <div class="meta-info">
            <span>扫描时间</span>
            <strong>{{ lastScannedAt }}</strong>
          </div>
          <div class="meta-info" v-if="selectedGroup">
            <span>窗口数</span>
            <strong>{{ selectedGroup.window_count || 0 }}</strong>
          </div>
        </div>
      </div>
      <div class="command-right">
        <el-tag size="large" :type="statusTagType">{{ statusText }}</el-tag>
        <el-button size="large" @click="refreshAll" :loading="latestLoading">刷新</el-button>
        <el-button size="large" type="warning" :loading="scanLoading" @click="scanNow">
          扫描账号与次数
        </el-button>
      </div>
    </section>

    <section class="metrics-grid">
      <article class="metric-card">
        <span class="metric-label">窗口总数</span>
        <strong class="metric-value">{{ metrics.total }}</strong>
      </article>
      <article class="metric-card success">
        <span class="metric-label">本次成功</span>
        <strong class="metric-value">{{ metrics.success }}</strong>
      </article>
      <article class="metric-card danger">
        <span class="metric-label">本次失败</span>
        <strong class="metric-value">{{ metrics.failed }}</strong>
      </article>
      <article class="metric-card accent">
        <span class="metric-label">总可用次数</span>
        <strong class="metric-value">{{ metrics.available }}</strong>
      </article>
      <article class="metric-card highlight">
        <span class="metric-label">预估可用视频条数</span>
        <strong class="metric-value">{{ metrics.estimatedVideos }}</strong>
      </article>
    </section>

    <section class="result-panel" v-loading="latestLoading || scanLoading">
      <div class="panel-header">
        <div>
          <div class="panel-title">窗口扫描结果</div>
          <div class="panel-subtitle">展示当前分组的最新扫描数据</div>
        </div>
        <div class="panel-actions" />
      </div>

      <el-table
        v-if="scanRows.length"
        :data="scanRows"
        border
        stripe
        height="560"
        class="scan-table"
        :row-class-name="getRowClass"
        @row-click="viewSession"
      >
        <el-table-column prop="profile_id" label="窗口ID" width="90" />
        <el-table-column prop="window_name" label="窗口名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="account" label="账号" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.account || '-' }}</template>
        </el-table-column>
        <el-table-column prop="quota_remaining_count" label="可用次数" width="100">
          <template #default="{ row }">{{ row.quota_remaining_count ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="数据来源" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.fallback_applied ? 'warning' : 'success'">
              {{ row.fallback_applied ? '回填' : '本次' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="session_status" label="Session" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.session_status === 200 ? 'success' : 'info'">
              {{ row.session_status ?? '-' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" label="耗时(ms)" width="100" />
        <el-table-column label="错误" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.error || row.quota_error || '-' }}</template>
        </el-table-column>
        <el-table-column label="详情" fixed="right" width="86">
          <template #default="{ row }">
            <el-button size="small" @click.stop="viewSession(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无扫描结果" :image-size="90">
        <el-button type="primary" :loading="scanLoading" @click="scanNow">立即扫描</el-button>
      </el-empty>
    </section>

    <el-dialog v-model="sessionDialogVisible" title="Session / Quota 详情" width="900px">
      <pre class="session-preview">{{ currentSessionText }}</pre>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getIxBrowserGroupWindows,
  getLatestIxBrowserSoraSessionAccounts,
  scanIxBrowserSoraSessionAccounts
} from '../api'

const latestLoading = ref(false)
const scanLoading = ref(false)

const groups = ref([])
const selectedGroupTitle = ref('Sora')
const scanData = ref(null)

const sessionDialogVisible = ref(false)
const currentSessionText = ref('')

const scanRows = computed(() => scanData.value?.results || [])
const selectedGroup = computed(() => groups.value.find((g) => g.title === selectedGroupTitle.value) || null)

const metrics = computed(() => {
  const rows = scanRows.value
  const available = rows.reduce((sum, row) => {
    const count = row?.quota_remaining_count
    if (typeof count === 'number' && !Number.isNaN(count)) {
      return sum + count
    }
    return sum
  }, 0)
  return {
    total: scanData.value?.total_windows || 0,
    success: scanData.value?.success_count || 0,
    failed: scanData.value?.failed_count || 0,
    available,
    estimatedVideos: Math.floor(available / 2)
  }
})

const currentRunId = computed(() => scanData.value?.run_id || '-')
const lastScannedAt = computed(() => formatTime(scanData.value?.scanned_at))

const statusText = computed(() => {
  if (scanLoading.value) return '扫描中'
  if (!scanData.value) return '暂无数据'
  if (scanData.value.failed_count > 0) return '有失败'
  return '正常'
})

const statusTagType = computed(() => {
  if (scanLoading.value) return 'warning'
  if (!scanData.value) return 'info'
  if (scanData.value.failed_count > 0) return 'danger'
  return 'success'
})

const formatTime = (value) => {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return String(value)
  }
}

const loadGroups = async () => {
  try {
    const data = await getIxBrowserGroupWindows()
    groups.value = Array.isArray(data) ? data : []
    const sora = groups.value.find((g) => g.title === 'Sora')
    if (sora) {
      selectedGroupTitle.value = sora.title
    } else if (!groups.value.some((g) => g.title === selectedGroupTitle.value) && groups.value.length > 0) {
      selectedGroupTitle.value = groups.value[0].title
    }
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '获取分组失败')
  } finally {
  }
}

const loadLatest = async () => {
  if (!selectedGroupTitle.value) return
  latestLoading.value = true
  try {
    const data = await getLatestIxBrowserSoraSessionAccounts(selectedGroupTitle.value, true)
    scanData.value = data
  } catch (error) {
    if (error?.response?.status === 404) {
      scanData.value = null
      return
    }
    ElMessage.error(error?.response?.data?.detail || '获取最新结果失败')
  } finally {
    latestLoading.value = false
  }
}

const refreshAll = async () => {
  await loadLatest()
}

const scanNow = async () => {
  if (!selectedGroupTitle.value) {
    ElMessage.warning('请先选择分组')
    return
  }
  scanLoading.value = true
  try {
    const data = await scanIxBrowserSoraSessionAccounts(selectedGroupTitle.value)
    scanData.value = data
    ElMessage.success('扫描完成，结果已入库')
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '扫描失败')
  } finally {
    scanLoading.value = false
  }
}

const onGroupChange = async () => {
  await loadLatest()
}

const viewSession = (row) => {
  const payload = {
    account: row.account || null,
    session_status: row.session_status || null,
    fallback_applied: row.fallback_applied || false,
    fallback_run_id: row.fallback_run_id || null,
    fallback_scanned_at: row.fallback_scanned_at || null,
    session: row.session ?? row.session_raw ?? null,
    quota: {
      remaining_count: row.quota_remaining_count ?? null,
      total_count: row.quota_total_count ?? null,
      reset_at: row.quota_reset_at ?? null,
      source: row.quota_source ?? null,
      payload: row.quota_payload ?? null,
      error: row.quota_error ?? null
    }
  }
  currentSessionText.value = JSON.stringify(payload, null, 2)
  sessionDialogVisible.value = true
}

const getRowClass = ({ row }) => {
  if (row?.success === false) return 'row-failed'
  if (row?.fallback_applied) return 'row-fallback'
  if (row?.success === true) return 'row-success'
  return ''
}

onMounted(async () => {
  await loadGroups()
  await loadLatest()
})
</script>

<style scoped>
.ix-page {
  padding: 6px;
  min-height: calc(100vh - 80px);
  background: transparent;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.command-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background: linear-gradient(135deg, rgba(7, 89, 133, 0.88) 0%, rgba(17, 94, 89, 0.82) 52%, rgba(3, 105, 161, 0.8) 100%);
  color: #f8fafc;
  border-radius: 18px;
  padding: 18px 20px;
  border: 1px solid rgba(255, 255, 255, 0.35);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: 0 12px 28px rgba(15, 23, 42, 0.16);
}

.command-title {
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 8px;
}

.command-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.meta-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.meta-label {
  font-size: 12px;
  color: #cbd5e1;
}

.meta-info {
  font-size: 12px;
  color: #e2e8f0;
  display: flex;
  align-items: center;
  gap: 6px;
}

.meta-info strong {
  font-size: 13px;
  color: #f8fafc;
}

.group-select {
  width: 220px;
}

.command-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}

.metric-card {
  background: linear-gradient(140deg, rgba(255, 255, 255, 0.66) 0%, rgba(255, 255, 255, 0.34) 100%);
  border: 1px solid rgba(255, 255, 255, 0.58);
  border-radius: 12px;
  padding: 12px 14px;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.08);
}

.metric-card.success {
  border-color: #bbf7d0;
}

.metric-card.danger {
  border-color: #fecaca;
}

.metric-card.warning {
  border-color: #fde68a;
}

.metric-card.accent {
  border-color: #bae6fd;
}

.metric-card.highlight {
  border-color: #f5d0fe;
}

.metric-label {
  display: block;
  font-size: 12px;
  color: #64748b;
}

.metric-value {
  display: block;
  margin-top: 6px;
  font-size: 28px;
  line-height: 1;
  color: #0f172a;
}

.result-panel {
  background: linear-gradient(140deg, rgba(255, 255, 255, 0.66) 0%, rgba(255, 255, 255, 0.34) 100%);
  border: 1px solid rgba(255, 255, 255, 0.58);
  border-radius: 14px;
  padding: 14px;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.panel-title {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.panel-subtitle {
  font-size: 12px;
  color: #64748b;
  margin-top: 4px;
}

.scan-table :deep(.el-table__cell) {
  font-size: 12px;
}

.scan-table :deep(.el-table__header-wrapper th) {
  background: rgba(248, 250, 252, 0.9);
  color: #0f172a;
  font-weight: 600;
}

.scan-table :deep(.el-table__row) {
  transition: background 0.15s ease;
  cursor: pointer;
}

.scan-table :deep(.el-table__row:hover) {
  background: rgba(14, 165, 233, 0.08);
}

.scan-table :deep(.el-table__row.row-failed) {
  background: rgba(248, 113, 113, 0.08);
}

.scan-table :deep(.el-table__row.row-failed:hover) {
  background: rgba(248, 113, 113, 0.14);
}

.scan-table :deep(.el-table__row.row-fallback) {
  background: rgba(250, 204, 21, 0.08);
}

.scan-table :deep(.el-table__row.row-success) {
  background: rgba(34, 197, 94, 0.06);
}

.session-preview {
  margin: 0;
  max-height: 520px;
  overflow: auto;
  background: #0f172a;
  color: #e2e8f0;
  padding: 12px;
  border-radius: 8px;
}

@media (max-width: 1360px) {
  .command-bar {
    flex-direction: column;
    align-items: flex-start;
  }

  .command-right {
    width: 100%;
    flex-wrap: wrap;
  }
}
</style>
