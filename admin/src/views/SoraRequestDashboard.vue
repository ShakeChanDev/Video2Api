<template>
  <div class="sora-requests-page">
    <section class="command-bar" v-loading="loading">
      <div class="command-left">
        <div class="brand">
          <div class="title">ChatGPT 外呼请求看板</div>
          <div class="subtitle">服务器直连 · 趋势 · 归因 · 质量</div>
        </div>
        <div class="filters">
          <el-select v-model="filters.window" class="w-120" @change="handleFilterChange">
            <el-option label="最近1小时" value="1h" />
            <el-option label="最近6小时" value="6h" />
            <el-option label="最近24小时" value="24h" />
            <el-option label="最近7天" value="7d" />
          </el-select>
          <el-select v-model="filters.bucket" class="w-120" @change="handleFilterChange">
            <el-option label="自动分桶" value="auto" />
            <el-option label="1分钟" value="1m" />
            <el-option label="5分钟" value="5m" />
            <el-option label="1小时" value="1h" />
          </el-select>
          <el-select v-model="filters.endpoint_limit" class="w-120" @change="handleFilterChange">
            <el-option :label="`Top ${item}`" :value="item" v-for="item in [5, 10, 15, 20, 30]" :key="`top-${item}`" />
          </el-select>
          <el-select v-model="filters.transport" class="w-120" @change="handleFilterChange">
            <el-option label="全部通道" value="all" />
            <el-option label="httpx" value="httpx" />
            <el-option label="curl-cffi" value="curl_cffi" />
          </el-select>
          <el-input
            v-model="filters.host"
            class="w-220"
            clearable
            placeholder="目标域名（如 sora.chatgpt.com）"
            @keyup.enter="handleFilterChange"
            @clear="handleFilterChange"
          />
          <el-input
            v-model="filters.profile_id"
            class="w-180"
            clearable
            placeholder="profile_id"
            @keyup.enter="handleFilterChange"
            @clear="handleFilterChange"
          />
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
          <span class="switch-label">自动刷新(15s)</span>
          <el-switch v-model="autoRefreshEnabled" @change="handleAutoRefreshToggle" />
        </div>
        <div class="actions">
          <el-button @click="resetAll">重置</el-button>
          <el-button type="primary" @click="loadDashboard">刷新</el-button>
        </div>
      </div>
    </section>

    <section class="metrics-grid">
      <article class="metric-card metric-total">
        <span class="metric-label">总请求</span>
        <strong class="metric-value">{{ kpi.total_count }}</strong>
      </article>
      <article class="metric-card metric-failed">
        <span class="metric-label">失败率</span>
        <strong class="metric-value">{{ formatPercent(kpi.failure_rate) }}</strong>
      </article>
      <article class="metric-card metric-slow">
        <span class="metric-label">慢请求率</span>
        <strong class="metric-value">{{ formatPercent(kpi.slow_rate) }}</strong>
      </article>
      <article class="metric-card">
        <span class="metric-label">CF 命中率</span>
        <strong class="metric-value">{{ formatPercent(kpi.cf_rate) }}</strong>
      </article>
      <article class="metric-card metric-latency">
        <span class="metric-label">P95 延迟</span>
        <strong class="metric-value">{{ formatDuration(kpi.p95_ms) }}</strong>
      </article>
      <article class="metric-card">
        <span class="metric-label">平均 RPM</span>
        <strong class="metric-value">{{ Number(kpi.avg_rpm || 0).toFixed(2) }}</strong>
      </article>
    </section>

    <section class="drill-row">
      <el-tag v-if="selectedEndpoint" type="warning" closable @close="clearEndpointDrill">
        端点钻取：{{ selectedEndpointLabel }}
      </el-tag>
      <el-tag v-if="selectedBucket" type="info" closable @close="clearBucketDrill">
        时间桶：{{ selectedBucket }}
      </el-tag>
      <el-tag v-if="selectedHeatmapCells.length > 0" type="success" closable @close="clearHeatmapDrill">
        热力筛选：{{ selectedHeatmapCells.length }} 格
      </el-tag>
      <span class="meta-text">
        口径：{{ dashboard.meta.scope_rule || '-' }} ｜ 分桶：{{ dashboard.meta.bucket || '-' }} ｜ 更新时间：{{ dashboard.meta.refreshed_at || '-' }}
      </span>
    </section>

    <section class="charts-grid">
      <el-card class="chart-card wide">
        <template #header>
          <div class="table-head stack">
            <span>请求趋势（点击柱体可过滤下方样本）</span>
            <span class="table-hint">柱状：请求量 ｜ 折线：失败率、P95</span>
          </div>
        </template>
        <div ref="trendChartEl" class="chart-canvas chart-lg" />
      </el-card>

      <el-card class="chart-card">
        <template #header>
          <div class="table-head stack">
            <span>端点 TopN（点击端点进入钻取）</span>
            <span class="table-hint">堆叠：成功 / 失败 / 慢请求</span>
          </div>
        </template>
        <div ref="endpointChartEl" class="chart-canvas chart-md" />
      </el-card>

      <el-card class="chart-card">
        <template #header>
          <div class="table-head stack">
            <span>请求热力图（24x7）</span>
            <span class="table-hint">周节律观察</span>
          </div>
        </template>
        <div ref="heatmapChartEl" class="chart-canvas chart-md" />
      </el-card>

      <el-card class="chart-card">
        <template #header>
          <div class="table-head stack">
            <span>延迟直方图</span>
            <span class="table-hint">尾延迟分布</span>
          </div>
        </template>
        <div ref="latencyChartEl" class="chart-canvas chart-sm" />
      </el-card>

      <el-card class="chart-card">
        <template #header>
          <div class="table-head stack">
            <span>状态码分布</span>
            <span class="table-hint">2xx/3xx/4xx/5xx</span>
          </div>
        </template>
        <div ref="statusChartEl" class="chart-canvas chart-sm" />
      </el-card>

      <el-card class="chart-card">
        <template #header>
          <div class="table-head stack">
            <span>Host 分布</span>
            <span class="table-hint">chatgpt 主域外呼占比</span>
          </div>
        </template>
        <div ref="hostChartEl" class="chart-canvas chart-sm" />
      </el-card>
    </section>

    <el-card class="table-card" v-loading="loading">
      <template #header>
        <div class="table-head">
          <span>最近样本（{{ filteredSamples.length }}）</span>
          <span class="table-hint">点击 request_id 跳转日志中心</span>
        </div>
      </template>
      <el-table :data="filteredSamples" class="card-table" empty-text="暂无样本">
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column prop="host" label="Host" min-width="180" show-overflow-tooltip />
        <el-table-column prop="path" label="路径" min-width="280" show-overflow-tooltip />
        <el-table-column prop="transport" label="通道" width="110" />
        <el-table-column prop="profile_id" label="profile_id" width="110" />
        <el-table-column prop="status_code" label="状态码" width="90" />
        <el-table-column prop="duration_ms" label="耗时" width="120">
          <template #default="{ row }">
            <span :class="{ 'slow-value': row.is_slow }">{{ formatDuration(row.duration_ms) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="request_id" label="request_id" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button v-if="row.request_id" link type="primary" @click="jumpToRequest(row)">
              {{ row.request_id }}
            </el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="trace_id" label="trace_id" min-width="220" show-overflow-tooltip />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { getSoraRequestDashboard } from '../api'

const router = useRouter()
const loading = ref(false)
const autoRefreshEnabled = ref(true)
const timeRange = ref([])
const selectedEndpoint = ref(null)
const selectedBucket = ref('')
const selectedHeatmapCells = ref([])

const filters = ref({
  window: '24h',
  bucket: 'auto',
  endpoint_limit: 10,
  transport: 'all',
  host: '',
  profile_id: '',
  sample_limit: 30
})

const createEmptyDashboard = () => ({
  meta: {
    start_at: '',
    end_at: '',
    bucket: '5m',
    bucket_seconds: 300,
    scope_rule: '',
    slow_threshold_ms_current: 2000,
    refreshed_at: '',
    path_filter: null,
    host_filter: null,
    transport_filter: 'all',
    profile_id_filter: null
  },
  kpi: {
    total_count: 0,
    failed_count: 0,
    failure_rate: 0,
    slow_count: 0,
    slow_rate: 0,
    cf_count: 0,
    cf_rate: 0,
    p95_ms: 0,
    avg_rpm: 0
  },
  series: [],
  endpoint_top: [],
  status_code_dist: [],
  host_dist: [],
  transport_dist: [],
  latency_histogram: [],
  heatmap_hourly: [],
  recent_samples: []
})

const dashboard = ref(createEmptyDashboard())
const kpi = computed(() => dashboard.value.kpi || createEmptyDashboard().kpi)
const selectedEndpointLabel = computed(() => {
  if (!selectedEndpoint.value) return ''
  const host = String(selectedEndpoint.value.host || '')
  const path = String(selectedEndpoint.value.path || '')
  if (!host) return path || '-'
  return `${host}${path || ''}`
})
const filteredSeries = computed(() => {
  const rows = Array.isArray(dashboard.value.series) ? dashboard.value.series : []
  if (!selectedHeatmapCells.value.length) return rows
  const selectedSet = new Set(selectedHeatmapCells.value)
  return rows.filter((item) => {
    const text = String(item.bucket_at || '')
    if (!text) return false
    const dt = new Date(text.replace(' ', 'T'))
    if (!Number.isFinite(dt.getTime())) return false
    const weekday = (dt.getDay() + 6) % 7
    const hour = dt.getHours()
    return selectedSet.has(`${weekday}-${hour}`)
  })
})

const filteredSamples = computed(() => {
  const rows = Array.isArray(dashboard.value.recent_samples) ? dashboard.value.recent_samples : []
  const selectedSet = new Set(selectedHeatmapCells.value)
  return rows.filter((item) => {
    if (selectedBucket.value && String(item.bucket_at || '') !== selectedBucket.value) return false
    if (selectedEndpoint.value) {
      const endpointPath = String(selectedEndpoint.value.path || '')
      const endpointHost = String(selectedEndpoint.value.host || '')
      if (endpointPath && String(item.path || '') !== endpointPath) return false
      if (endpointHost && String(item.host || '') !== endpointHost) return false
    }
    if (selectedSet.size > 0) {
      const dt = new Date(String(item.created_at || '').replace(' ', 'T'))
      if (!Number.isFinite(dt.getTime())) return false
      const weekday = (dt.getDay() + 6) % 7
      const hour = dt.getHours()
      if (!selectedSet.has(`${weekday}-${hour}`)) return false
    }
    return true
  })
})

const trendChartEl = ref(null)
const endpointChartEl = ref(null)
const heatmapChartEl = ref(null)
const latencyChartEl = ref(null)
const statusChartEl = ref(null)
const hostChartEl = ref(null)
let trendChart = null
let endpointChart = null
let heatmapChart = null
let latencyChart = null
let statusChart = null
let hostChart = null
let debounceTimer = null
let autoRefreshTimer = null

const formatPercent = (value) => `${Number(value || 0).toFixed(2)}%`
const formatDuration = (value) => {
  if (value === null || value === undefined) return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  if (num >= 1000) return `${(num / 1000).toFixed(2)}s`
  return `${Math.round(num)}ms`
}

const shortPath = (value, maxLen = 42) => {
  const text = String(value || '')
  if (!text) return '-'
  if (text.length <= maxLen) return text
  return `${text.slice(0, maxLen)}...`
}

const buildEndpointText = (item) => {
  if (!item) return '-'
  if (String(item.path || '') === '__others__') return '其他端点'
  const host = String(item.host || '')
  const path = String(item.path || '')
  return host ? `${host}${path}` : path || '-'
}

const formatBucketAxisLabel = (value) => {
  const text = String(value || '')
  if (!text) return '-'
  if (filters.value.window === '7d') return text.slice(5, 16)
  return text.slice(11, 16)
}

const normalizeDashboard = (payload) => ({
  ...createEmptyDashboard(),
  ...(payload || {}),
  meta: { ...createEmptyDashboard().meta, ...(payload?.meta || {}) },
  kpi: { ...createEmptyDashboard().kpi, ...(payload?.kpi || {}) },
  series: Array.isArray(payload?.series) ? payload.series : [],
  endpoint_top: Array.isArray(payload?.endpoint_top) ? payload.endpoint_top : [],
  status_code_dist: Array.isArray(payload?.status_code_dist) ? payload.status_code_dist : [],
  host_dist: Array.isArray(payload?.host_dist) ? payload.host_dist : [],
  transport_dist: Array.isArray(payload?.transport_dist) ? payload.transport_dist : [],
  latency_histogram: Array.isArray(payload?.latency_histogram) ? payload.latency_histogram : [],
  heatmap_hourly: Array.isArray(payload?.heatmap_hourly) ? payload.heatmap_hourly : [],
  recent_samples: Array.isArray(payload?.recent_samples) ? payload.recent_samples : []
})

const buildParams = () => {
  const params = {
    window: filters.value.window,
    bucket: filters.value.bucket,
    endpoint_limit: filters.value.endpoint_limit,
    transport: filters.value.transport,
    sample_limit: filters.value.sample_limit
  }
  if (selectedEndpoint.value?.path) {
    params.path = selectedEndpoint.value.path
  }
  if (selectedEndpoint.value?.host) {
    params.host = selectedEndpoint.value.host
  } else if (filters.value.host) {
    params.host = String(filters.value.host).trim()
  }
  if (filters.value.profile_id) {
    const profileId = Number(filters.value.profile_id)
    if (Number.isInteger(profileId) && profileId > 0) {
      params.profile_id = profileId
    }
  }
  if (Array.isArray(timeRange.value) && timeRange.value.length === 2) {
    const [start, end] = timeRange.value
    if (start) params.start_at = new Date(start).toISOString()
    if (end) params.end_at = new Date(end).toISOString()
  }
  return params
}

const ensureCharts = () => {
  if (trendChartEl.value && !trendChart) trendChart = echarts.init(trendChartEl.value)
  if (endpointChartEl.value && !endpointChart) endpointChart = echarts.init(endpointChartEl.value)
  if (heatmapChartEl.value && !heatmapChart) heatmapChart = echarts.init(heatmapChartEl.value)
  if (latencyChartEl.value && !latencyChart) latencyChart = echarts.init(latencyChartEl.value)
  if (statusChartEl.value && !statusChart) statusChart = echarts.init(statusChartEl.value)
  if (hostChartEl.value && !hostChart) hostChart = echarts.init(hostChartEl.value)
}

const renderTrendChart = () => {
  if (!trendChart) return
  const rows = Array.isArray(filteredSeries.value) ? filteredSeries.value : []
  const categories = rows.map((item) => String(item.bucket_at || ''))
  const totalData = rows.map((item) => Number(item.total_count || 0))
  const failedRateData = rows.map((item) => Number(item.failure_rate || 0))
  const p95Data = rows.map((item) => (item.p95_ms === null || item.p95_ms === undefined ? null : Number(item.p95_ms)))
  const slowMap = new Map(rows.map((item) => [String(item.bucket_at || ''), Number(item.slow_count || 0)]))
  trendChart.setOption(
    {
      color: ['#0ea5a4', '#ef4444', '#1f2937'],
      animation: false,
      grid: { left: 56, right: 92, top: 36, bottom: 44 },
      tooltip: {
        trigger: 'axis',
        formatter: (params) => {
          const list = Array.isArray(params) ? params : []
          const point = list[0] || {}
          const key = String(point.axisValue || '')
          const total = Number(list.find((item) => item.seriesName === '请求量')?.value || 0)
          const failedRate = Number(list.find((item) => item.seriesName === '失败率')?.value || 0)
          const p95 = list.find((item) => item.seriesName === 'P95')?.value
          const slowCount = Number(slowMap.get(key) || 0)
          return `${key}<br/>请求量：${total}<br/>失败率：${failedRate.toFixed(2)}%<br/>P95：${p95 === null || p95 === undefined ? '-' : formatDuration(p95)}<br/>慢请求：${slowCount}`
        }
      },
      legend: { top: 6 },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { formatter: (value) => formatBucketAxisLabel(value) }
      },
      yAxis: [
        { type: 'value', name: '请求量', minInterval: 1, splitLine: { show: true } },
        { type: 'value', name: '失败率%', min: 0, max: 100, position: 'right' },
        { type: 'value', name: 'P95(ms)', position: 'right', offset: 60, splitLine: { show: false } }
      ],
      series: [
        { name: '请求量', type: 'bar', yAxisIndex: 0, data: totalData, barMaxWidth: 22 },
        { name: '失败率', type: 'line', yAxisIndex: 1, data: failedRateData, smooth: false, symbol: 'none' },
        { name: 'P95', type: 'line', yAxisIndex: 2, data: p95Data, smooth: false, symbol: 'none' }
      ]
    },
    true
  )
  trendChart.off('click')
  trendChart.on('click', (event) => {
    const bucketAt = String(event?.name || '')
    if (!bucketAt) return
    selectedBucket.value = selectedBucket.value === bucketAt ? '' : bucketAt
  })
}

const renderEndpointChart = () => {
  if (!endpointChart) return
  const rows = Array.isArray(dashboard.value.endpoint_top) ? dashboard.value.endpoint_top : []
  const labels = rows.map((item) => shortPath(buildEndpointText(item)))
  endpointChart.setOption(
    {
      color: ['#22c55e', '#ef4444', '#f59e0b'],
      animation: false,
      grid: { left: 14, right: 20, top: 20, bottom: 26, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const list = Array.isArray(params) ? params : []
          const idx = Number(list[0]?.dataIndex || 0)
          const row = rows[idx] || {}
          return `${buildEndpointText(row)}<br/>请求量：${row.total_count || 0}<br/>占比：${Number(row.share_pct || 0).toFixed(2)}%`
        }
      },
      xAxis: { type: 'value', minInterval: 1 },
      yAxis: { type: 'category', data: labels, inverse: true },
      series: [
        { name: '成功', type: 'bar', stack: 'total', data: rows.map((item) => Number(item.success_count || 0)) },
        { name: '失败', type: 'bar', stack: 'total', data: rows.map((item) => Number(item.failed_count || 0)) },
        { name: '慢请求', type: 'bar', stack: 'total', data: rows.map((item) => Number(item.slow_count || 0)) }
      ]
    },
    true
  )
  endpointChart.off('click')
  endpointChart.on('click', (event) => {
    const row = rows[Number(event?.dataIndex || 0)]
    if (!row || !row.path || row.path === '__others__') return
    selectedBucket.value = ''
    const nextValue = {
      host: String(row.host || ''),
      path: String(row.path || '')
    }
    const isSame =
      selectedEndpoint.value &&
      String(selectedEndpoint.value.host || '') === nextValue.host &&
      String(selectedEndpoint.value.path || '') === nextValue.path
    selectedEndpoint.value = isSame ? null : nextValue
    scheduleReload()
  })
}

const renderHeatmapChart = () => {
  if (!heatmapChart) return
  const rows = Array.isArray(dashboard.value.heatmap_hourly) ? dashboard.value.heatmap_hourly : []
  const weekdayLabels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
  const hourLabels = Array.from({ length: 24 }, (_, idx) => String(idx).padStart(2, '0'))
  const maxCount = rows.reduce((max, item) => Math.max(max, Number(item.count || 0)), 0)
  const data = rows.map((item) => [Number(item.hour || 0), Number(item.weekday || 0), Number(item.count || 0)])
  heatmapChart.setOption(
    {
      animation: false,
      grid: { left: 54, right: 20, top: 20, bottom: 30 },
      tooltip: {
        formatter: (params) => {
          const value = Array.isArray(params?.value) ? params.value : []
          const hour = Number(value[0] || 0)
          const weekday = Number(value[1] || 0)
          const count = Number(value[2] || 0)
          return `${weekdayLabels[weekday]} ${String(hour).padStart(2, '0')}:00<br/>请求量：${count}`
        }
      },
      xAxis: { type: 'category', data: hourLabels },
      yAxis: { type: 'category', data: weekdayLabels },
      toolbox: {
        right: 4,
        feature: {
          brush: {
            type: ['rect', 'clear']
          }
        }
      },
      brush: {
        xAxisIndex: 0,
        yAxisIndex: 0,
        brushMode: 'single',
        throttleType: 'debounce',
        throttleDelay: 200
      },
      visualMap: {
        min: 0,
        max: Math.max(1, maxCount),
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        inRange: { color: ['#ecfeff', '#0ea5a4'] }
      },
      series: [
        {
          type: 'heatmap',
          data,
          emphasis: { itemStyle: { borderColor: '#1f2937', borderWidth: 1 } }
        }
      ]
    },
    true
  )
  heatmapChart.off('brushSelected')
  heatmapChart.on('brushSelected', (event) => {
    const batch = Array.isArray(event?.batch) ? event.batch : []
    const selected = batch[0]?.selected?.[0]?.dataIndex
    if (!Array.isArray(selected) || selected.length === 0) {
      selectedHeatmapCells.value = []
      renderTrendChart()
      return
    }
    const keys = selected
      .map((idx) => rows[Number(idx)])
      .filter(Boolean)
      .map((item) => `${Number(item.weekday || 0)}-${Number(item.hour || 0)}`)
    selectedHeatmapCells.value = Array.from(new Set(keys))
    renderTrendChart()
  })
}

const renderLatencyChart = () => {
  if (!latencyChart) return
  const rows = Array.isArray(dashboard.value.latency_histogram) ? dashboard.value.latency_histogram : []
  latencyChart.setOption(
    {
      animation: false,
      grid: { left: 42, right: 16, top: 20, bottom: 40 },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: rows.map((item) => item.label || item.key) },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        {
          type: 'bar',
          data: rows.map((item) => Number(item.count || 0)),
          itemStyle: { color: '#1f2937' },
          barMaxWidth: 36
        }
      ]
    },
    true
  )
}

const renderStatusChart = () => {
  if (!statusChart) return
  const rows = Array.isArray(dashboard.value.status_code_dist) ? dashboard.value.status_code_dist : []
  statusChart.setOption(
    {
      animation: false,
      color: ['#10b981', '#06b6d4', '#f59e0b', '#ef4444', '#64748b'],
      tooltip: {
        trigger: 'item',
        formatter: (item) => `${item?.name || '-'}：${item?.value || 0}`
      },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: ['36%', '66%'],
          center: ['50%', '45%'],
          data: rows.map((item) => ({ name: item.key, value: Number(item.count || 0) }))
        }
      ]
    },
    true
  )
}

const renderHostChart = () => {
  if (!hostChart) return
  const rows = Array.isArray(dashboard.value.host_dist) ? dashboard.value.host_dist : []
  hostChart.setOption(
    {
      animation: false,
      color: ['#0ea5a4', '#0284c7', '#475569', '#f59e0b', '#ef4444', '#16a34a'],
      tooltip: {
        trigger: 'item',
        formatter: (item) => `${item?.name || '-'}：${item?.value || 0}`
      },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: ['36%', '66%'],
          center: ['50%', '45%'],
          data: rows.map((item) => ({ name: item.key, value: Number(item.count || 0) }))
        }
      ]
    },
    true
  )
}

const renderCharts = () => {
  ensureCharts()
  renderTrendChart()
  renderEndpointChart()
  renderHeatmapChart()
  renderLatencyChart()
  renderStatusChart()
  renderHostChart()
}

const resizeCharts = () => {
  trendChart?.resize()
  endpointChart?.resize()
  heatmapChart?.resize()
  latencyChart?.resize()
  statusChart?.resize()
  hostChart?.resize()
}

const scheduleReload = () => {
  if (debounceTimer) {
    clearTimeout(debounceTimer)
    debounceTimer = null
  }
  debounceTimer = setTimeout(() => {
    void loadDashboard()
  }, 300)
}

const loadDashboard = async ({ silent = false } = {}) => {
  if (!silent) loading.value = true
  try {
    const payload = await getSoraRequestDashboard(buildParams())
    dashboard.value = normalizeDashboard(payload)
    await nextTick()
    renderCharts()
  } catch (error) {
    if (!silent) {
      ElMessage.error(error?.response?.data?.detail || '读取看板数据失败')
    }
  } finally {
    if (!silent) loading.value = false
  }
}

const startAutoRefresh = () => {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer)
    autoRefreshTimer = null
  }
  if (!autoRefreshEnabled.value) return
  autoRefreshTimer = setInterval(() => {
    void loadDashboard({ silent: true })
  }, 15000)
}

const handleAutoRefreshToggle = () => {
  startAutoRefresh()
}

const handleFilterChange = () => {
  selectedEndpoint.value = null
  selectedBucket.value = ''
  selectedHeatmapCells.value = []
  if (heatmapChart) {
    try {
      heatmapChart.dispatchAction({ type: 'brush', areas: [] })
    } catch {
      // noop
    }
  }
  scheduleReload()
}

const clearEndpointDrill = () => {
  selectedEndpoint.value = null
  selectedBucket.value = ''
  scheduleReload()
}

const clearBucketDrill = () => {
  selectedBucket.value = ''
}

const clearHeatmapDrill = () => {
  selectedHeatmapCells.value = []
  renderTrendChart()
  if (heatmapChart) {
    try {
      heatmapChart.dispatchAction({ type: 'brush', areas: [] })
    } catch {
      // noop
    }
  }
}

const resetAll = () => {
  filters.value = {
    window: '24h',
    bucket: 'auto',
    endpoint_limit: 10,
    transport: 'all',
    host: '',
    profile_id: '',
    sample_limit: 30
  }
  timeRange.value = []
  selectedEndpoint.value = null
  selectedBucket.value = ''
  selectedHeatmapCells.value = []
  void loadDashboard()
}

const jumpToRequest = (row) => {
  if (!row?.request_id) return
  router.push({
    path: '/logs',
    query: {
      source: 'task',
      request_id: row.request_id
    }
  })
}

const disposeCharts = () => {
  trendChart?.dispose()
  endpointChart?.dispose()
  heatmapChart?.dispose()
  latencyChart?.dispose()
  statusChart?.dispose()
  hostChart?.dispose()
  trendChart = null
  endpointChart = null
  heatmapChart = null
  latencyChart = null
  statusChart = null
  hostChart = null
}

onMounted(async () => {
  await loadDashboard()
  startAutoRefresh()
  window.addEventListener('resize', resizeCharts)
})

onBeforeUnmount(() => {
  if (debounceTimer) {
    clearTimeout(debounceTimer)
    debounceTimer = null
  }
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer)
    autoRefreshTimer = null
  }
  window.removeEventListener('resize', resizeCharts)
  disposeCharts()
})
</script>

<style scoped>
.sora-requests-page {
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
}

.drill-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.meta-text {
  font-size: 12px;
  color: var(--muted);
}

.metric-card {
  min-height: 96px;
}

.metric-total {
  border-color: rgba(14, 165, 164, 0.3);
}

.metric-failed {
  border-color: rgba(239, 68, 68, 0.3);
}

.metric-slow {
  border-color: rgba(245, 158, 11, 0.35);
}

.metric-latency {
  border-color: rgba(15, 23, 42, 0.24);
}

.charts-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--page-gap);
}

.chart-card {
  overflow: hidden;
}

.chart-card.wide {
  grid-column: 1 / span 2;
}

.chart-canvas {
  width: 100%;
}

.chart-lg {
  height: 340px;
}

.chart-md {
  height: 320px;
}

.chart-sm {
  height: 280px;
}

.slow-value {
  color: var(--danger);
  font-weight: 600;
}

@media (max-width: 1200px) {
  .charts-grid {
    grid-template-columns: 1fr;
  }

  .chart-card.wide {
    grid-column: auto;
  }
}
</style>
