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
            <el-option label="接口请求" value="api" />
            <el-option label="审计操作" value="audit" />
            <el-option label="任务执行" value="task" />
            <el-option label="系统运行" value="system" />
          </el-select>

          <el-select v-model="filters.status" class="w-120" clearable @change="handleFilterChange">
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
          </el-select>

          <el-select v-model="filters.level" class="w-120" clearable @change="handleFilterChange">
            <el-option label="调试(DEBUG)" value="DEBUG" />
            <el-option label="信息(INFO)" value="INFO" />
            <el-option label="警告(WARN)" value="WARN" />
            <el-option label="错误(ERROR)" value="ERROR" />
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
          <div v-for="item in stats.top_actions || []" :key="`action-${item.key}`">{{ formatAction(item.key) }} · {{ item.count }}</div>
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
        <el-table-column prop="created_at" label="时间" width="168">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="108">
          <template #default="{ row }">
            <el-tag size="small" :type="sourceTag(row.source)">{{ sourceLabel(row.source) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ formatAction(row.action, row) }}</template>
        </el-table-column>
        <el-table-column prop="operator_username" label="操作人" width="120">
          <template #default="{ row }">{{ row.operator_username || '-' }}</template>
        </el-table-column>
        <el-table-column prop="status" label="结果" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.status" size="small" :type="statusTag(row.status)">{{ statusLabel(row.status) }}</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="level" label="级别" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.level" size="small" :type="levelTag(row.level)">{{ levelLabel(row.level) }}</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" label="耗时" width="100">
          <template #default="{ row }">
            <span :class="{ 'slow-value': row.is_slow }">{{ formatDuration(row.duration_ms) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="事件说明" min-width="420">
          <template #default="{ row }">
            <div class="message-main">{{ formatReadableMessage(row) }}</div>
            <div v-if="showRawMessage(row)" class="message-sub">{{ row.message }}</div>
          </template>
        </el-table-column>
        <el-table-column label="详情" width="88" fixed="right">
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
          <div class="detail-item"><span>来源</span><strong>{{ sourceLabel(detailRow.source) }}</strong></div>
          <div class="detail-item"><span>动作</span><strong>{{ formatAction(detailRow.action, detailRow) }}</strong></div>
          <div class="detail-item"><span>状态</span><strong>{{ statusLabel(detailRow.status) }}</strong></div>
          <div class="detail-item"><span>等级</span><strong>{{ levelLabel(detailRow.level) }}</strong></div>
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

const SOURCE_LABELS = {
  api: '接口',
  audit: '审计',
  task: '任务',
  system: '系统',
  all: '全部'
}

const STATUS_LABELS = {
  success: '成功',
  failed: '失败'
}

const LEVEL_LABELS = {
  DEBUG: '调试',
  INFO: '信息',
  WARN: '警告',
  ERROR: '错误'
}

const ACTION_LABELS = {
  'api.request': '接口请求',
  'auth.login': '用户登录',
  'ixbrowser.profile.open': '打开浏览器配置',
  'ixbrowser.scan': '扫描账号会话',
  'ixbrowser.generate.create': '创建生成任务',
  'ixbrowser.generate.publish': '发布生成结果',
  'ixbrowser.generate.genid': '获取生成 ID',
  'sora.job.create': '创建 Sora 任务',
  'sora.job.retry': '重试 Sora 任务',
  'sora.job.watermark.retry': '重试去水印',
  'sora.job.cancel': '取消 Sora 任务',
  'nurture.batch.create': '创建养号批次',
  'nurture.batch.cancel': '取消养号批次',
  'scheduler.scan.lock_conflict': '扫描调度冲突',
  'scheduler.scan.trigger': '触发扫描调度',
  'scheduler.account_recovery.trigger': '触发账号恢复',
  'scheduler.account_recovery.paused': '账号恢复已暂停',
  'scheduler.missing': '调度配置缺失',
  'worker.start': '任务进程启动',
  'worker.start.skipped': '任务进程跳过启动',
  'worker.stop': '任务进程停止',
  'worker.stop.skipped': '任务进程跳过停止',
  'worker.sora.claim': 'Sora 任务领取',
  'worker.sora.run': 'Sora 任务执行',
  'worker.sora.lease.clear': 'Sora 任务释放租约',
  'worker.sora.heartbeat': 'Sora 进程心跳',
  'worker.nurture.claim': '养号任务领取',
  'worker.nurture.run': '养号任务执行',
  'worker.nurture.lease.clear': '养号任务释放租约',
  'worker.nurture.heartbeat': '养号进程心跳',
  'background.task.error': '后台任务异常',
  'app.startup.background_services': '启动后台服务',
  'app.shutdown.background_services': '停止后台服务'
}

const ACTION_SEGMENT_LABELS = {
  app: '应用',
  api: '接口',
  auth: '认证',
  ixbrowser: 'ixBrowser',
  sora: 'Sora',
  nurture: '养号',
  scheduler: '调度',
  worker: '任务进程',
  account_recovery: '账号恢复',
  background: '后台',
  task: '任务',
  start: '启动',
  stop: '停止',
  skipped: '跳过',
  run: '执行',
  claim: '领取',
  heartbeat: '心跳',
  create: '创建',
  retry: '重试',
  cancel: '取消',
  trigger: '触发',
  lock_conflict: '锁冲突',
  request: '请求',
  scan: '扫描',
  profile: '配置',
  generate: '生成',
  publish: '发布',
  genid: '生成ID',
  login: '登录',
  error: '异常',
  missing: '缺失',
  lease: '租约',
  clear: '释放',
  watermark: '去水印',
  batch: '批次',
  job: '任务',
  services: '服务'
}

const API_PATH_LABELS = [
  { prefix: '/api/v1/admin/logs/stats', label: '日志统计查询' },
  { prefix: '/api/v1/admin/logs/stream', label: '日志实时订阅' },
  { prefix: '/api/v1/admin/logs', label: '日志列表查询' },
  { prefix: '/api/v1/admin/settings/system', label: '系统设置' },
  { prefix: '/api/v1/admin/settings', label: '系统设置' },
  { prefix: '/api/v1/admin/users', label: '后台用户管理' },
  { prefix: '/api/v1/sora/jobs/stream', label: 'Sora 任务订阅' },
  { prefix: '/api/v1/sora/jobs', label: 'Sora 任务管理' },
  { prefix: '/api/v1/nurture', label: '养号任务' },
  { prefix: '/api/v1/ixbrowser/sora-generate', label: 'Sora 生成' },
  { prefix: '/api/v1/ixbrowser/sora-session-accounts', label: '会话账号扫描' },
  { prefix: '/api/v1/ixbrowser/profiles', label: '浏览器配置' },
  { prefix: '/api/v1/auth/login', label: '登录鉴权' },
  { prefix: '/api/v1/auth/me', label: '当前用户信息' }
]

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

const isSelfLogQuery = (row) => {
  if (!row || String(row.source || '').toLowerCase() !== 'api') return false
  const path = String(row.path || '')
  return path === '/api/v1/admin/logs' || path === '/api/v1/admin/logs/stats'
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
      if (!isSelfLogQuery(row)) scheduleStatsRefresh()
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

const sourceLabel = (source) => {
  const key = String(source || '').toLowerCase()
  return SOURCE_LABELS[key] || (source ? String(source) : '-')
}

const statusLabel = (status) => {
  const key = String(status || '').toLowerCase()
  return STATUS_LABELS[key] || (status ? String(status) : '-')
}

const levelLabel = (level) => {
  const key = String(level || '').toUpperCase()
  return LEVEL_LABELS[key] || (level ? String(level) : '-')
}

const formatAction = (action) => {
  const key = String(action || '').trim()
  if (!key) return '系统事件'
  if (ACTION_LABELS[key]) return ACTION_LABELS[key]
  if (!key.includes('.')) return key
  return key
    .split('.')
    .map((segment) => ACTION_SEGMENT_LABELS[segment] || segment)
    .join(' / ')
}

const formatApiTarget = (path) => {
  const text = String(path || '').trim()
  if (!text) return '接口请求'
  const hit = API_PATH_LABELS.find((item) => text.startsWith(item.prefix))
  if (hit) return hit.label
  return text
}

const formatReadableMessage = (row) => {
  if (!row || typeof row !== 'object') return '-'
  const source = String(row.source || '').toLowerCase()
  if (source === 'api') {
    const method = String(row.method || '').toUpperCase()
    const result = statusLabel(row.status)
    const duration = formatDuration(row.duration_ms)
    const parts = []
    if (method) parts.push(method)
    parts.push(formatApiTarget(row.path))
    if (result !== '-') parts.push(result)
    if (duration !== '-') parts.push(duration)
    return parts.join(' · ')
  }
  const actionText = formatAction(row.action)
  if (row.status) return `${actionText} · ${statusLabel(row.status)}`
  return actionText
}

const showRawMessage = (row) => {
  const raw = String(row?.message || '').trim()
  if (!raw) return false
  return raw !== formatReadableMessage(row)
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

.message-main {
  color: var(--ink);
  font-weight: 600;
  font-size: 13px;
  line-height: 1.35;
}

.message-sub {
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
