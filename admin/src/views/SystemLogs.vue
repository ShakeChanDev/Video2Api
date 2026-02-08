<template>
  <div class="logs-page">
    <section class="command-bar" v-loading="loading">
      <div class="command-left">
        <div class="brand">
          <div class="title">日志中心 V2</div>
          <div class="subtitle">统一事件 · 实时追踪 · 统计分析</div>
        </div>
        <div class="filters">
          <el-select v-model="filters.source" class="w-120" @change="handleFilterChange">
            <el-option label="全部" value="all" />
            <el-option label="API" value="api" />
            <el-option label="审计" value="audit" />
            <el-option label="任务" value="task" />
            <el-option label="系统" value="system" />
          </el-select>

          <el-select v-model="filters.status" class="w-120" clearable @change="handleFilterChange">
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
          </el-select>

          <el-select v-model="filters.level" class="w-120" clearable @change="handleFilterChange">
            <el-option label="DEBUG" value="DEBUG" />
            <el-option label="INFO" value="INFO" />
            <el-option label="WARN" value="WARN" />
            <el-option label="ERROR" value="ERROR" />
          </el-select>

          <el-input v-model="filters.keyword" class="w-220" clearable placeholder="关键词" @keyup.enter="loadAll" />
          <el-input v-model="filters.user" class="w-120" clearable placeholder="用户" @keyup.enter="loadAll" />
          <el-input v-model="filters.action" class="w-180" clearable placeholder="动作" @keyup.enter="loadAll" />
          <el-input v-model="filters.path" class="w-220" clearable placeholder="路径" @keyup.enter="loadAll" />
          <el-input v-model="filters.trace_id" class="w-220" clearable placeholder="trace_id" @keyup.enter="loadAll" />
          <el-input v-model="filters.request_id" class="w-220" clearable placeholder="request_id" @keyup.enter="loadAll" />

          <el-date-picker
            v-model="timeRange"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            @change="handleFilterChange"
          />
        </div>
      </div>

      <div class="command-right">
        <div class="switch-row">
          <span class="switch-label">仅慢请求</span>
          <el-switch v-model="filters.slow_only" @change="handleFilterChange" />
        </div>
        <div class="switch-row">
          <span class="switch-label">实时</span>
          <el-switch v-model="realtimeEnabled" @change="handleRealtimeToggle" />
          <el-tag size="small" :type="realtimeTagType">{{ realtimeStatusText }}</el-tag>
        </div>
        <div class="actions">
          <el-button @click="resetFilters">重置</el-button>
          <el-button type="primary" @click="loadAll">刷新</el-button>
        </div>
      </div>
    </section>

    <section class="stats-grid" v-loading="statsLoading">
      <article class="stat-card">
        <span class="stat-label">总量</span>
        <strong class="stat-value">{{ stats.total_count || 0 }}</strong>
      </article>
      <article class="stat-card danger">
        <span class="stat-label">失败</span>
        <strong class="stat-value">{{ stats.failed_count || 0 }}</strong>
        <span class="stat-sub">{{ stats.failure_rate || 0 }}%</span>
      </article>
      <article class="stat-card warning">
        <span class="stat-label">API P95</span>
        <strong class="stat-value">{{ formatDuration(stats.p95_duration_ms) }}</strong>
      </article>
      <article class="stat-card accent">
        <span class="stat-label">慢请求</span>
        <strong class="stat-value">{{ stats.slow_count || 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">Top 动作</span>
        <div class="stat-list">
          <div v-for="item in stats.top_actions || []" :key="`action-${item.key}`">{{ item.key }} · {{ item.count }}</div>
          <div v-if="!stats.top_actions || stats.top_actions.length === 0">-</div>
        </div>
      </article>
      <article class="stat-card">
        <span class="stat-label">Top 失败原因</span>
        <div class="stat-list">
          <div v-for="item in stats.top_failed_reasons || []" :key="`reason-${item.key}`">{{ item.key }} · {{ item.count }}</div>
          <div v-if="!stats.top_failed_reasons || stats.top_failed_reasons.length === 0">-</div>
        </div>
      </article>
    </section>

    <el-card class="table-card" v-loading="loading">
      <template #header>
        <div class="table-head">
          <span>事件列表</span>
          <span class="table-hint">默认实时全量，当前保留 {{ retentionDays }} 天</span>
        </div>
      </template>

      <el-table :data="logs" class="card-table" empty-text="暂无日志">
        <el-table-column prop="created_at" label="时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="sourceTag(row.source)">{{ row.source }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="action" label="动作" min-width="170" />
        <el-table-column prop="operator_username" label="用户" width="110" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.status" size="small" :type="statusTag(row.status)">{{ row.status }}</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="level" label="等级" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.level" size="small" :type="levelTag(row.level)">{{ row.level }}</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" label="耗时" width="110">
          <template #default="{ row }">
            <span :class="{ 'slow-value': row.is_slow }">{{ formatDuration(row.duration_ms) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="消息" min-width="300" show-overflow-tooltip />
        <el-table-column label="详情" width="100" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openDetail(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="load-more-row">
        <el-button :disabled="!hasMore || loadingMore" :loading="loadingMore" @click="loadMore">
          {{ hasMore ? '加载更多' : '已到底部' }}
        </el-button>
      </div>
    </el-card>

    <el-drawer v-model="detailVisible" title="日志详情" size="560px" direction="rtl">
      <div v-if="detailRow" class="detail">
        <div class="detail-grid">
          <div class="detail-item"><span>ID</span><strong>{{ detailRow.id }}</strong></div>
          <div class="detail-item"><span>来源</span><strong>{{ detailRow.source }}</strong></div>
          <div class="detail-item"><span>动作</span><strong>{{ detailRow.action }}</strong></div>
          <div class="detail-item"><span>状态</span><strong>{{ detailRow.status || '-' }}</strong></div>
          <div class="detail-item"><span>等级</span><strong>{{ detailRow.level || '-' }}</strong></div>
          <div class="detail-item"><span>耗时</span><strong>{{ formatDuration(detailRow.duration_ms) }}</strong></div>
          <div class="detail-item"><span>请求ID</span><strong>{{ detailRow.request_id || '-' }}</strong></div>
          <div class="detail-item"><span>链路ID</span><strong>{{ detailRow.trace_id || '-' }}</strong></div>
          <div class="detail-item"><span>方法</span><strong>{{ detailRow.method || '-' }}</strong></div>
          <div class="detail-item"><span>路径</span><strong>{{ detailRow.path || '-' }}</strong></div>
          <div class="detail-item"><span>资源</span><strong>{{ detailRow.resource_type || '-' }}</strong></div>
          <div class="detail-item"><span>资源ID</span><strong>{{ detailRow.resource_id || '-' }}</strong></div>
          <div class="detail-item"><span>时间</span><strong>{{ formatTime(detailRow.created_at) }}</strong></div>
        </div>
        <div class="detail-label">消息</div>
        <div class="detail-text">{{ detailRow.message || '-' }}</div>

        <div class="detail-label">Metadata</div>
        <pre class="detail-json">{{ formatJson(detailRow.metadata) }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { buildSystemLogStreamUrl, getSystemLogStats, getSystemSettings, listSystemLogsV2 } from '../api'

const loading = ref(false)
const loadingMore = ref(false)
const statsLoading = ref(false)
const logs = ref([])
const hasMore = ref(false)
const nextCursor = ref(null)
const detailVisible = ref(false)
const detailRow = ref(null)
const retentionDays = ref(30)
const realtimeEnabled = ref(true)
const realtimeStatus = ref('disconnected')
const stats = ref({
  total_count: 0,
  failed_count: 0,
  failure_rate: 0,
  p95_duration_ms: null,
  slow_count: 0,
  source_distribution: [],
  top_actions: [],
  top_failed_reasons: []
})

const filters = ref({
  source: 'all',
  status: '',
  level: '',
  keyword: '',
  user: '',
  action: '',
  path: '',
  trace_id: '',
  request_id: '',
  slow_only: false,
  limit: 100
})

const timeRange = ref([])
let realtimeSource = null
let reconnectTimer = null
let statsRefreshTimer = null
let reconnectDelay = 1000

const realtimeStatusText = computed(() => {
  if (!realtimeEnabled.value) return '已关闭'
  if (realtimeStatus.value === 'connected') return '已连接'
  if (realtimeStatus.value === 'connecting') return '连接中'
  return '已断开'
})

const realtimeTagType = computed(() => {
  if (!realtimeEnabled.value) return 'info'
  if (realtimeStatus.value === 'connected') return 'success'
  if (realtimeStatus.value === 'connecting') return 'warning'
  return 'danger'
})

const initRange = () => {
  const end = new Date()
  const start = new Date(end.getTime() - 3 * 24 * 60 * 60 * 1000)
  timeRange.value = [start, end]
}

const buildParams = (cursor = null) => {
  const params = {
    source: filters.value.source,
    limit: filters.value.limit,
    slow_only: !!filters.value.slow_only
  }
  if (filters.value.status) params.status = filters.value.status
  if (filters.value.level) params.level = filters.value.level
  if (filters.value.keyword) params.keyword = filters.value.keyword
  if (filters.value.user) params.user = filters.value.user
  if (filters.value.action) params.action = filters.value.action
  if (filters.value.path) params.path = filters.value.path
  if (filters.value.trace_id) params.trace_id = filters.value.trace_id
  if (filters.value.request_id) params.request_id = filters.value.request_id
  if (cursor) params.cursor = cursor
  if (Array.isArray(timeRange.value) && timeRange.value.length === 2) {
    const [start, end] = timeRange.value
    if (start) params.start_at = new Date(start).toISOString()
    if (end) params.end_at = new Date(end).toISOString()
  }
  return params
}

const matchesFilters = (row) => {
  if (!row || typeof row !== 'object') return false
  if (filters.value.source && filters.value.source !== 'all' && row.source !== filters.value.source) return false
  if (filters.value.status && row.status !== filters.value.status) return false
  if (filters.value.level && row.level !== filters.value.level) return false
  if (filters.value.user && row.operator_username !== filters.value.user) return false
  if (filters.value.action && !String(row.action || '').includes(filters.value.action)) return false
  if (filters.value.path && !String(row.path || '').includes(filters.value.path)) return false
  if (filters.value.trace_id && String(row.trace_id || '') !== filters.value.trace_id) return false
  if (filters.value.request_id && String(row.request_id || '') !== filters.value.request_id) return false
  if (filters.value.slow_only && !row.is_slow) return false
  if (filters.value.keyword) {
    const text = [
      row.message,
      row.action,
      row.path,
      row.request_id,
      row.trace_id,
      row.resource_id,
      row.operator_username
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
    if (!text.includes(String(filters.value.keyword || '').toLowerCase())) return false
  }
  if (Array.isArray(timeRange.value) && timeRange.value.length === 2) {
    const [start, end] = timeRange.value
    const ts = new Date(row.created_at).getTime()
    if (start && ts < new Date(start).getTime()) return false
    if (end && ts > new Date(end).getTime()) return false
  }
  return true
}

const scheduleStatsRefresh = () => {
  if (statsRefreshTimer) clearTimeout(statsRefreshTimer)
  statsRefreshTimer = setTimeout(() => {
    loadStats()
  }, 500)
}

const loadLogs = async ({ append = false } = {}) => {
  if (append) loadingMore.value = true
  else loading.value = true

  try {
    const data = await listSystemLogsV2(buildParams(append ? nextCursor.value : null))
    const items = Array.isArray(data?.items) ? data.items : []
    if (append) logs.value = [...logs.value, ...items]
    else logs.value = items
    hasMore.value = !!data?.has_more
    nextCursor.value = data?.next_cursor || null
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '读取日志失败')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

const loadStats = async () => {
  statsLoading.value = true
  try {
    stats.value = await getSystemLogStats(buildParams())
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '读取统计失败')
  } finally {
    statsLoading.value = false
  }
}

const loadSystemSettings = async () => {
  try {
    const envelope = await getSystemSettings()
    const days = envelope?.data?.logging?.event_log_retention_days
    if (typeof days === 'number' && !Number.isNaN(days)) retentionDays.value = days
  } catch {
    retentionDays.value = 30
  }
}

const loadAll = async () => {
  await Promise.all([loadLogs(), loadStats()])
}

const loadMore = async () => {
  if (!hasMore.value || loadingMore.value) return
  await loadLogs({ append: true })
}

const openDetail = (row) => {
  detailRow.value = row
  detailVisible.value = true
}

const handleFilterChange = () => {
  loadAll()
  if (realtimeEnabled.value) startRealtime()
}

const resetFilters = () => {
  filters.value = {
    source: 'all',
    status: '',
    level: '',
    keyword: '',
    user: '',
    action: '',
    path: '',
    trace_id: '',
    request_id: '',
    slow_only: false,
    limit: 100
  }
  initRange()
  handleFilterChange()
}

const stopRealtime = () => {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (realtimeSource) {
    realtimeSource.close()
    realtimeSource = null
  }
  realtimeStatus.value = 'disconnected'
}

const startRealtime = () => {
  stopRealtime()
  if (!realtimeEnabled.value) return
  realtimeStatus.value = 'connecting'
  const url = buildSystemLogStreamUrl({ source: filters.value.source || 'all' })
  realtimeSource = new EventSource(url)

  realtimeSource.addEventListener('open', () => {
    reconnectDelay = 1000
    realtimeStatus.value = 'connected'
  })

  realtimeSource.addEventListener('ping', () => {
    realtimeStatus.value = 'connected'
  })

  realtimeSource.addEventListener('log', (event) => {
    try {
      const row = JSON.parse(event.data || '{}')
      if (!row || !matchesFilters(row)) return
      const rowId = Number(row.id || 0)
      if (!rowId) return
      if (logs.value.some((item) => Number(item.id || 0) === rowId)) return
      logs.value = [row, ...logs.value]
      if (logs.value.length > 500) logs.value = logs.value.slice(0, 500)
      scheduleStatsRefresh()
    } catch {
      // noop
    }
  })

  realtimeSource.onerror = () => {
    realtimeStatus.value = 'disconnected'
    if (!realtimeEnabled.value) return
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(() => {
      startRealtime()
    }, reconnectDelay)
    reconnectDelay = Math.min(reconnectDelay * 2, 10000)
  }
}

const handleRealtimeToggle = () => {
  if (realtimeEnabled.value) startRealtime()
  else stopRealtime()
}

const formatTime = (value) => {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return String(value)
  }
}

const formatDuration = (value) => {
  if (value === null || value === undefined) return '-'
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${num} ms`
}

const formatJson = (value) => {
  if (!value) return '-'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

const statusTag = (status) => {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  return 'info'
}

const sourceTag = (source) => {
  if (source === 'audit') return 'success'
  if (source === 'api') return 'info'
  if (source === 'task') return 'warning'
  if (source === 'system') return 'danger'
  return 'primary'
}

const levelTag = (level) => {
  if (level === 'ERROR') return 'danger'
  if (level === 'WARN') return 'warning'
  if (level === 'DEBUG') return 'info'
  return 'success'
}

onMounted(async () => {
  initRange()
  await loadSystemSettings()
  await loadAll()
  startRealtime()
})

onBeforeUnmount(() => {
  stopRealtime()
  if (statsRefreshTimer) clearTimeout(statsRefreshTimer)
})
</script>

<style scoped>
.logs-page {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
}

.brand .title {
  font-size: 20px;
  font-weight: 700;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px;
}

.stat-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.stat-card.danger {
  border-color: rgba(239, 68, 68, 0.35);
}

.stat-card.warning {
  border-color: rgba(245, 158, 11, 0.35);
}

.stat-card.accent {
  border-color: rgba(14, 116, 144, 0.35);
}

.stat-label {
  font-size: 12px;
  color: var(--muted);
}

.stat-value {
  font-size: 22px;
  line-height: 1;
}

.stat-sub {
  font-size: 12px;
  color: var(--muted);
}

.stat-list {
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
}

.switch-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.switch-label {
  color: var(--muted);
  font-size: 12px;
}

.load-more-row {
  margin-top: 12px;
  display: flex;
  justify-content: center;
}

.slow-value {
  color: #b45309;
  font-weight: 600;
}

.detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
}

.detail-item {
  background: rgba(15, 23, 42, 0.04);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  font-size: 12px;
  color: var(--muted);
}

.detail-item strong {
  display: block;
  color: var(--ink);
  font-size: 14px;
  margin-top: 4px;
  word-break: break-all;
}

.detail-label {
  font-size: 12px;
  color: var(--muted);
}

.detail-text {
  background: var(--card-strong);
  border-radius: var(--radius-md);
  padding: 12px;
  border: 1px solid var(--border);
  font-size: 13px;
}

.detail-json {
  background: #0f172a;
  color: #e2e8f0;
  padding: 12px;
  border-radius: 12px;
  font-size: 12px;
  max-height: 420px;
  overflow: auto;
}

.w-120 {
  width: 120px;
}

.w-180 {
  width: 180px;
}

.w-220 {
  width: 220px;
}

@media (max-width: 980px) {
  .command-bar {
    flex-direction: column;
    align-items: flex-start;
  }

  .command-right {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
