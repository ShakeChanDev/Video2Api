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
          <div class="meta-info" v-if="selectedGroup">
            <span>窗口数</span>
            <strong>{{ selectedGroup.window_count || 0 }}</strong>
          </div>
        </div>
        <div class="command-note">静默更新优先通过 API 拉取账号数据；token 异常时会自动补扫窗口（可能短暂弹窗）。</div>
      </div>
      <div class="command-right">
        <el-tag size="large" :type="statusTagType">{{ statusText }}</el-tag>
        <el-button size="large" @click="refreshAll" :loading="latestLoading" :disabled="actionLockedBySilentRefresh">刷新</el-button>
        <el-button size="large" type="warning" :loading="scanLoading" :disabled="actionLockedBySilentRefresh" @click="scanNow">
          扫描账号与次数
        </el-button>
        <el-button size="large" type="primary" :loading="silentRefreshStarting" :disabled="actionLockedBySilentRefresh || scanLoading" @click="startSilentRefresh">
          {{ silentRefreshButtonText }}
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

    <el-card class="table-card" v-loading="latestLoading || scanLoading || weightsLoading">
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
              <div class="window-proxy">
                <span class="proxy-label">代理</span>
                <span class="proxy-value">{{ formatProxy(row) }}</span>
                <span v-if="row.real_ip" class="proxy-real">real_ip {{ row.real_ip }}</span>
              </div>
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
        <el-table-column label="总分" width="100" align="center">
          <template #default="{ row }">
            <div class="score-cell">
              <span class="score-total">{{ formatScore(row.score_total) }}</span>
              <el-tag size="small" :type="row.selectable ? 'success' : 'info'">
                {{ row.selectable ? '可选' : '不可选' }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="数量分" width="90" align="center">
          <template #default="{ row }">
            <span class="score-sub">{{ formatScore(row.score_quantity) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="质量分" width="90" align="center">
          <template #default="{ row }">
            <span class="score-sub">{{ formatScore(row.score_quality) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="冷却到期" width="170" align="center">
          <template #default="{ row }">
            <span class="cooldown-text">{{ row.cooldown_until || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="忽略错误" width="110" align="center">
          <template #default="{ row }">
            <span>{{ row.ignored_error_count ?? 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="最近非忽略错误" min-width="260">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.last_non_ignored_error"
              :content="row.last_non_ignored_error"
              placement="top"
              effect="dark"
            >
              <span class="error-text">{{ shorten(row.last_non_ignored_error, 56) }}</span>
            </el-tooltip>
            <span v-else class="error-text error-empty">-</span>
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
        <el-button type="primary" :loading="scanLoading" :disabled="actionLockedBySilentRefresh" @click="scanNow">立即扫描</el-button>
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
  buildIxBrowserSilentRefreshStreamUrl,
  createIxBrowserSilentRefreshJob,
  getIxBrowserSilentRefreshJob,
  getIxBrowserGroupWindows,
  getLatestIxBrowserSoraSessionAccounts,
  getSoraAccountWeights,
  openIxBrowserProfileWindow,
  scanIxBrowserSoraSessionAccounts,
  getSystemSettings
} from '../api'

const latestLoading = ref(false)
const scanLoading = ref(false)
const weightsLoading = ref(false)
const realtimeStatus = ref('disconnected')
let realtimeSource = null
let relativeTimeTimer = null
let silentRefreshSource = null
let silentRefreshReconnectTimer = null
let silentRefreshReconnectAttempt = 0
const cachePrefix = 'sora_accounts_cache_'
const nowTick = ref(Date.now())

const groups = ref([])
const selectedGroupTitle = ref('Sora')
const scanData = ref(null)
const systemSettings = ref(null)
const weightsData = ref([])

const sessionDialogVisible = ref(false)
const currentSessionText = ref('')
const openingProfileIds = ref({})
const scanTableRef = ref(null)
const selectedProfileIds = ref([])
const silentRefreshState = ref(null)
const silentRefreshStarting = ref(false)
const silentRefreshJobId = ref(null)
const silentRefreshWarned = ref(false)

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

const weightMap = computed(() => {
  const map = {}
  const rows = Array.isArray(weightsData.value) ? weightsData.value : []
  rows.forEach((item) => {
    const pid = Number(item?.profile_id || 0)
    if (!Number.isFinite(pid) || pid <= 0) return
    map[pid] = item
  })
  return map
})

const scanRows = computed(() => {
  const rows = scanData.value?.results || []
  const merged = [...rows].map((row) => {
    const pid = Number(row?.profile_id || 0)
    const weight = weightMap.value[pid]
    return {
      ...row,
      ...(weight || {})
    }
  })
  return merged.sort((a, b) => {
    const aSelectable = a?.selectable ? 1 : 0
    const bSelectable = b?.selectable ? 1 : 0
    if (aSelectable !== bSelectable) return bSelectable - aSelectable
    const scoreDiff = Number(b?.score_total || 0) - Number(a?.score_total || 0)
    if (scoreDiff !== 0) return scoreDiff
    const quotaDiff = Number(b?.quota_remaining_count ?? -1) - Number(a?.quota_remaining_count ?? -1)
    if (quotaDiff !== 0) return quotaDiff
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

const isSilentRefreshRunning = computed(() => {
  const status = String(silentRefreshState.value?.status || '').trim().toLowerCase()
  return status === 'queued' || status === 'running'
})

const actionLockedBySilentRefresh = computed(() => {
  if (silentRefreshStarting.value) return true
  return isSilentRefreshRunning.value
})

const silentRefreshButtonText = computed(() => {
  if (!actionLockedBySilentRefresh.value) return '静默更新账号信息'
  const total = Number(silentRefreshState.value?.total_windows || 0)
  const processed = Number(silentRefreshState.value?.processed_windows || 0)
  const pctRaw = Number(silentRefreshState.value?.progress_pct || 0)
  const pct = Number.isFinite(pctRaw) ? Math.max(0, Math.min(100, Math.round(pctRaw))) : 0
  return `更新中 ${processed}/${total} (${pct}%)`
})

const statusText = computed(() => {
  if (scanLoading.value) return '扫描中'
  if (isSilentRefreshRunning.value) return '静默更新中'
  if (!scanData.value) return '暂无数据'
  if (scanData.value.failed_count > 0) return '有失败'
  return '正常'
})

const statusTagType = computed(() => {
  if (scanLoading.value) return 'warning'
  if (isSilentRefreshRunning.value) return 'warning'
  if (!scanData.value) return 'info'
  if (scanData.value.failed_count > 0) return 'danger'
  return 'success'
})

const formatUpdatedTime = (value) => formatRelativeTimeZh(value, nowTick.value)

const formatScore = (value) => {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toFixed(1)
}

const formatProxy = (row) => {
  const ip = String(row?.proxy_ip || '').trim()
  const port = String(row?.proxy_port || '').trim()
  if (!ip || !port) return '-'
  const ptype = String(row?.proxy_type || 'http').trim().toLowerCase() || 'http'
  const localId = Number(row?.proxy_local_id || 0)
  const suffix = localId > 0 ? ` (本地#${localId})` : ''
  return `${ptype}://${ip}:${port}${suffix}`
}

const shorten = (value, maxLen = 60) => {
  const text = String(value || '')
  if (!text) return ''
  if (text.length <= maxLen) return text
  return `${text.slice(0, maxLen)}...`
}

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

const loadWeights = async () => {
  if (!selectedGroupTitle.value) return
  weightsLoading.value = true
  try {
    const data = await getSoraAccountWeights(selectedGroupTitle.value, 200)
    weightsData.value = Array.isArray(data) ? data : []
  } catch (error) {
    weightsData.value = []
    ElMessage.error(error?.response?.data?.detail || '获取账号权重失败')
  } finally {
    weightsLoading.value = false
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

const clearSilentRefreshReconnectTimer = () => {
  if (silentRefreshReconnectTimer) {
    clearTimeout(silentRefreshReconnectTimer)
    silentRefreshReconnectTimer = null
  }
}

const stopSilentRefreshStream = () => {
  clearSilentRefreshReconnectTimer()
  if (silentRefreshSource) {
    silentRefreshSource.close()
    silentRefreshSource = null
  }
}

const resetSilentRefreshState = () => {
  stopSilentRefreshStream()
  silentRefreshReconnectAttempt = 0
  silentRefreshJobId.value = null
  silentRefreshState.value = null
  silentRefreshStarting.value = false
  silentRefreshWarned.value = false
}

const normalizeSilentRefreshPayload = (payload) => {
  if (!payload || typeof payload !== 'object') return null
  const normalized = { ...payload }
  normalized.job_id = Number(normalized.job_id || 0)
  normalized.total_windows = Number(normalized.total_windows || 0)
  normalized.processed_windows = Number(normalized.processed_windows || 0)
  normalized.success_count = Number(normalized.success_count || 0)
  normalized.failed_count = Number(normalized.failed_count || 0)
  normalized.progress_pct = Number(normalized.progress_pct || 0)
  normalized.run_id = normalized.run_id === null || normalized.run_id === undefined ? null : Number(normalized.run_id)
  return normalized
}

const applySilentRefreshPayload = (payload) => {
  const normalized = normalizeSilentRefreshPayload(payload)
  if (!normalized) return null
  if (silentRefreshJobId.value && Number(normalized.job_id || 0) !== Number(silentRefreshJobId.value)) return null
  silentRefreshState.value = normalized
  return normalized
}

const scheduleSilentRefreshReconnect = (jobId) => {
  silentRefreshReconnectAttempt += 1
  const delay = Math.min(10000, 1000 * (2 ** Math.max(0, silentRefreshReconnectAttempt - 1)))
  clearSilentRefreshReconnectTimer()
  silentRefreshReconnectTimer = window.setTimeout(() => {
    connectSilentRefreshStream(jobId)
  }, delay)
}

const handleSilentRefreshDone = async (payload) => {
  const normalized = applySilentRefreshPayload(payload)
  stopSilentRefreshStream()
  silentRefreshStarting.value = false
  silentRefreshReconnectAttempt = 0

  if (normalized?.status === 'failed' && !silentRefreshWarned.value) {
    silentRefreshWarned.value = true
    ElMessage.warning(normalized?.error || normalized?.message || '静默更新失败')
  }

  await loadLatest()
  await loadWeights()
}

const probeSilentRefreshJobStatus = async (jobId) => {
  try {
    const job = await getIxBrowserSilentRefreshJob(jobId)
    const normalized = applySilentRefreshPayload(job)
    if (!normalized) return false
    if (normalized.status === 'completed' || normalized.status === 'failed') {
      await handleSilentRefreshDone(normalized)
      return true
    }
  } catch {
  }
  return false
}

const connectSilentRefreshStream = (jobId) => {
  stopSilentRefreshStream()
  if (!jobId || Number(jobId) <= 0) return

  const currentJobId = Number(jobId)
  const url = buildIxBrowserSilentRefreshStreamUrl(currentJobId)
  silentRefreshSource = new EventSource(url)

  silentRefreshSource.addEventListener('snapshot', (event) => {
    try {
      const payload = JSON.parse(event.data)
      applySilentRefreshPayload(payload)
    } catch {
    }
  })

  silentRefreshSource.addEventListener('progress', (event) => {
    try {
      const payload = JSON.parse(event.data)
      applySilentRefreshPayload(payload)
    } catch {
    }
  })

  silentRefreshSource.addEventListener('done', (event) => {
    try {
      const payload = JSON.parse(event.data)
      handleSilentRefreshDone(payload)
    } catch {
      handleSilentRefreshDone(silentRefreshState.value || {})
    }
  })

  silentRefreshSource.onerror = async () => {
    if (!silentRefreshJobId.value || Number(silentRefreshJobId.value) !== currentJobId) return
    stopSilentRefreshStream()
    const ended = await probeSilentRefreshJobStatus(currentJobId)
    if (!ended) {
      scheduleSilentRefreshReconnect(currentJobId)
    }
  }
}

const startSilentRefresh = async () => {
  if (!selectedGroupTitle.value) {
    ElMessage.warning('请先选择分组')
    return
  }
  if (actionLockedBySilentRefresh.value || scanLoading.value) return

  silentRefreshStarting.value = true
  silentRefreshWarned.value = false
  try {
    const data = await createIxBrowserSilentRefreshJob(selectedGroupTitle.value, true)
    const job = normalizeSilentRefreshPayload(data?.job)
    if (!job || !job.job_id) {
      throw new Error('静默更新任务创建失败')
    }
    silentRefreshJobId.value = Number(job.job_id)
    silentRefreshState.value = job
    silentRefreshReconnectAttempt = 0
    if (job.status === 'completed' || job.status === 'failed') {
      await handleSilentRefreshDone(job)
      return
    }
    connectSilentRefreshStream(silentRefreshJobId.value)
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '静默更新失败')
  } finally {
    silentRefreshStarting.value = false
  }
}

const refreshAll = async () => {
  await loadLatest()
  await loadWeights()
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
    await loadWeights()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '扫描失败')
  } finally {
    scanLoading.value = false
  }
}

const onGroupChange = async () => {
  resetSilentRefreshState()
  clearSelection()
  loadCache(selectedGroupTitle.value)
  await loadLatest()
  await loadWeights()
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
  await loadWeights()
  startRealtimeStream()
})

onBeforeUnmount(() => {
  if (relativeTimeTimer) {
    clearInterval(relativeTimeTimer)
    relativeTimeTimer = null
  }
  stopRealtimeStream()
  stopSilentRefreshStream()
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

.command-note {
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted);
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

.window-proxy {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  color: rgba(71, 85, 105, 0.92);
}

.proxy-label {
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(14, 165, 164, 0.12);
  color: var(--accent-strong);
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 0.04em;
}

.proxy-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: rgba(15, 23, 42, 0.78);
  font-weight: 700;
}

.proxy-real {
  color: rgba(71, 85, 105, 0.82);
  font-size: 11px;
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

.score-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.score-total {
  font-size: 16px;
  font-weight: 800;
  color: var(--ink);
  line-height: 1;
}

.score-sub {
  font-size: 13px;
  font-weight: 700;
  color: rgba(15, 23, 42, 0.86);
}

.cooldown-text {
  font-size: 12px;
  color: rgba(71, 85, 105, 0.92);
  font-weight: 600;
}

.error-text {
  display: inline-block;
  max-width: 100%;
  font-size: 12px;
  color: rgba(71, 85, 105, 0.92);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.error-text.error-empty {
  color: rgba(100, 116, 139, 0.9);
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
