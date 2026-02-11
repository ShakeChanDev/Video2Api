<template>
  <div class="proxy-page">
    <section class="command-bar" v-loading="loading">
      <div class="command-left">
        <div class="command-title">代理列表</div>
        <div class="filters">
          <el-input
            v-model="keyword"
            class="w-260"
            clearable
            placeholder="搜索 ip/端口/ix_id/账号"
            @clear="handleSearch"
            @keyup.enter="handleSearch"
          />
          <el-select v-model="pageSize" class="w-140" @change="handlePageSizeChange">
            <el-option label="50/页" :value="50" />
            <el-option label="100/页" :value="100" />
            <el-option label="200/页" :value="200" />
            <el-option label="500/页" :value="500" />
          </el-select>
        </div>
      </div>

      <div class="command-right">
        <el-tag size="large" effect="light" type="info">已选 {{ selectedIds.length }}</el-tag>
        <el-button size="large" @click="loadList">刷新</el-button>
        <el-button size="large" type="primary" @click="openImportDialog">批量导入</el-button>
        <el-button size="large" type="warning" :loading="pulling" @click="syncPull">从 ixBrowser 同步</el-button>
        <el-button size="large" :disabled="!selectedIds.length" :loading="pushing" @click="syncPush">
          同步到 ixBrowser
        </el-button>
        <el-button size="large" :disabled="!selectedIds.length" @click="openUpdateDialog">批量更新</el-button>
        <el-button size="large" :disabled="!selectedIds.length" :loading="checking" @click="openCheckDialog">
          批量检测
        </el-button>
      </div>
    </section>

    <el-card class="table-card" v-loading="loading">
      <template #header>
        <div class="table-head">
          <span>代理列表</span>
          <span class="table-hint">SQLite 为主库；账号代理绑定关系以 ixBrowser profile-list 为准（本系统只读透传）</span>
        </div>
      </template>

      <el-table
        :data="rows"
        class="card-table"
        row-key="id"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="46" align="center" reserve-selection :selectable="isSelectableRow" />
        <el-table-column label="类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="light" :type="isUnknownRow(row) ? 'warning' : 'info'">
              {{ isUnknownRow(row) ? 'UNKNOWN' : (row.proxy_type || 'http').toUpperCase() }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="代理" min-width="460">
          <template #default="{ row }">
            <div class="addr-cell">
              <div class="addr-main">
                <span class="mono addr-text" :title="formatProxyDisplay(row)">{{ formatProxyDisplay(row) }}</span>
                <el-button
                  v-if="!isUnknownRow(row)"
                  class="copy-btn"
                  link
                  type="primary"
                  size="small"
                  @click.stop="copyProxyText(row)"
                >
                  复制
                </el-button>
              </div>
              <span v-if="!isUnknownRow(row) && (row.ix_country || row.ix_city)" class="addr-meta">
                <span class="country-flag">{{ getCountryFlag(row?.ix_country) }}</span>
                <span>{{ [row.ix_country, row.ix_city].filter(Boolean).join(' / ') }}</span>
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="`CF 风控(近${cfRecentWindow}次)`" width="210" align="center">
          <template #default="{ row }">
            <div class="cf-heat-cell" @mouseenter="handleCfHeatMouseEnter(row)">
              <div class="cf-heat-grid" :style="{ '--cf-window': String(cfRecentWindow || 30) }">
                <span
                  v-for="(dot, dotIndex) in getCfHeatDots(row)"
                  :key="`${row.id || 'unknown'}-${dotIndex}`"
                  :class="['cf-heat-dot', getCfHeatDotClass(dot)]"
                  :title="getCfDotTitle(row, dotIndex, dot)"
                />
              </div>
              <span v-if="Number(row.cf_recent_total || 0) > 0" class="mono cf-heat-stat">{{ formatCfStat(row) }}</span>
              <span v-else class="note-empty">-</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="健康度" width="210" align="center">
          <template #default="{ row }">
            <span v-if="isUnknownRow(row)" class="note-empty">-</span>
            <div v-else class="health-cell">
              <el-tag size="small" effect="light" :type="getHealthTagType(row)">
                {{ formatHealthScore(row.check_health_score) }}
              </el-tag>
              <div class="health-meta">
                <span>{{ formatRiskLevel(row.check_risk_level) }}</span>
                <span class="check-split">·</span>
                <span>{{ getRiskHitCount(row) }} 命中</span>
                <el-tag v-if="isReusedRow(row)" size="small" effect="plain" type="info">复用</el-tag>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="检测" min-width="280">
          <template #default="{ row }">
            <span v-if="isUnknownRow(row)" class="check-error check-empty">-</span>
            <div v-else class="check-cell">
              <el-tag
                size="small"
                effect="light"
                :type="row.check_status === 'success' ? 'success' : row.check_status === 'failed' ? 'danger' : 'info'"
              >
                {{ formatCheckStatusTag(row) }}
              </el-tag>
              <span v-if="getRuntimeHint(row)" class="check-runtime">{{ getRuntimeHint(row) }}</span>
              <div class="check-at">{{ row.check_at || '-' }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170" align="center">
          <template #default="{ row }">
            <span class="mono">{{ isUnknownRow(row) ? '-' : row.updated_at }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="pager-bar">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        layout="prev, pager, next, sizes, total"
        :page-sizes="[50, 100, 200, 500]"
        @current-change="handlePageChange"
        @size-change="handlePageSizeChange"
      />
    </div>

    <el-dialog v-model="importDialogVisible" title="批量导入代理" width="760px">
      <el-form :model="importForm" label-width="110px">
        <el-form-item label="默认类型">
          <el-select v-model="importForm.default_type" class="w-180">
            <el-option label="http" value="http" />
            <el-option label="https" value="https" />
            <el-option label="socks5" value="socks5" />
            <el-option label="ssh" value="ssh" />
          </el-select>
        </el-form-item>
        <el-form-item label="统一 Tag">
          <el-input v-model="importForm.tag" placeholder="可留空" />
        </el-form-item>
        <el-form-item label="统一备注">
          <el-input v-model="importForm.note" placeholder="可留空" />
        </el-form-item>
        <el-form-item label="代理文本">
          <el-input
            v-model="importForm.text"
            type="textarea"
            :rows="10"
            placeholder="每行一个代理：\n1) ip:port\n2) ip:port:user:pass\n3) http://user:pass@ip:port\n# 开头为注释行"
          />
        </el-form-item>
      </el-form>

      <div v-if="importResult" class="import-result">
        <div class="import-summary">
          导入结果：新增 {{ importResult.created }}，更新 {{ importResult.updated }}，跳过 {{ importResult.skipped }}
        </div>
        <el-alert
          v-if="Array.isArray(importResult.errors) && importResult.errors.length"
          type="warning"
          :closable="false"
          title="部分行解析失败（仅展示前 10 条）"
        >
          <template #default>
            <pre class="import-errors">{{ importResult.errors.slice(0, 10).join('\n') }}</pre>
          </template>
        </el-alert>
      </div>

      <template #footer>
        <el-button @click="importDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="importing" @click="doImport">导入</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="updateDialogVisible" title="批量更新代理" width="720px">
      <div class="dialog-tip">
        仅对已选代理生效。勾选字段后才会更新对应值；不勾选则保持不变。
      </div>
      <el-form label-width="110px">
        <el-form-item label="代理类型">
          <div class="field-row">
            <el-checkbox v-model="updateMask.proxy_type">修改</el-checkbox>
            <el-select v-model="updateForm.proxy_type" class="w-180" :disabled="!updateMask.proxy_type">
              <el-option label="http" value="http" />
              <el-option label="https" value="https" />
              <el-option label="socks5" value="socks5" />
              <el-option label="ssh" value="ssh" />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="账号">
          <div class="field-row">
            <el-checkbox v-model="updateMask.proxy_user">修改</el-checkbox>
            <el-input v-model="updateForm.proxy_user" :disabled="!updateMask.proxy_user" placeholder="允许为空以清空" />
          </div>
        </el-form-item>
        <el-form-item label="密码">
          <div class="field-row">
            <el-checkbox v-model="updateMask.proxy_password">修改</el-checkbox>
            <el-input
              v-model="updateForm.proxy_password"
              :disabled="!updateMask.proxy_password"
              placeholder="允许为空以清空"
              show-password
            />
          </div>
        </el-form-item>
        <el-form-item label="Tag">
          <div class="field-row">
            <el-checkbox v-model="updateMask.tag">修改</el-checkbox>
            <el-input v-model="updateForm.tag" :disabled="!updateMask.tag" placeholder="允许为空以清空" />
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <div class="field-row">
            <el-checkbox v-model="updateMask.note">修改</el-checkbox>
            <el-input v-model="updateForm.note" :disabled="!updateMask.note" placeholder="允许为空以清空" />
          </div>
        </el-form-item>
        <el-form-item label="同步到 ixBrowser">
          <el-switch v-model="updateForm.sync_to_ixbrowser" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="updateDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="updating" @click="doBatchUpdate">提交</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="checkDialogVisible" title="批量检测代理" width="680px">
      <div class="dialog-tip">
        后端将通过代理调用 ipapi + proxycheck 检测出口与风险标签；ssh 类型不支持检测。
      </div>
      <el-form :model="checkForm" label-width="110px">
        <el-form-item label="并发">
          <el-input-number v-model="checkForm.concurrency" :min="1" :max="100" />
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="checkForm.timeout_sec" :min="1" :max="60" :step="0.5" />
        </el-form-item>
        <el-form-item label="强制刷新">
          <el-switch v-model="checkForm.force_refresh" />
        </el-form-item>
        <div class="check-hint">关闭后 30 天内优先复用历史成功检测结果。</div>
      </el-form>

      <template #footer>
        <el-button @click="checkDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="checking" @click="doBatchCheck">开始检测</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  batchCheckProxies,
  batchImportProxies,
  batchUpdateProxies,
  getProxyCfEvents,
  getUnknownProxyCfEvents,
  listProxies,
  syncPullProxies,
  syncPushProxies
} from '../api'

const loading = ref(false)
const pulling = ref(false)
const pushing = ref(false)
const importing = ref(false)
const updating = ref(false)
const checking = ref(false)

const keyword = ref('')
const page = ref(1)
const pageSize = ref(50)
const total = ref(0)
const rows = ref([])
const cfRecentWindow = ref(30)
const selectedIds = ref([])
const checkMetaById = ref({})
const cfEventCache = ref({})
const cfEventLoading = ref({})
const cfEventError = ref({})

const importDialogVisible = ref(false)
const importForm = ref({
  text: '',
  default_type: 'http',
  tag: '',
  note: ''
})
const importResult = ref(null)

const updateDialogVisible = ref(false)
const updateMask = ref({
  proxy_type: false,
  proxy_user: false,
  proxy_password: false,
  tag: false,
  note: false
})
const updateForm = ref({
  proxy_type: 'http',
  proxy_user: '',
  proxy_password: '',
  tag: '',
  note: '',
  sync_to_ixbrowser: false
})

const checkDialogVisible = ref(false)
const checkForm = ref({
  concurrency: 20,
  timeout_sec: 8.0,
  force_refresh: true
})

const shorten = (text, maxLen = 60) => {
  const raw = String(text || '')
  if (raw.length <= maxLen) return raw
  return raw.slice(0, maxLen - 1) + '…'
}

const isUnknownRow = (row) => Boolean(row?.__unknown_proxy)

const isSelectableRow = (row) => !isUnknownRow(row)

const formatProxyDisplay = (row) => {
  if (isUnknownRow(row)) return '未知代理（无法关联本地代理）'
  const ip = String(row?.proxy_ip || '').trim()
  const port = String(row?.proxy_port || '').trim()
  const user = String(row?.proxy_user || '').trim()
  const password = String(row?.proxy_password || '').trim()
  if (!ip || !port) return '-'
  if (!user && !password) return `${ip}:${port}`
  return [ip, port, user, password].join(':')
}

const copyProxyText = async (row) => {
  const text = formatProxyDisplay(row)
  if (!text || text === '-') {
    ElMessage.warning('暂无可复制内容')
    return
  }
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.focus()
      textarea.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(textarea)
      if (!ok) throw new Error('copy command failed')
    }
    ElMessage.success('已复制代理')
  } catch {
    ElMessage.error('复制失败，请手动复制')
  }
}

const COUNTRY_NAME_CODE_MAP = {
  'united states': 'US',
  'usa': 'US',
  'united kingdom': 'GB',
  uk: 'GB',
  russia: 'RU',
  russian: 'RU',
  china: 'CN',
  japan: 'JP',
  korea: 'KR',
  'south korea': 'KR',
  singapore: 'SG',
  germany: 'DE',
  france: 'FR',
  canada: 'CA',
  australia: 'AU',
  india: 'IN',
  brazil: 'BR',
  netherlands: 'NL',
  hongkong: 'HK',
  'hong kong': 'HK',
  taiwan: 'TW',
  spain: 'ES',
  italy: 'IT',
  sweden: 'SE',
  norway: 'NO',
  denmark: 'DK',
  finland: 'FI'
}

const countryCodeToFlag = (code) => {
  const normalized = String(code || '').trim().toUpperCase()
  if (!/^[A-Z]{2}$/.test(normalized)) return '🌐'
  const chars = [...normalized].map((c) => String.fromCodePoint(127397 + c.charCodeAt(0)))
  return chars.join('')
}

const getCountryCode = (country) => {
  const text = String(country || '').trim()
  if (!text) return ''
  if (/^[A-Za-z]{2}$/.test(text)) return text.toUpperCase()
  const key = text.toLowerCase()
  return COUNTRY_NAME_CODE_MAP[key] || ''
}

const getCountryFlag = (country) => countryCodeToFlag(getCountryCode(country))

const formatPercent = (value) => {
  const num = Number(value || 0)
  if (!Number.isFinite(num)) return '0'
  const fixed = num.toFixed(1)
  return fixed.endsWith('.0') ? fixed.slice(0, -2) : fixed
}

const formatCfStat = (row) => {
  const count = Number(row?.cf_recent_count || 0)
  const totalCount = Number(row?.cf_recent_total || 0)
  if (!Number.isFinite(totalCount) || totalCount <= 0) return '-'
  return `${count}/${totalCount}(${formatPercent(row?.cf_recent_ratio)}%)`
}

const resetCfEventState = () => {
  cfEventCache.value = {}
  cfEventLoading.value = {}
  cfEventError.value = {}
}

const getCfEventCacheKey = (row) => {
  if (isUnknownRow(row)) return 'unknown'
  const id = Number(row?.id || 0)
  if (!Number.isFinite(id) || id <= 0) return ''
  return `proxy:${id}`
}

const getCfHeatDots = (row) => {
  const windowSize = Math.max(1, Number(cfRecentWindow.value || 30))
  const heatText = String(row?.cf_recent_heat || '').toUpperCase()
  const chars = heatText
    .split('')
    .filter((char) => char === 'C' || char === 'P' || char === '-')
    .slice(-windowSize)
  if (chars.length < windowSize) {
    return [...Array(windowSize - chars.length).fill('-'), ...chars]
  }
  return chars
}

const getCfHeatDotClass = (dot) => {
  if (dot === 'C') return 'cf-heat-dot--c'
  if (dot === 'P') return 'cf-heat-dot--p'
  return 'cf-heat-dot--empty'
}

const getCfEventByDotIndex = (row, dotIndex) => {
  const key = getCfEventCacheKey(row)
  if (!key) return null
  const events = cfEventCache.value[key]
  if (!Array.isArray(events) || !events.length) return null

  const windowSize = Math.max(1, Number(cfRecentWindow.value || 30))
  const count = Math.min(events.length, windowSize)
  const padCount = Math.max(windowSize - count, 0)
  if (dotIndex < padCount) return null

  const offsetFromOldest = dotIndex - padCount
  const indexInNewestFirst = count - 1 - offsetFromOldest
  if (indexInNewestFirst < 0 || indexInNewestFirst >= count) return null
  return events[indexInNewestFirst] || null
}

const getCfDotTitle = (row, dotIndex, dot) => {
  const base = dot === 'C' ? 'CF 命中' : dot === 'P' ? '通过' : '无记录'
  if (dot === '-') return base

  const key = getCfEventCacheKey(row)
  if (!key) return base
  if (cfEventError.value[key]) return `${base}\n详情加载失败`
  if (cfEventLoading.value[key] && !Array.isArray(cfEventCache.value[key])) return `${base}\n加载中`

  const event = getCfEventByDotIndex(row, dotIndex)
  if (!event) return `${base}\n无详情`
  return [
    `结果: ${base}`,
    `时间: ${event?.created_at || '-'}`,
    `来源: ${event?.source || '-'}`,
    `Endpoint: ${shorten(event?.endpoint || '-', 120)}`,
    `Status: ${event?.status_code ?? '-'}`,
    `Error: ${event?.error_text || '-'}`
  ].join('\n')
}

const handleCfHeatMouseEnter = async (row) => {
  const key = getCfEventCacheKey(row)
  if (!key) return
  if (Array.isArray(cfEventCache.value[key])) return
  if (cfEventLoading.value[key]) return

  cfEventLoading.value = { ...cfEventLoading.value, [key]: true }
  cfEventError.value = { ...cfEventError.value, [key]: false }
  try {
    const windowSize = Math.max(1, Number(cfRecentWindow.value || 30))
    const data = isUnknownRow(row)
      ? await getUnknownProxyCfEvents(windowSize)
      : await getProxyCfEvents(Number(row?.id || 0), windowSize)
    const events = Array.isArray(data?.events) ? data.events : []
    cfEventCache.value = { ...cfEventCache.value, [key]: events }
  } catch {
    cfEventError.value = { ...cfEventError.value, [key]: true }
  } finally {
    cfEventLoading.value = { ...cfEventLoading.value, [key]: false }
  }
}

const parseRiskFlags = (row) => {
  const raw = row?.check_risk_flags
  if (Array.isArray(raw)) return raw.filter((x) => String(x || '').trim())
  const text = String(raw || '').trim()
  if (!text) return []
  try {
    const parsed = JSON.parse(text)
    if (!Array.isArray(parsed)) return []
    return parsed.map((x) => String(x || '').trim()).filter(Boolean)
  } catch {
    return []
  }
}

const getRiskHitCount = (row) => parseRiskFlags(row).length

const formatHealthScore = (value) => {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  const score = Math.max(0, Math.min(100, Math.round(num)))
  return `${score}`
}

const formatRiskLevel = (value) => {
  const level = String(value || '').trim().toLowerCase()
  if (level === 'low') return '低风险'
  if (level === 'medium') return '中风险'
  if (level === 'high') return '高风险'
  return '-'
}

const getHealthTagType = (row) => {
  const level = String(row?.check_risk_level || '').trim().toLowerCase()
  if (level === 'low') return 'success'
  if (level === 'medium') return 'warning'
  if (level === 'high') return 'danger'
  return 'info'
}

const getCheckMeta = (row) => {
  const id = Number(row?.id || 0)
  if (!Number.isFinite(id) || id <= 0) return null
  return checkMetaById.value[id] || null
}

const isReusedRow = (row) => Boolean(getCheckMeta(row)?.reused)

const getRuntimeHint = (row) => {
  const meta = getCheckMeta(row)
  if (!meta) return ''
  if (meta.quota_limited) return '本次触发额度限制，沿用旧值'
  if (meta.reused) return '本次复用历史检测结果'
  if (meta.error) return String(meta.error || '')
  return ''
}

const formatCheckErrorCode = (errorText) => {
  const text = String(errorText || '').trim()
  if (!text) return 'ERROR'
  const lower = text.toLowerCase()
  if (lower.includes('timeout') || lower.includes('timed out') || text.includes('超时')) return 'TIMEOUT'
  if (lower.includes('quota') || lower.includes('rate limit') || lower.includes('too many requests') || text.includes('超限')) return 'LIMIT'
  if (lower.includes('proxy authentication') || lower.includes('auth') || text.includes('认证') || lower.includes('407')) return 'AUTH'
  if (text.includes('不支持检测')) return 'UNSUPPORTED'
  const statusMatch = lower.match(/(?:http|状态码)\s*[: ]?(\d{3})/)
  if (statusMatch?.[1]) return statusMatch[1]
  const directCodeMatch = lower.match(/\b(4\d{2}|5\d{2})\b/)
  if (directCodeMatch?.[1]) return directCodeMatch[1]
  return 'ERROR'
}

const formatCheckStatusTag = (row) => {
  const status = String(row?.check_status || 'unknown').trim().toLowerCase()
  if (status === 'failed') {
    return `failed/${formatCheckErrorCode(row?.check_error)}`
  }
  return status || 'unknown'
}

const loadList = async () => {
  loading.value = true
  try {
    const data = await listProxies({
      keyword: keyword.value || null,
      page: page.value,
      limit: pageSize.value
    })
    cfRecentWindow.value = Math.max(1, Number(data?.cf_recent_window || 30))
    const items = Array.isArray(data?.items) ? data.items : []
    const unknownCount = Number(data?.unknown_cf_recent_count || 0)
    const unknownTotal = Number(data?.unknown_cf_recent_total || 0)
    const unknownRatio = Number(data?.unknown_cf_recent_ratio || 0)
    const unknownHeat = String(data?.unknown_cf_recent_heat || '')
    if (unknownTotal > 0) {
      const unknownRow = {
        id: 'unknown-proxy',
        __unknown_proxy: true,
        ix_id: null,
        proxy_type: 'unknown',
        proxy_ip: '',
        proxy_port: '',
        proxy_user: '',
        proxy_password: '',
        tag: null,
        note: null,
        check_status: null,
        check_error: null,
        check_ip: null,
        check_country: null,
        check_city: null,
        check_timezone: null,
        check_at: null,
        check_health_score: null,
        check_risk_level: null,
        check_risk_flags: null,
        check_proxycheck_type: null,
        check_proxycheck_risk: null,
        check_is_proxy: null,
        check_is_vpn: null,
        check_is_tor: null,
        check_is_datacenter: null,
        check_is_abuser: null,
        created_at: '-',
        updated_at: '-',
        cf_recent_count: unknownCount,
        cf_recent_total: unknownTotal,
        cf_recent_ratio: unknownRatio,
        cf_recent_heat: unknownHeat
      }
      rows.value = [unknownRow, ...items]
    } else {
      rows.value = items
    }
    resetCfEventState()
    total.value = Number(data?.total || 0)
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  page.value = 1
  loadList()
}

const handlePageChange = () => {
  loadList()
}

const handlePageSizeChange = () => {
  page.value = 1
  loadList()
}

const handleSelectionChange = (selection) => {
  const ids = []
  const rowsArr = Array.isArray(selection) ? selection : []
  rowsArr.forEach((r) => {
    const id = Number(r?.id || 0)
    if (Number.isFinite(id) && id > 0) ids.push(id)
  })
  selectedIds.value = ids
}

const openImportDialog = () => {
  importResult.value = null
  importDialogVisible.value = true
}

const doImport = async () => {
  const text = String(importForm.value.text || '').trim()
  if (!text) {
    ElMessage.warning('请输入代理文本')
    return
  }
  importing.value = true
  try {
    const payload = {
      text,
      default_type: importForm.value.default_type || 'http',
      tag: String(importForm.value.tag || '').trim() || null,
      note: String(importForm.value.note || '').trim() || null
    }
    const resp = await batchImportProxies(payload)
    importResult.value = resp
    ElMessage.success(`导入完成：新增 ${resp.created || 0}，更新 ${resp.updated || 0}，跳过 ${resp.skipped || 0}`)
    await loadList()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '导入失败')
  } finally {
    importing.value = false
  }
}

const syncPull = async () => {
  try {
    await ElMessageBox.confirm(
      '该操作会从 ixBrowser 拉取代理并覆盖本地同 ix_id 的记录（本地为主库的数据仍会保留，但会被同步更新）。确定继续？',
      '从 ixBrowser 同步',
      { type: 'warning', confirmButtonText: '继续', cancelButtonText: '取消' }
    )
  } catch {
    return
  }

  pulling.value = true
  try {
    const resp = await syncPullProxies()
    ElMessage.success(`同步完成：新增 ${resp.created || 0}，更新 ${resp.updated || 0}，总计 ${resp.total || 0}`)
    page.value = 1
    await loadList()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '同步失败')
  } finally {
    pulling.value = false
  }
}

const syncPush = async () => {
  if (!selectedIds.value.length) return
  try {
    await ElMessageBox.confirm(
      `将所选 ${selectedIds.value.length} 条代理同步到 ixBrowser（匹配则更新/绑定，不存在则创建）。继续？`,
      '同步到 ixBrowser',
      { type: 'warning', confirmButtonText: '继续', cancelButtonText: '取消' }
    )
  } catch {
    return
  }

  pushing.value = true
  try {
    const resp = await syncPushProxies({ proxy_ids: selectedIds.value })
    const ok = (resp?.results || []).filter((r) => r.ok).length
    const fail = (resp?.results || []).filter((r) => !r.ok).length
    ElMessage.success(`同步完成：成功 ${ok}，失败 ${fail}`)
    await loadList()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '同步失败')
  } finally {
    pushing.value = false
  }
}

const openUpdateDialog = () => {
  if (!selectedIds.value.length) return
  updateDialogVisible.value = true
}

const doBatchUpdate = async () => {
  if (!selectedIds.value.length) return
  const payload = { proxy_ids: selectedIds.value, sync_to_ixbrowser: !!updateForm.value.sync_to_ixbrowser }
  if (updateMask.value.proxy_type) payload.proxy_type = updateForm.value.proxy_type || 'http'
  if (updateMask.value.proxy_user) payload.proxy_user = String(updateForm.value.proxy_user || '')
  if (updateMask.value.proxy_password) payload.proxy_password = String(updateForm.value.proxy_password || '')
  if (updateMask.value.tag) payload.tag = String(updateForm.value.tag || '')
  if (updateMask.value.note) payload.note = String(updateForm.value.note || '')

  if (
    !updateMask.value.proxy_type &&
    !updateMask.value.proxy_user &&
    !updateMask.value.proxy_password &&
    !updateMask.value.tag &&
    !updateMask.value.note
  ) {
    ElMessage.warning('请至少勾选一个要修改的字段')
    return
  }

  updating.value = true
  try {
    const resp = await batchUpdateProxies(payload)
    const ok = (resp?.results || []).filter((r) => r.ok).length
    const fail = (resp?.results || []).filter((r) => !r.ok).length
    ElMessage.success(`更新完成：成功 ${ok}，失败 ${fail}`)
    updateDialogVisible.value = false
    await loadList()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '更新失败')
  } finally {
    updating.value = false
  }
}

const openCheckDialog = () => {
  if (!selectedIds.value.length) return
  checkDialogVisible.value = true
}

const doBatchCheck = async () => {
  if (!selectedIds.value.length) return
  checking.value = true
  try {
    const payload = {
      proxy_ids: selectedIds.value,
      concurrency: Number(checkForm.value.concurrency || 20),
      timeout_sec: Number(checkForm.value.timeout_sec || 8.0),
      force_refresh: !!checkForm.value.force_refresh
    }
    const resp = await batchCheckProxies(payload)
    const results = Array.isArray(resp?.results) ? resp.results : []
    const runtimeMeta = {}
    results.forEach((item) => {
      const pid = Number(item?.proxy_id || 0)
      if (!Number.isFinite(pid) || pid <= 0) return
      runtimeMeta[pid] = {
        reused: !!item?.reused,
        quota_limited: !!item?.quota_limited,
        error: String(item?.error || '')
      }
    })
    checkMetaById.value = { ...checkMetaById.value, ...runtimeMeta }
    const reused = results.filter((r) => r?.reused).length
    const quota = results.filter((r) => r?.quota_limited).length
    const ok = results.filter((r) => r?.ok && !r?.reused).length
    const fail = results.filter((r) => !r?.ok && !r?.quota_limited).length
    ElMessage.success(`检测完成：成功 ${ok}，失败 ${fail}，复用 ${reused}，超限 ${quota}`)
    checkDialogVisible.value = false
    await loadList()
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '检测失败')
  } finally {
    checking.value = false
  }
}

onMounted(() => {
  loadList()
})
</script>

<style scoped>
.proxy-page {
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
}

.pager-bar {
  display: flex;
  justify-content: flex-end;
  padding: 6px 4px 0;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
}

.addr-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
}

.addr-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.addr-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.copy-btn {
  flex: 0 0 auto;
  padding: 0;
}

.addr-meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}

.country-flag {
  font-size: 13px;
  line-height: 1;
}

.note-text {
  color: #334155;
}

.note-empty {
  color: #94a3b8;
}

.cf-heat-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.cf-heat-grid {
  --cf-window: 30;
  display: grid;
  grid-template-columns: repeat(var(--cf-window), 5px);
  gap: 2px;
  justify-content: center;
}

.cf-heat-dot {
  width: 5px;
  height: 14px;
  border-radius: 2px;
}

.cf-heat-dot--c {
  background: #ef4444;
}

.cf-heat-dot--p {
  background: #22c55e;
}

.cf-heat-dot--empty {
  background: #dbe4ee;
}

.cf-heat-stat {
  line-height: 1;
}

.check-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.health-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
}

.health-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #475569;
}

.check-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  color: #475569;
}

.check-split {
  color: rgba(15, 23, 42, 0.25);
}

.check-error {
  font-size: 12px;
  color: rgba(148, 163, 184, 1);
}

.check-empty {
  color: rgba(148, 163, 184, 1);
}

.check-runtime {
  font-size: 12px;
  color: #0f766e;
}

.check-at {
  font-size: 12px;
  color: rgba(100, 116, 139, 1);
}

.check-hint {
  margin-top: 4px;
  padding-left: 110px;
  font-size: 12px;
  color: #64748b;
}

.import-result {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.import-summary {
  font-size: 13px;
  color: #334155;
  font-weight: 600;
}

.import-errors {
  margin: 8px 0 0;
  white-space: pre-wrap;
  font-size: 12px;
  color: #475569;
}

.dialog-tip {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid rgba(148, 163, 184, 0.24);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.8);
  color: #475569;
  font-size: 12px;
  line-height: 1.5;
}

.field-row {
  display: flex;
  gap: 10px;
  align-items: center;
  width: 100%;
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
</style>
