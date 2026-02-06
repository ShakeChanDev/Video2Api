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
            <span>最新批次</span>
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

    <el-card class="table-card" v-loading="latestLoading || scanLoading">
      <template #header>
        <div class="table-head stack">
          <span>窗口扫描结果</span>
          <span class="table-hint">实时使用与最近扫描会自动汇总到这里</span>
        </div>
      </template>

      <el-table
        v-if="scanRows.length"
        ref="scanTableRef"
        :data="scanRows"
        class="scan-table card-table"
        :row-class-name="getRowClass"
        row-key="profile_id"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="46" align="center" reserve-selection />
        <el-table-column label="窗口" min-width="360">
          <template #default="{ row }">
            <div class="window-card">
              <div class="window-title">
                <span class="window-name">{{ row.window_name || '-' }}</span>
                <span class="window-id">ID {{ row.profile_id }}</span>
              </div>
              <div class="window-account">{{ row.account || '未识别账号' }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="可用次数" width="170" align="center">
          <template #default="{ row }">
            <div class="quota-card">
              <div class="quota-primary">
                <span class="quota-primary-value">{{ estimateVideos(row) }}</span>
                <span v-if="estimateVideos(row) !== '-'" class="quota-primary-unit">条</span>
              </div>
              <div class="quota-secondary">{{ getQuotaRemainingText(row) }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="套餐" width="118" align="center">
          <template #default="{ row }">
            <span v-if="isPlusPlan(row)" class="plan-badge">Plus</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="150" align="center">
          <template #default="{ row }">
            <span class="updated-relative">{{ formatUpdatedTime(row.scanned_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="来源 / 状态" width="188">
          <template #default="{ row }">
            <div class="status-stack">
              <el-tag size="small" effect="light" class="source-tag" :class="getSourceClass(row)">
                {{ getSourceLabel(row) }}
              </el-tag>
              <el-tag size="small" :type="row.session_status === 200 ? 'success' : 'info'">
                {{ row.session_status ?? '-' }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" fixed="right" width="210" align="center" class-name="detail-column" label-class-name="detail-column-header">
          <template #default="{ row }">
            <div class="action-buttons">
              <el-button
                size="small"
                class="btn-soft"
                :loading="isOpeningProfile(row.profile_id)"
                @click.stop="openWindow(row)"
              >
                打开窗口
              </el-button>
              <el-button size="small" class="btn-soft" @click.stop="viewSession(row)">查看</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无扫描结果" :image-size="90">
        <el-button type="primary" :loading="scanLoading" @click="scanNow">立即扫描</el-button>
      </el-empty>
    </el-card>

    <el-dialog v-model="sessionDialogVisible" title="Session / Quota 详情" width="900px">
      <pre class="session-preview">{{ currentSessionText }}</pre>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { formatRelativeTimeZh } from '../utils/relativeTime'
import {
  getIxBrowserGroupWindows,
  getLatestIxBrowserSoraSessionAccounts,
  openIxBrowserProfileWindow,
  scanIxBrowserSoraSessionAccounts,
  getSystemSettings
} from '../api'

const latestLoading = ref(false)
const scanLoading = ref(false)
const realtimeStatus = ref('disconnected')
let realtimeSource = null
let relativeTimeTimer = null
const cachePrefix = 'sora_accounts_cache_'
const nowTick = ref(Date.now())

const groups = ref([])
const selectedGroupTitle = ref('Sora')
const scanData = ref(null)
const systemSettings = ref(null)

const sessionDialogVisible = ref(false)
const currentSessionText = ref('')
const openingProfileIds = ref({})
const scanTableRef = ref(null)
const selectedProfileIds = ref([])

const parseScanTime = (value) => {
  if (!value) return 0
  const raw = String(value).trim()
  if (!raw) return 0
  let ms = Date.parse(raw)
  if (!Number.isNaN(ms)) return ms
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(raw)) {
    ms = Date.parse(raw.replace(' ', 'T'))
    if (!Number.isNaN(ms)) return ms
  }
  return 0
}

const scanRows = computed(() => {
  const rows = scanData.value?.results || []
  return [...rows].sort((a, b) => {
    const timeDiff = parseScanTime(b?.scanned_at) - parseScanTime(a?.scanned_at)
    if (timeDiff !== 0) return timeDiff
    return Number(b?.profile_id || 0) - Number(a?.profile_id || 0)
  })
})
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

const formatUpdatedTime = (value) => formatRelativeTimeZh(value, nowTick.value)

const applySystemDefaults = () => {
  const defaults = systemSettings.value?.scan || {}
  if (defaults.default_group_title) {
    selectedGroupTitle.value = defaults.default_group_title
  }
}

const loadSystemSettings = async () => {
  try {
    const envelope = await getSystemSettings()
    systemSettings.value = envelope?.data || null
    applySystemDefaults()
  } catch {
    systemSettings.value = null
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
    saveCache(selectedGroupTitle.value, data)
  } catch (error) {
    if (error?.response?.status === 404) {
      return
    }
    ElMessage.error(error?.response?.data?.detail || '获取最新结果失败')
  } finally {
    latestLoading.value = false
  }
}

const stopRealtimeStream = () => {
  if (realtimeSource) {
    realtimeSource.close()
    realtimeSource = null
  }
  realtimeStatus.value = 'disconnected'
}

const startRealtimeStream = () => {
  stopRealtimeStream()
  if (!selectedGroupTitle.value) return
  const token = localStorage.getItem('token')
  if (!token) return
  const url = `/api/v1/ixbrowser/sora-session-accounts/stream?group_title=${encodeURIComponent(selectedGroupTitle.value)}&token=${encodeURIComponent(token)}`
  realtimeSource = new EventSource(url)
  realtimeStatus.value = 'connecting'

  realtimeSource.addEventListener('update', (event) => {
    try {
      const payload = JSON.parse(event.data)
      scanData.value = payload
      saveCache(selectedGroupTitle.value, payload)
      realtimeStatus.value = 'connected'
    } catch (error) {
      realtimeStatus.value = 'error'
    }
  })

  realtimeSource.addEventListener('ping', () => {
    if (realtimeStatus.value === 'connecting') {
      realtimeStatus.value = 'connected'
    }
  })

  realtimeSource.onerror = () => {
    realtimeStatus.value = 'error'
  }
}

const refreshAll = async () => {
  await loadLatest()
}

const handleSelectionChange = (rows) => {
  const raw = Array.isArray(rows) ? rows : []
  const ids = raw
    .map((row) => Number(row?.profile_id))
    .filter((id) => Number.isFinite(id) && id > 0)
  selectedProfileIds.value = Array.from(new Set(ids))
}

const clearSelection = () => {
  selectedProfileIds.value = []
  if (scanTableRef.value && typeof scanTableRef.value.clearSelection === 'function') {
    scanTableRef.value.clearSelection()
  }
}

const scanNow = async () => {
  if (!selectedGroupTitle.value) {
    ElMessage.warning('请先选择分组')
    return
  }
  scanLoading.value = true
  try {
    const ids = selectedProfileIds.value
    const data = await scanIxBrowserSoraSessionAccounts(selectedGroupTitle.value, ids.length ? ids : null)
    scanData.value = data
    ElMessage.success('扫描完成，结果已入库')
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '扫描失败')
  } finally {
    scanLoading.value = false
  }
}

const onGroupChange = async () => {
  clearSelection()
  loadCache(selectedGroupTitle.value)
  await loadLatest()
  startRealtimeStream()
}

const isOpeningProfile = (profileId) => Boolean(openingProfileIds.value[profileId])

const openWindow = async (row) => {
  const profileId = Number(row?.profile_id)
  if (!Number.isFinite(profileId) || profileId <= 0) {
    ElMessage.error('无效窗口 ID')
    return
  }
  if (openingProfileIds.value[profileId]) return
  openingProfileIds.value = {
    ...openingProfileIds.value,
    [profileId]: true
  }
  try {
    const groupTitle = selectedGroupTitle.value || row?.group_title || 'Sora'
    await openIxBrowserProfileWindow(profileId, groupTitle)
    ElMessage.success(`窗口 ${profileId} 已打开`)
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '打开窗口失败')
  } finally {
    const nextState = { ...openingProfileIds.value }
    delete nextState[profileId]
    openingProfileIds.value = nextState
  }
}

const viewSession = (row) => {
  const payload = {
    account: row.account || null,
    account_plan: row.account_plan || null,
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
  if (row?.quota_source === 'realtime') return 'row-realtime'
  if (row?.fallback_applied) return 'row-fallback'
  if (row?.success === false) return 'row-failed'
  if (row?.success === true) return 'row-success'
  return ''
}

const getSourceLabel = (row) => {
  if (row?.quota_source === 'realtime') return '实时使用'
  if (row?.fallback_applied) return '回填'
  return '最近扫描'
}

const getSourceClass = (row) => {
  if (row?.quota_source === 'realtime') return 'source-realtime'
  if (row?.fallback_applied) return 'source-fallback'
  return 'source-recent'
}

const estimateVideos = (row) => {
  const count = row?.quota_remaining_count
  if (typeof count !== 'number' || Number.isNaN(count)) return '-'
  return Math.floor(count / 2)
}

const getQuotaRemainingText = (row) => {
  const count = row?.quota_remaining_count
  if (typeof count !== 'number' || Number.isNaN(count)) return '-'
  return String(count)
}

const isPlusPlan = (row) => String(row?.account_plan || '').trim().toLowerCase() === 'plus'

const loadCache = (groupTitle) => {
  if (!groupTitle) return
  try {
    const raw = localStorage.getItem(`${cachePrefix}${groupTitle}`)
    if (!raw) return
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && parsed.results) {
      scanData.value = parsed
    }
  } catch {
  }
}

const saveCache = (groupTitle, data) => {
  if (!groupTitle || !data) return
  try {
    localStorage.setItem(`${cachePrefix}${groupTitle}`, JSON.stringify(data))
  } catch {
  }
}

onMounted(async () => {
  nowTick.value = Date.now()
  relativeTimeTimer = window.setInterval(() => {
    nowTick.value = Date.now()
  }, 60000)
  await loadSystemSettings()
  loadCache(selectedGroupTitle.value)
  await loadGroups()
  loadCache(selectedGroupTitle.value)
  await loadLatest()
  startRealtimeStream()
})

onBeforeUnmount(() => {
  if (relativeTimeTimer) {
    clearInterval(relativeTimeTimer)
    relativeTimeTimer = null
  }
  stopRealtimeStream()
})
</script>

<style scoped>
.ix-page {
  padding: 0;
  min-height: auto;
  background: transparent;
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
  --accent-realtime: rgba(16, 185, 129, 0.95);
  --accent-fallback: rgba(245, 158, 11, 0.95);
  --accent-recent: rgba(100, 116, 139, 0.95);
  --accent-danger: rgba(248, 113, 113, 0.95);
}

.command-title {
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--ink);
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
  color: var(--muted);
}

.group-select {
  width: 220px;
}

.meta-info {
  font-size: 12px;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 6px;
}

.meta-info strong {
  font-size: 13px;
  color: var(--ink);
}

.command-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.metric-card.success {
  border-color: #bbf7d0;
}

.metric-card.danger {
  border-color: #fecaca;
}

.metric-card.accent {
  border-color: #bae6fd;
}

.metric-card.highlight {
  border-color: #f5d0fe;
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

.window-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.window-title {
  display: flex;
  align-items: center;
  gap: 10px;
}

.window-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--ink);
}

.window-id {
  font-size: 11px;
  color: var(--muted);
  padding: 2px 6px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.08);
}

.window-account {
  font-size: 12px;
  color: var(--muted);
}

.quota-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.quota-primary {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
}

.quota-primary-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--ink);
  line-height: 1;
}

.quota-primary-unit {
  font-size: 11px;
  color: rgba(71, 85, 105, 0.82);
}

.quota-secondary {
  font-size: 12px;
  font-weight: 600;
  color: rgba(71, 85, 105, 0.92);
  line-height: 1.1;
}

.status-stack {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
}

.updated-relative {
  display: inline-block;
  font-size: 12px;
  color: #475569;
  font-weight: 600;
}

.plan-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 52px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.01em;
  color: #075985;
  border: 1px solid #7dd3fc;
  background: linear-gradient(135deg, rgba(186, 230, 253, 0.9), rgba(219, 234, 254, 0.92));
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8), 0 3px 10px rgba(14, 116, 144, 0.16);
}

.source-tag {
  border: 1px solid transparent;
  font-weight: 600;
}

.source-tag.source-realtime {
  background: rgba(16, 185, 129, 0.12);
  color: #047857;
  border-color: rgba(16, 185, 129, 0.3);
}

.source-tag.source-fallback {
  background: rgba(245, 158, 11, 0.16);
  color: #92400e;
  border-color: rgba(245, 158, 11, 0.35);
}

.source-tag.source-recent {
  background: rgba(148, 163, 184, 0.16);
  color: #475569;
  border-color: rgba(148, 163, 184, 0.35);
}

.scan-table :deep(td.detail-column .cell),
.scan-table :deep(th.detail-column-header .cell) {
  overflow: visible;
  text-overflow: clip;
  white-space: nowrap;
}

.action-buttons {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
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
