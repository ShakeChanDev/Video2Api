<template>
  <div class="tasks-page">
    <section class="command-bar" v-loading="loading">
      <div class="command-left">
        <div class="brand">
          <div class="title">Sora 任务指挥台</div>
          <div class="subtitle">生成 → 进度 → GenID → 发布</div>
        </div>
        <div class="filters">
          <el-select v-model="selectedGroupTitle" class="w-180" @change="handleRealtimeFilterChange">
            <el-option
              v-for="group in groups"
              :key="group.id"
              :label="`${group.title} (ID:${group.id})`"
              :value="group.title"
            />
          </el-select>

          <el-select v-model="statusFilter" class="w-140" @change="handleRealtimeFilterChange">
            <el-option label="全部状态" value="all" />
            <el-option label="排队中" value="queued" />
            <el-option label="运行中" value="running" />
            <el-option label="成功" value="completed" />
            <el-option label="失败" value="failed" />
            <el-option label="已取消" value="canceled" />
          </el-select>

          <el-select v-model="phaseFilter" class="w-140" @change="handleRealtimeFilterChange">
            <el-option label="全部阶段" value="all" />
            <el-option label="排队" value="queue" />
            <el-option label="提交" value="submit" />
            <el-option label="进度" value="progress" />
            <el-option label="GenID" value="genid" />
            <el-option label="发布" value="publish" />
            <el-option label="去水印" value="watermark" />
            <el-option label="完成" value="done" />
          </el-select>

          <el-input
            v-model="keyword"
            class="w-260"
            clearable
            placeholder="搜索 Job/Task/GenID/Prompt"
            @clear="handleRealtimeFilterChange"
            @keyup.enter="handleRealtimeFilterChange"
          />
        </div>
      </div>

      <div class="command-right">
        <div class="concurrency">
          <div class="metric">
            <span class="label">并发</span>
            <span class="value">{{ runningCount }}/{{ concurrencyLimit }}</span>
          </div>
          <div class="metric">
            <span class="label">排队</span>
            <span class="value">{{ queuedCount }}</span>
          </div>
        </div>
        <div class="actions">
          <el-button @click="handleRealtimeFilterChange">刷新</el-button>
          <el-button type="primary" @click="openCreateDialog('sora')">新建任务</el-button>
          <el-button type="primary" plain @click="openCreateDialog('video_api')">新建任务（/v1/videos）</el-button>
        </div>
      </div>
    </section>

    <section class="overview">
      <div class="overview-card">
        <div class="card-title">排队中</div>
        <div class="card-value">{{ queuedCount }}</div>
        <div class="card-meta">等待进入执行</div>
      </div>
      <div class="overview-card">
        <div class="card-title">运行中</div>
        <div class="card-value">{{ runningCount }}</div>
        <div class="card-meta">正在生成或发布</div>
      </div>
      <div class="overview-card">
        <div class="card-title">已完成</div>
        <div class="card-value">{{ completedCount }}</div>
        <div class="card-meta">已获取分享链接</div>
      </div>
      <div class="overview-card danger">
        <div class="card-title">失败</div>
        <div class="card-value">{{ failedCount }}</div>
        <div class="card-meta">可从失败阶段重试</div>
      </div>
    </section>

    <el-card class="table-card" v-loading="loading">
      <template #header>
        <div class="table-head">
          <span>任务列表</span>
          <span class="table-hint">自动发布 · 只用 GenID 进入 /d/gen_xxx</span>
        </div>
      </template>

      <el-table :data="filteredJobs" class="card-table task-table">
        <el-table-column label="任务" min-width="480">
          <template #default="{ row }">
            <div class="task-cell">
              <div class="task-title">
                <span class="task-id">#{{ row.job_id }}</span>
                <span class="task-dot" />
                <span class="task-window">
                  窗口 {{ row.profile_id }}
                  <span v-if="isPlusProfile(row.profile_id)" class="task-plus">Plus</span>
                </span>
                <template v-if="row.proxy_ip && row.proxy_port">
                  <span class="task-dot" />
                  <span class="task-proxy">{{ formatProxy(row) }}</span>
                </template>
                <span class="task-dot" />
                <span class="task-meta">{{ row.duration }}</span>
                <span class="task-dot" />
                <span class="task-meta">{{ row.aspect_ratio }}</span>
                <span class="task-dot" />
                <span class="task-time">{{ formatTaskTime(row.created_at) }}</span>
              </div>
              <el-tooltip v-if="row.prompt" :content="row.prompt" placement="top" effect="dark">
                <span class="task-prompt">{{ row.prompt }}</span>
              </el-tooltip>
              <span v-else class="task-prompt task-prompt-empty">-</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="240">
          <template #default="{ row }">
            <div class="status-cell">
              <div class="status-head">
                <el-tag size="small" :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
                <span class="status-phase">{{ phaseText(row.phase) }}</span>
              </div>
              <el-progress
                v-if="row.status === 'running'"
                class="status-progress"
                :percentage="row.progress_pct"
                :status="progressStatus(row)"
                :stroke-width="8"
                :text-inside="true"
              />
              <el-tooltip
                v-if="jobErrorSummary(row)"
                :content="jobErrorSummary(row)"
                placement="top"
                effect="dark"
              >
                <span class="status-error">{{ shorten(jobErrorSummary(row), 72) }}</span>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="输出" min-width="300">
          <template #default="{ row }">
            <div class="output-cell">
              <div class="output-line">
                <a
                  v-if="row.publish_url"
                  class="share-code-link"
                  href="#"
                  :title="extractShareCode(row.publish_url)"
                  @click.prevent="openLink(row.publish_url)"
                >
                  {{ extractShareCode(row.publish_url) }}
                </a>
                <span v-else class="output-empty" />
              </div>
              <div class="output-line">
                <a
                  v-if="row.watermark_url"
                  class="share-code-link watermark-link"
                  href="#"
                  :title="extractShareCode(row.watermark_url)"
                  @click.prevent="openLink(row.watermark_url)"
                >
                  {{ extractShareCode(row.watermark_url) }}
                </a>
                <template v-else-if="row.watermark_status === 'failed'">
                  <span class="output-error">去水印失败</span>
                  <el-button
                    v-if="canRetryWatermark(row)"
                    size="small"
                    class="btn-soft"
                    @click="retryWatermark(row)"
                  >
                    重试
                  </el-button>
                </template>
                <span v-else class="output-empty" />
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" fixed="right" width="150">
          <template #default="{ row }">
            <div class="action-cell">
              <el-button size="small" class="btn-soft" @click="openDetail(row)">详情</el-button>
              <el-button
                v-if="canRetryJob(row)"
                size="small"
                type="warning"
                @click="retryJob(row)"
              >
                重试
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="createDialogVisible"
      :title="createEntryMode === 'video_api' ? '新建任务（/v1/videos）' : '新建任务'"
      width="620px"
    >
      <el-form :model="createForm" label-width="90px">
        <template v-if="createEntryMode === 'sora'">
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
          <el-form-item label="账号">
            <div class="dispatch-mode">
              <el-radio-group v-model="createForm.dispatch_mode">
                <el-radio-button label="weighted_auto">自动分配</el-radio-button>
                <el-radio-button label="manual">手动指定</el-radio-button>
              </el-radio-group>
              <div class="dispatch-tip">自动分配会优先选择高权重账号（可用次数 + 账号质量）。</div>
            </div>
          </el-form-item>

          <template v-if="createForm.dispatch_mode === 'manual'">
            <el-form-item label="窗口ID">
              <el-input-number v-model="createForm.profile_id" style="width: 100%" :min="1" />
            </el-form-item>
          </template>
          <template v-else>
            <el-form-item label="推荐账号">
              <div class="weights-panel" v-loading="weightsLoading">
                <el-table v-if="accountWeights.length" :data="accountWeights" size="small" class="weights-table">
                  <el-table-column label="窗口" width="110">
                    <template #default="{ row }">
                      <span class="mono">#{{ row.profile_id }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="总分" width="90" align="center">
                    <template #default="{ row }">
                      <span class="score">{{ row.score_total }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="可用" width="80" align="center">
                    <template #default="{ row }">
                      <span>{{ row.quota_remaining_count ?? '-' }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="说明" min-width="200">
                    <template #default="{ row }">
                      <span class="weights-reason">{{ (row.reasons || []).slice(0, 2).join('；') || '-' }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="可选" width="80" align="center">
                    <template #default="{ row }">
                      <el-tag size="small" :type="row.selectable ? 'success' : 'info'">
                        {{ row.selectable ? '可用' : '不可用' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                </el-table>
                <div v-else class="weights-empty">暂无权重数据（可先去“账号管理”扫描一次）。</div>
              </div>
            </el-form-item>
          </template>
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
        </template>
        <template v-else>
          <el-form-item label="说明">
            <span class="create-mode-hint">该模式会调用 /v1/videos，账号由后端自动分配。</span>
          </el-form-item>
          <el-form-item label="型号">
            <el-select v-model="createForm.video_api_model" style="width: 100%">
              <el-option
                v-for="item in videoApiModelOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
          </el-form-item>
        </template>
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
        <el-form-item label="图片 URL">
          <el-input
            v-model="createForm.image_url"
            placeholder="可选，传入参考图片 URL"
            clearable
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitTask">创建</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailDrawerVisible" title="任务详情" size="620px" direction="rtl">
      <div class="detail-block" v-if="detailJob">
        <div class="detail-grid">
          <div class="detail-item"><span>Job</span><strong>#{{ detailJob.job_id }}</strong></div>
          <div class="detail-item"><span>窗口</span><strong>{{ detailJob.profile_id }}</strong></div>
          <div class="detail-item"><span>调度方式</span><strong>{{ detailJob.dispatch_mode || '-' }}</strong></div>
          <div class="detail-item"><span>调度总分</span><strong>{{ detailJob.dispatch_score ?? '-' }}</strong></div>
          <div class="detail-item"><span>调度原因</span><strong>{{ detailJob.dispatch_reason || '-' }}</strong></div>
          <div class="detail-item"><span>状态</span><strong>{{ detailJob.status }}</strong></div>
          <div class="detail-item"><span>阶段</span><strong>{{ detailJob.phase }}</strong></div>
          <div class="detail-item"><span>进度</span><strong>{{ detailJob.progress_pct }}%</strong></div>
          <div class="detail-item"><span>TaskID</span><strong>{{ detailJob.task_id || '-' }}</strong></div>
          <div class="detail-item"><span>GenID</span><strong>{{ detailJob.generation_id || '-' }}</strong></div>
          <div class="detail-item"><span>分享</span><strong>{{ detailJob.publish_url || '-' }}</strong></div>
          <div class="detail-item"><span>去水印</span><strong>{{ detailJob.watermark_status || '-' }}</strong></div>
          <div class="detail-item"><span>无水印链接</span><strong>{{ detailJob.watermark_url || '-' }}</strong></div>
          <div class="detail-item"><span>去水印重试次数</span><strong>{{ detailJob.watermark_attempts ?? 0 }}</strong></div>
          <div class="detail-item"><span>去水印错误</span><strong>{{ detailJob.watermark_error || '-' }}</strong></div>
          <div class="detail-item">
            <span>图片URL</span>
            <strong>
              <a v-if="detailJob.image_url" href="#" @click.prevent="openLink(detailJob.image_url)">
                {{ detailJob.image_url }}
              </a>
              <template v-else>-</template>
            </strong>
          </div>
        </div>
        <div class="detail-prompt">
          <div class="detail-label">Prompt</div>
          <div class="detail-text">{{ detailJob.prompt }}</div>
        </div>
      </div>
      <div class="detail-block">
        <div class="detail-label">阶段事件</div>
        <div v-if="detailEvents.length" class="event-list">
          <div v-for="event in detailEvents" :key="event.id" class="event-item">
            <div class="event-time">{{ formatTime(event.created_at) }}</div>
            <div class="event-meta">
              <span class="event-phase">{{ event.phase }}</span>
              <span class="event-action">{{ event.event }}</span>
            </div>
            <div class="event-message">{{ event.message || '-' }}</div>
          </div>
        </div>
        <div v-else class="event-empty">暂无事件记录</div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { formatRelativeTimeZh } from '../utils/relativeTime'
import {
  buildSoraJobStreamUrl,
  createVideoViaVideoApi,
  createSoraJob,
  getSoraAccountWeights,
  getIxBrowserGroupWindows,
  getLatestIxBrowserSoraSessionAccounts,
  getSystemSettings,
  listSoraJobEvents,
  listSoraJobs,
  retrySoraJob,
  retrySoraJobWatermark
} from '../api'

const loading = ref(false)
const submitting = ref(false)
const createDialogVisible = ref(false)
const detailDrawerVisible = ref(false)
const detailJob = ref(null)
const detailEvents = ref([])
const groups = ref([])
const jobs = ref([])
const systemSettings = ref(null)
const selectedGroupTitle = ref('Sora')
const statusFilter = ref('all')
const phaseFilter = ref('all')
const keyword = ref('')
let relativeTimeTimer = null
let realtimeSource = null
let realtimeReconnectTimer = null
let realtimeReconnectDelay = 1000
let fallbackPollingTimer = null
let allowRealtime = true
const nowTick = ref(Date.now())

const weightsLoading = ref(false)
const accountWeights = ref([])

const accountPlanMap = ref({})
const accountPlanGroupTitle = ref(null)
const accountPlanUpdatedAt = ref(0)
const ACCOUNT_PLAN_TTL_MS = 60 * 1000
let accountPlanLoading = false
const createEntryMode = ref('sora')

const videoApiModelOptions = [
  { value: 'sora2-landscape', label: 'sora2-landscape：横屏（电脑）10秒' },
  { value: 'sora2-portrait', label: 'sora2-portrait：竖屏（手机）10秒' },
  { value: 'sora2-landscape-15s', label: 'sora2-landscape-15s：横屏（电脑）15秒' },
  { value: 'sora2-portrait-15s', label: 'sora2-portrait-15s：竖屏（手机）15秒' }
]

const createForm = ref({
  group_title: 'Sora',
  dispatch_mode: 'weighted_auto',
  profile_id: null,
  prompt: '',
  image_url: '',
  duration: '10s',
  aspect_ratio: 'landscape',
  video_api_model: 'sora2-landscape'
})

const normalizeVideoApiModel = (value) => {
  const text = String(value || '').trim()
  if (videoApiModelOptions.some((item) => item.value === text)) {
    return text
  }
  return 'sora2-landscape'
}

const buildVideoApiModel = (aspectRatio, duration) => {
  const ratio = String(aspectRatio || '').trim().toLowerCase() === 'portrait' ? 'portrait' : 'landscape'
  const seconds = String(duration || '').trim().toLowerCase() === '15s' ? '15s' : '10s'
  if (seconds === '15s') {
    return `sora2-${ratio}-15s`
  }
  return `sora2-${ratio}`
}

const concurrencyLimit = computed(() => systemSettings.value?.sora?.job_max_concurrency || 2)

const queuedCount = computed(() => jobs.value.filter((item) => item.status === 'queued').length)
const runningCount = computed(() => jobs.value.filter((item) => item.status === 'running').length)
const completedCount = computed(() => jobs.value.filter((item) => item.status === 'completed').length)
const failedCount = computed(() => jobs.value.filter((item) => item.status === 'failed').length)

const filteredJobs = computed(() => {
  return jobs.value.filter((job) => {
    const byStatus = statusFilter.value === 'all' || job.status === statusFilter.value
    const byPhase = phaseFilter.value === 'all' || job.phase === phaseFilter.value
    const q = keyword.value.trim().toLowerCase()
    if (!q) {
      return byStatus && byPhase
    }
    const joined = [
      job.job_id,
      job.profile_id,
      job.task_id || '',
      job.generation_id || '',
      job.prompt || '',
      job.image_url || '',
      job.error || '',
      job.watermark_url || '',
      job.watermark_error || ''
    ]
      .join(' ')
      .toLowerCase()
    return byStatus && byPhase && joined.includes(q)
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

const formatTaskTime = (value) => formatRelativeTimeZh(value, nowTick.value)

const shorten = (value, maxLen) => {
  if (!value) return ''
  if (value.length <= maxLen) return value
  return `${value.slice(0, maxLen)}...`
}

const formatProxy = (row) => {
  const ip = String(row?.proxy_ip || '').trim()
  const port = String(row?.proxy_port || '').trim()
  if (!ip || !port) return '-'
  const ptype = String(row?.proxy_type || 'http').trim().toLowerCase() || 'http'
  const localId = Number(row?.proxy_local_id || 0)
  const suffix = localId > 0 ? `#${localId}` : ''
  return `代理 ${ptype} ${ip}:${port}${suffix ? ` (${suffix})` : ''}`
}

const statusType = (status) => {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'running') return 'warning'
  if (status === 'queued') return 'info'
  if (status === 'canceled') return 'warning'
  return ''
}

const statusText = (status) => {
  if (status === 'queued') return '排队中'
  if (status === 'running') return '执行中'
  if (status === 'completed') return '已完成'
  if (status === 'failed') return '失败'
  if (status === 'canceled') return '已取消'
  return status || '-'
}

const phaseText = (phase) => {
  if (phase === 'queue') return '排队'
  if (phase === 'submit') return '提交'
  if (phase === 'progress') return '进度'
  if (phase === 'genid') return '捕获 GenID'
  if (phase === 'publish') return '发布'
  if (phase === 'watermark') return '去水印'
  if (phase === 'done') return '完成'
  return phase || '-'
}

const progressStatus = (row) => {
  if (row.status === 'completed') return 'success'
  if (row.status === 'failed') return 'exception'
  if (row.status === 'canceled') return 'warning'
  return ''
}

const canRetryJob = (row) => row?.status === 'failed'

const canRetryWatermark = (row) => row?.watermark_status === 'failed' && Boolean(row?.publish_url)

const jobErrorSummary = (row) => {
  if (!row) return ''
  if (row.status === 'failed') return row.error || row.watermark_error || ''
  return ''
}

const extractShareCode = (url) => {
  if (!url) return '-'
  const raw = String(url).trim()
  if (!raw) return '-'

  const decodeSafe = (value) => {
    try {
      return decodeURIComponent(value)
    } catch {
      return value
    }
  }

  const text = decodeSafe(raw)
  const prefixed = text.match(/(s_[A-Za-z0-9]+)/i)
  if (prefixed?.[1]) {
    return prefixed[1]
  }

  try {
    const parsed = new URL(text)
    const segments = parsed.pathname.split('/').filter(Boolean)
    if (segments.length) {
      return segments[segments.length - 1]
    }
  } catch {
    const fallback = text.split('?')[0].split('#')[0].split('/').filter(Boolean)
    if (fallback.length) {
      return fallback[fallback.length - 1]
    }
  }
  return text
}

const loadAccountPlans = async (force = false) => {
  const groupTitle = selectedGroupTitle.value || 'Sora'
  const now = Date.now()
  const ttlOk =
    accountPlanGroupTitle.value === groupTitle && now - (accountPlanUpdatedAt.value || 0) < ACCOUNT_PLAN_TTL_MS

  if (accountPlanGroupTitle.value !== groupTitle) {
    accountPlanMap.value = {}
    accountPlanGroupTitle.value = groupTitle
    accountPlanUpdatedAt.value = 0
  }

  if (!force && ttlOk) return
  if (accountPlanLoading) return

  accountPlanLoading = true
  try {
    const data = await getLatestIxBrowserSoraSessionAccounts(groupTitle, true)
    const results = Array.isArray(data?.results) ? data.results : []
    const map = {}
    for (const item of results) {
      const pid = item?.profile_id
      if (pid === null || pid === undefined) continue
      const plan = String(item?.account_plan || '').trim().toLowerCase()
      if (plan) {
        map[String(pid)] = plan
      }
    }
    accountPlanMap.value = map
    accountPlanGroupTitle.value = groupTitle
    accountPlanUpdatedAt.value = Date.now()
  } catch {
    // 账号套餐信息仅用于 UI 标识：失败时静默降级即可
    accountPlanUpdatedAt.value = Date.now()
  } finally {
    accountPlanLoading = false
  }
}

const isPlusProfile = (profileId) => {
  if (profileId === null || profileId === undefined) return false
  const plan = accountPlanMap.value?.[String(profileId)]
  if (!plan) return false
  return String(plan).toLowerCase().includes('plus')
}

const applySystemDefaults = () => {
  const defaults = systemSettings.value?.sora || {}
  if (defaults.default_group_title) {
    selectedGroupTitle.value = defaults.default_group_title
    createForm.value.group_title = defaults.default_group_title
  }
  if (defaults.default_duration) {
    createForm.value.duration = defaults.default_duration
  }
  if (defaults.default_aspect_ratio) {
    createForm.value.aspect_ratio = defaults.default_aspect_ratio
  }
  createForm.value.video_api_model = normalizeVideoApiModel(
    buildVideoApiModel(createForm.value.aspect_ratio, createForm.value.duration)
  )
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

const normalizeProgress = (item) => {
  const raw = item?.progress_pct
  if (typeof raw === 'number' && !Number.isNaN(raw)) {
    if (raw <= 1) {
      return Math.min(100, Math.max(0, Math.round(raw * 100)))
    }
    return Math.min(100, Math.max(0, Math.round(raw)))
  }
  if (item?.status === 'completed') return 100
  return 0
}

const normalizeJobs = (data) => {
  return (Array.isArray(data) ? data : []).map((item) => ({
    ...item,
    progress_pct: normalizeProgress(item)
  }))
}

const syncDetailJob = () => {
  const current = detailJob.value
  if (!current?.job_id) return
  const currentId = Number(current.job_id || 0)
  if (!currentId) return
  const matched = jobs.value.find((item) => Number(item?.job_id || 0) === currentId)
  if (matched) {
    detailJob.value = { ...matched }
  }
}

const upsertJob = (item) => {
  const normalized = normalizeJobs([item])[0]
  if (!normalized?.job_id) return
  const targetId = Number(normalized.job_id || 0)
  if (!targetId) return
  const idx = jobs.value.findIndex((job) => Number(job?.job_id || 0) === targetId)
  if (idx >= 0) {
    jobs.value.splice(idx, 1, normalized)
  } else {
    jobs.value.unshift(normalized)
    if (jobs.value.length > 200) {
      jobs.value = jobs.value.slice(0, 200)
    }
  }
  syncDetailJob()
}

const removeJob = (jobId) => {
  const targetId = Number(jobId || 0)
  if (!targetId) return
  const next = jobs.value.filter((job) => Number(job?.job_id || 0) !== targetId)
  if (next.length !== jobs.value.length) {
    jobs.value = next
    syncDetailJob()
  }
}

const appendDetailEvent = (item) => {
  if (!detailDrawerVisible.value || !detailJob.value?.job_id) return
  const detailJobId = Number(detailJob.value.job_id || 0)
  const eventJobId = Number(item?.job_id || 0)
  if (!detailJobId || detailJobId !== eventJobId) return
  const eventId = Number(item?.id || 0)
  if (!eventId) return
  if (detailEvents.value.some((eventItem) => Number(eventItem?.id || 0) === eventId)) {
    return
  }
  detailEvents.value = [...detailEvents.value, item].sort((a, b) => Number(a.id || 0) - Number(b.id || 0))
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

const loadJobs = async (withLoading = true, options = {}) => {
  const silent = Boolean(options?.silent)
  void loadAccountPlans()
  if (withLoading) {
    loading.value = true
  }
  try {
    const data = await listSoraJobs({
      group_title: selectedGroupTitle.value || undefined,
      status: statusFilter.value,
      phase: phaseFilter.value,
      keyword: keyword.value || undefined,
      limit: 100
    })
    jobs.value = normalizeJobs(data)
    syncDetailJob()
    return true
  } catch (error) {
    if (!silent) {
      ElMessage.error(error?.response?.data?.detail || '获取任务失败')
    }
    return false
  } finally {
    if (withLoading) {
      loading.value = false
    }
  }
}

const clearRealtimeReconnectTimer = () => {
  if (realtimeReconnectTimer) {
    clearTimeout(realtimeReconnectTimer)
    realtimeReconnectTimer = null
  }
}

const closeJobRealtimeSource = () => {
  if (realtimeSource) {
    realtimeSource.close()
    realtimeSource = null
  }
}

const stopJobRealtime = () => {
  clearRealtimeReconnectTimer()
  closeJobRealtimeSource()
}

const scheduleJobRealtimeReconnect = () => {
  if (!allowRealtime) return
  clearRealtimeReconnectTimer()
  realtimeReconnectTimer = setTimeout(() => {
    if (!allowRealtime) return
    startJobRealtime()
  }, realtimeReconnectDelay)
  realtimeReconnectDelay = Math.min(realtimeReconnectDelay * 2, 10000)
}

const buildJobRealtimeParams = () => ({
  group_title: selectedGroupTitle.value || undefined,
  status: statusFilter.value || undefined,
  phase: phaseFilter.value || undefined,
  keyword: keyword.value?.trim() || undefined,
  limit: 100,
  with_events: true
})

const startJobRealtime = () => {
  if (!allowRealtime) return
  stopJobRealtime()
  const url = buildSoraJobStreamUrl(buildJobRealtimeParams())
  realtimeSource = new EventSource(url)

  realtimeSource.addEventListener('open', () => {
    realtimeReconnectDelay = 1000
  })

  realtimeSource.addEventListener('snapshot', (event) => {
    try {
      const payload = JSON.parse(event.data || '{}')
      jobs.value = normalizeJobs(payload?.jobs || [])
      syncDetailJob()
    } catch {
      // noop
    }
  })

  realtimeSource.addEventListener('job', (event) => {
    try {
      const payload = JSON.parse(event.data || '{}')
      upsertJob(payload)
    } catch {
      // noop
    }
  })

  realtimeSource.addEventListener('remove', (event) => {
    try {
      const payload = JSON.parse(event.data || '{}')
      removeJob(payload?.job_id)
    } catch {
      // noop
    }
  })

  realtimeSource.addEventListener('phase', (event) => {
    try {
      const payload = JSON.parse(event.data || '{}')
      appendDetailEvent(payload)
    } catch {
      // noop
    }
  })

  realtimeSource.onerror = () => {
    closeJobRealtimeSource()
    scheduleJobRealtimeReconnect()
  }
}

const startFallbackPolling = () => {
  if (fallbackPollingTimer) return
  fallbackPollingTimer = setInterval(() => {
    void loadJobs(false, { silent: true })
  }, 30000)
}

const stopFallbackPolling = () => {
  if (fallbackPollingTimer) {
    clearInterval(fallbackPollingTimer)
    fallbackPollingTimer = null
  }
}

const handleRealtimeFilterChange = async () => {
  await loadJobs()
  startJobRealtime()
}

const openCreateDialog = (mode = 'sora') => {
  createEntryMode.value = mode === 'video_api' ? 'video_api' : 'sora'
  const defaults = systemSettings.value?.sora || {}
  const duration = defaults.default_duration || '10s'
  const aspectRatio = defaults.default_aspect_ratio || 'landscape'
  const groupTitle = selectedGroupTitle.value || defaults.default_group_title || 'Sora'
  createForm.value = {
    group_title: groupTitle,
    dispatch_mode: 'weighted_auto',
    profile_id: null,
    prompt: '',
    image_url: '',
    duration,
    aspect_ratio: aspectRatio,
    video_api_model: normalizeVideoApiModel(buildVideoApiModel(aspectRatio, duration))
  }
  createDialogVisible.value = true
}

const loadAccountWeights = async () => {
  if (!createDialogVisible.value) return
  if (createEntryMode.value !== 'sora') return
  if (createForm.value.dispatch_mode !== 'weighted_auto') return
  weightsLoading.value = true
  try {
    const groupTitle = createForm.value.group_title || selectedGroupTitle.value || 'Sora'
    const data = await getSoraAccountWeights(groupTitle, 12)
    accountWeights.value = Array.isArray(data) ? data : []
  } catch (error) {
    accountWeights.value = []
    ElMessage.error(error?.response?.data?.detail || '获取账号权重失败')
  } finally {
    weightsLoading.value = false
  }
}

const submitTask = async () => {
  const prompt = createForm.value.prompt?.trim()
  if (!prompt) {
    ElMessage.warning('请输入提示词')
    return
  }
  const imageUrl = createForm.value.image_url?.trim()
  const isVideoApiMode = createEntryMode.value === 'video_api'
  const mode = createForm.value.dispatch_mode || (createForm.value.profile_id ? 'manual' : 'weighted_auto')
  if (!isVideoApiMode && mode === 'manual' && !createForm.value.profile_id) {
    ElMessage.warning('请输入窗口 ID')
    return
  }
  const videoApiToken = String(systemSettings.value?.video_api?.bearer_token || '').trim()
  if (isVideoApiMode && !videoApiToken) {
    ElMessage.warning('请先在系统设置中配置 Video API Token')
    return
  }
  submitting.value = true
  try {
    if (isVideoApiMode) {
      const payload = {
        prompt,
        model: normalizeVideoApiModel(createForm.value.video_api_model)
      }
      if (imageUrl) {
        payload.image_url = imageUrl
      }
      await createVideoViaVideoApi(payload, videoApiToken)
      ElMessage.success('任务已通过 /v1/videos 创建并进入队列')
    } else {
      const payload = {
        dispatch_mode: mode,
        prompt,
        duration: createForm.value.duration,
        aspect_ratio: createForm.value.aspect_ratio,
        group_title: createForm.value.group_title || 'Sora'
      }
      if (imageUrl) {
        payload.image_url = imageUrl
      }
      if (mode === 'manual') {
        payload.profile_id = createForm.value.profile_id
      }
      await createSoraJob(payload)
      ElMessage.success('任务已创建并进入队列')
    }
    createDialogVisible.value = false
    await loadJobs()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '创建任务失败')
  } finally {
    submitting.value = false
  }
}

const retryJob = async (row) => {
  if (!row?.job_id) {
    ElMessage.warning('缺少任务 ID')
    return
  }
  submitting.value = true
  try {
    const result = await retrySoraJob(row.job_id)
    const newJobId = result?.job_id
    if (newJobId && newJobId !== row.job_id) {
      ElMessage.success(`已换号创建新任务 Job #${newJobId}（原 Job #${row.job_id} 保留失败记录）`)
    } else {
      ElMessage.success(`Job #${row.job_id} 已重新进入队列`)
    }
    await loadJobs()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '重试失败')
  } finally {
    submitting.value = false
  }
}

const retryWatermark = async (row) => {
  if (!row?.job_id) {
    ElMessage.warning('缺少任务 ID')
    return
  }
  submitting.value = true
  try {
    await retrySoraJobWatermark(row.job_id)
    ElMessage.success(`Job #${row.job_id} 已触发去水印重试`)
    await loadJobs()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '去水印重试失败')
  } finally {
    submitting.value = false
  }
}

const openLink = (url) => {
  if (!url) return
  window.open(url, '_blank', 'noopener')
}

const openDetail = async (row) => {
  detailJob.value = { ...row }
  detailEvents.value = []
  try {
    const data = await listSoraJobEvents(row.job_id)
    const sorted = (Array.isArray(data) ? data : []).sort((a, b) => Number(a.id || 0) - Number(b.id || 0))
    detailEvents.value = sorted
  } catch (error) {
    detailEvents.value = []
  }
  detailDrawerVisible.value = true
}

watch(
  () => createDialogVisible.value,
  (visible) => {
    if (visible) {
      loadAccountWeights()
    } else {
      accountWeights.value = []
    }
  }
)

watch(
  () => createForm.value.dispatch_mode,
  () => {
    loadAccountWeights()
  }
)

watch(
  () => createForm.value.group_title,
  () => {
    loadAccountWeights()
  }
)

onMounted(async () => {
  allowRealtime = true
  nowTick.value = Date.now()
  relativeTimeTimer = window.setInterval(() => {
    nowTick.value = Date.now()
  }, 60000)
  await loadSystemSettings()
  await loadGroups()
  await loadAccountPlans(true)
  await loadJobs()
  startJobRealtime()
  startFallbackPolling()
})

onUnmounted(() => {
  allowRealtime = false
  if (relativeTimeTimer) {
    clearInterval(relativeTimeTimer)
    relativeTimeTimer = null
  }
  stopFallbackPolling()
  stopJobRealtime()
})
</script>

<style scoped>

.tasks-page {
  padding: 0;
  color: var(--ink);
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
  background: transparent;
}

.brand .title {
  font-size: 20px;
  font-weight: 700;
}

.concurrency {
  display: flex;
  gap: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: rgba(15, 23, 42, 0.04);
  border: 1px solid var(--border);
}

.metric {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.metric .label {
  font-size: 11px;
  color: var(--muted);
}

.metric .value {
  font-size: 16px;
  font-weight: 600;
}

.actions {
  display: flex;
  gap: 8px;
}

.create-mode-hint {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
}

.overview-card.danger {
  background: linear-gradient(135deg, rgba(255, 242, 242, 0.92), rgba(255, 255, 255, 0.9));
}

.task-table :deep(.el-table__cell) {
  vertical-align: middle;
  padding-top: 10px;
  padding-bottom: 10px;
}

.task-table :deep(.el-progress) {
  line-height: 1;
}

.task-table :deep(.el-progress-bar__outer),
.task-table :deep(.el-progress-bar__inner) {
  border-radius: 999px;
}

.task-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 56px;
}

.task-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-weight: 600;
  color: var(--ink);
  min-width: 0;
}

.task-id {
  font-size: 14px;
  font-weight: 700;
}

.task-dot {
  width: 4px;
  height: 4px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.35);
}

.task-window {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}

.task-proxy {
  font-size: 12px;
  color: rgba(15, 23, 42, 0.7);
  font-weight: 700;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.task-plus {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.4;
  font-weight: 650;
  color: #065f46;
  background: rgba(16, 185, 129, 0.12);
  border: 1px solid rgba(16, 185, 129, 0.28);
  margin-left: 6px;
}

.task-meta {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}

.task-time {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}

.task-prompt {
  min-width: 0;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 14px;
  line-height: 1.45;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.task-prompt-empty {
  color: var(--muted);
}

.status-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 56px;
}

.status-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.status-phase {
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}

.status-progress {
  width: 100%;
  max-width: 220px;
}

.status-error {
  font-size: 11px;
  color: #b91c1c;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.output-cell {
  display: flex;
  flex-direction: column;
  min-height: 52px;
  justify-content: center;
  align-items: flex-start;
  gap: 4px;
}

.output-line {
  width: 100%;
  min-height: 20px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.output-error {
  font-size: 12px;
  color: #b91c1c;
}

.output-empty {
  display: inline-block;
  min-height: 20px;
  min-width: 1px;
}

.share-code-link {
  max-width: 100%;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
  font-size: 12px;
  line-height: 1.35;
  color: var(--accent-strong);
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.watermark-link {
  color: #0f766e;
}

.share-code-link:hover {
  text-decoration: underline;
}

.action-cell {
  display: flex;
  flex-wrap: nowrap;
  gap: 6px;
  min-height: 56px;
  align-items: center;
}

.detail-block {
  margin-bottom: 18px;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
}

.detail-item {
  background: #f6f7fb;
  border-radius: 12px;
  padding: 10px 12px;
  font-size: 12px;
  color: #6b7280;
}

.detail-item strong {
  display: block;
  color: #1f2937;
  font-size: 14px;
  margin-top: 4px;
}

.detail-label {
  font-size: 12px;
  color: #8b95a7;
  margin-bottom: 6px;
}

.detail-text {
  background: #fff;
  border-radius: 12px;
  padding: 12px;
  border: 1px solid rgba(203, 213, 225, 0.5);
  font-size: 13px;
}

.event-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.event-item {
  padding: 10px 12px;
  border-radius: 12px;
  background: #fff;
  border: 1px solid rgba(203, 213, 225, 0.5);
}

.event-time {
  font-size: 11px;
  color: #8a93a5;
}

.event-meta {
  display: flex;
  gap: 8px;
  margin-top: 4px;
  font-size: 12px;
  color: #52607a;
}

.event-phase {
  font-weight: 600;
  color: #1f2937;
}

.event-message {
  margin-top: 6px;
  font-size: 12px;
  color: #4b5563;
}

.event-empty {
  padding: 12px;
  font-size: 12px;
  color: #8a93a5;
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

.dispatch-mode {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.dispatch-tip {
  font-size: 12px;
  color: #475569;
}

.weights-panel {
  width: 100%;
  border: 1px solid rgba(148, 163, 184, 0.32);
  border-radius: 12px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.92);
}

.weights-table :deep(.el-table__cell) {
  padding-top: 6px;
  padding-bottom: 6px;
}

.weights-empty {
  padding: 10px 12px;
  font-size: 12px;
  color: #64748b;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
}

.score {
  font-weight: 700;
  color: #0f172a;
}

.weights-reason {
  display: inline-block;
  max-width: 100%;
  font-size: 12px;
  color: #475569;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 980px) {
  .command-bar {
    flex-direction: column;
    align-items: flex-start;
  }

  .command-right {
    width: 100%;
    justify-content: space-between;
  }

  .overview {
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  }
}
</style>
