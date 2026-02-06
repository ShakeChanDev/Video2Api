<template>
  <div class="nurture-page">
    <section class="command-bar" v-loading="pageLoading">
      <div class="command-left">
        <div class="command-title">养号任务</div>
        <div class="filters">
          <el-select v-model="selectedGroupTitle" class="w-180" @change="onGroupChange">
            <el-option
              v-for="group in groups"
              :key="group.id"
              :label="`${group.title} (ID:${group.id})`"
              :value="group.title"
            />
          </el-select>

          <el-select v-model="batchStatus" class="w-140" @change="loadBatches">
            <el-option label="全部状态" value="all" />
            <el-option label="排队中" value="queued" />
            <el-option label="运行中" value="running" />
            <el-option label="成功" value="completed" />
            <el-option label="失败" value="failed" />
            <el-option label="已取消" value="canceled" />
          </el-select>

          <el-tag size="large" effect="light" type="info">一次刷 {{ createForm.scroll_count }} 条</el-tag>
        </div>
      </div>
      <div class="command-right">
        <el-button @click="refreshAll">刷新</el-button>
        <el-button
          type="warning"
          :loading="creating"
          :disabled="!selectedProfileIds.length"
          @click="createBatch"
        >
          创建并开始
        </el-button>
      </div>
    </section>

    <section class="content-grid">
      <el-card class="table-card" v-loading="groupsLoading || windowsLoading">
        <template #header>
          <div class="table-head stack">
            <span>选择账号</span>
            <span class="table-hint">从所选分组勾选窗口，创建任务组后会逐一打开并在 Explore 刷 10 条</span>
          </div>
        </template>

        <div class="create-panel">
          <el-form :model="createForm" label-width="120px" class="create-form">
            <el-form-item label="任务组名称">
              <el-input v-model="createForm.name" placeholder="可留空，系统自动生成" maxlength="60" />
            </el-form-item>

            <el-form-item label="刷条数">
              <el-input-number v-model="createForm.scroll_count" :min="1" :max="50" />
            </el-form-item>

            <el-form-item label="高级参数">
              <div class="advanced-grid">
                <div class="advanced-item">
                  <div class="advanced-label">点赞概率</div>
                  <el-input-number v-model="createForm.like_probability" :min="0" :max="1" :step="0.01" />
                </div>
                <div class="advanced-item">
                  <div class="advanced-label">关注概率</div>
                  <el-input-number v-model="createForm.follow_probability" :min="0" :max="1" :step="0.01" />
                </div>
                <div class="advanced-item">
                  <div class="advanced-label">单号最多关注</div>
                  <el-input-number v-model="createForm.max_follows_per_profile" :min="0" :max="5" />
                </div>
                <div class="advanced-item">
                  <div class="advanced-label">单号最多点赞</div>
                  <el-input-number v-model="createForm.max_likes_per_profile" :min="0" :max="10" />
                </div>
              </div>
            </el-form-item>
          </el-form>

          <div class="selection-tip">
            已选 <strong>{{ selectedProfileIds.length }}</strong> 个窗口
          </div>
        </div>

        <el-table
          v-if="windowRows.length"
          ref="windowTableRef"
          :data="windowRows"
          class="card-table"
          row-key="profile_id"
          @selection-change="handleSelectionChange"
        >
          <el-table-column type="selection" width="46" align="center" reserve-selection />
          <el-table-column label="窗口" min-width="240">
            <template #default="{ row }">
              <div class="window-cell">
                <div class="window-name">{{ row.window_name || '-' }}</div>
                <div class="window-meta">ID {{ row.profile_id }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="账号" min-width="240">
            <template #default="{ row }">
              <div class="account-cell">
                <span class="account-text">{{ row.account || '-' }}</span>
                <span v-if="row.account_plan === 'plus'" class="plan-badge">Plus</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="可用次数" width="160" align="center">
            <template #default="{ row }">
              <span>{{ row.quota_remaining_count ?? '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="180" align="center">
            <template #default="{ row }">{{ formatTime(row.scanned_at) }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无窗口数据" :image-size="90">
          <el-button @click="loadGroups" :loading="groupsLoading">重新加载</el-button>
        </el-empty>
      </el-card>

      <div class="right-stack">
        <el-card class="table-card" v-loading="batchesLoading">
          <template #header>
            <div class="table-head">
              <span>任务组列表</span>
              <span class="table-hint">会自动轮询刷新</span>
            </div>
          </template>

          <el-table
            :data="batches"
            class="card-table"
            empty-text="暂无任务组"
            highlight-current-row
            @row-click="selectBatch"
          >
            <el-table-column label="Batch" width="92">
              <template #default="{ row }">#{{ row.batch_id }}</template>
            </el-table-column>
            <el-table-column label="名称" min-width="220">
              <template #default="{ row }">
                <div class="batch-name">{{ row.name || '-' }}</div>
                <div class="batch-meta">窗口数 {{ row.total_jobs }}</div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="120" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="batchStatusTag(row.status)">{{ batchStatusText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="统计" width="170" align="center">
              <template #default="{ row }">
                <span class="stat-ok">{{ row.success_count }}</span>
                <span class="stat-split">/</span>
                <span class="stat-fail">{{ row.failed_count }}</span>
                <span class="stat-split">/</span>
                <span class="stat-cancel">{{ row.canceled_count }}</span>
              </template>
            </el-table-column>
            <el-table-column label="赞/关" width="140" align="center">
              <template #default="{ row }">
                <span>{{ row.like_total }}/{{ row.follow_total }}</span>
              </template>
            </el-table-column>
            <el-table-column label="更新时间" width="180" align="center">
              <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" fixed="right" width="130" align="center">
              <template #default="{ row }">
                <el-button
                  v-if="row.status === 'queued' || row.status === 'running'"
                  size="small"
                  type="warning"
                  @click.stop="cancelBatch(row)"
                >
                  取消
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card v-if="selectedBatch" class="table-card" v-loading="jobsLoading">
          <template #header>
            <div class="table-head stack">
              <span>任务明细（Batch #{{ selectedBatch.batch_id }}）</span>
              <span class="table-hint">{{ selectedBatch.name || '-' }}</span>
            </div>
          </template>

          <el-table :data="jobs" class="card-table" empty-text="暂无任务">
            <el-table-column label="窗口" min-width="240">
              <template #default="{ row }">
                <div class="window-cell">
                  <div class="window-name">{{ row.window_name || '-' }}</div>
                  <div class="window-meta">ID {{ row.profile_id }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="120" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="jobStatusTag(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="阶段" width="120" align="center">
              <template #default="{ row }">{{ row.phase }}</template>
            </el-table-column>
            <el-table-column label="进度" width="180">
              <template #default="{ row }">
                <div class="progress-cell">
                  <el-progress
                    v-if="row.scroll_target > 0"
                    :percentage="progressPct(row)"
                    :stroke-width="8"
                    :text-inside="true"
                    :status="row.status === 'failed' ? 'exception' : undefined"
                  />
                  <div class="progress-text">{{ row.scroll_done }}/{{ row.scroll_target }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="赞/关" width="120" align="center">
              <template #default="{ row }">{{ row.like_count }}/{{ row.follow_count }}</template>
            </el-table-column>
            <el-table-column label="错误" min-width="220">
              <template #default="{ row }">
                <span class="error-text">{{ shorten(row.error, 80) }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createNurtureBatch,
  listNurtureBatches,
  listNurtureJobs,
  cancelNurtureBatch,
  getIxBrowserGroupWindows,
  getLatestIxBrowserSoraSessionAccounts
} from '../api'

const groupsLoading = ref(false)
const windowsLoading = ref(false)
const batchesLoading = ref(false)
const jobsLoading = ref(false)
const creating = ref(false)
const pageLoading = computed(() => groupsLoading.value || windowsLoading.value || batchesLoading.value || jobsLoading.value)

const groups = ref([])
const selectedGroupTitle = ref('Sora')
const batchStatus = ref('all')

const latestScan = ref(null)
const scanMap = computed(() => {
  const rows = latestScan.value?.results || []
  const map = {}
  for (const row of rows) {
    const pid = Number(row?.profile_id)
    if (!Number.isFinite(pid) || pid <= 0) continue
    map[pid] = row
  }
  return map
})

const windowTableRef = ref(null)
const selectedProfileIds = ref([])

const createForm = ref({
  name: '',
  scroll_count: 10,
  like_probability: 0.25,
  follow_probability: 0.06,
  max_follows_per_profile: 1,
  max_likes_per_profile: 3
})

const batches = ref([])
const selectedBatch = ref(null)
const jobs = ref([])

let pollTimer = null

const selectedGroup = computed(() => groups.value.find((g) => g.title === selectedGroupTitle.value) || null)
const windowRows = computed(() => {
  const target = selectedGroup.value
  if (!target || !Array.isArray(target.windows)) return []
  const rows = target.windows.map((win) => {
    const pid = Number(win.profile_id)
    const scan = scanMap.value[pid] || null
    return {
      profile_id: pid,
      window_name: win.name,
      account: scan?.account || '',
      account_plan: scan?.account_plan || '',
      quota_remaining_count: scan?.quota_remaining_count,
      scanned_at: scan?.scanned_at || ''
    }
  })
  rows.sort((a, b) => Number(b.profile_id) - Number(a.profile_id))
  return rows
})

const loadGroups = async () => {
  groupsLoading.value = true
  try {
    const data = await getIxBrowserGroupWindows()
    groups.value = Array.isArray(data) ? data : []
    const sora = groups.value.find((g) => g.title === 'Sora')
    if (sora) {
      selectedGroupTitle.value = 'Sora'
    } else if (groups.value.length > 0 && !groups.value.some((g) => g.title === selectedGroupTitle.value)) {
      selectedGroupTitle.value = groups.value[0].title
    }
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '获取分组失败')
  } finally {
    groupsLoading.value = false
  }
}

const loadLatestScan = async () => {
  if (!selectedGroupTitle.value) return
  windowsLoading.value = true
  try {
    const data = await getLatestIxBrowserSoraSessionAccounts(selectedGroupTitle.value, true)
    latestScan.value = data || null
  } catch (error) {
    if (error?.response?.status !== 404) {
      latestScan.value = null
    }
  } finally {
    windowsLoading.value = false
  }
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
  if (windowTableRef.value && typeof windowTableRef.value.clearSelection === 'function') {
    windowTableRef.value.clearSelection()
  }
}

const loadBatches = async () => {
  batchesLoading.value = true
  try {
    const params = {
      group_title: selectedGroupTitle.value,
      limit: 50
    }
    if (batchStatus.value && batchStatus.value !== 'all') params.status = batchStatus.value
    const data = await listNurtureBatches(params)
    batches.value = Array.isArray(data) ? data : []
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '读取任务组失败')
  } finally {
    batchesLoading.value = false
  }
}

const loadJobs = async () => {
  if (!selectedBatch.value?.batch_id) return
  jobsLoading.value = true
  try {
    const data = await listNurtureJobs(selectedBatch.value.batch_id, { limit: 500 })
    jobs.value = Array.isArray(data) ? data : []
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '读取任务明细失败')
  } finally {
    jobsLoading.value = false
  }
}

const selectBatch = async (row) => {
  selectedBatch.value = row
  await loadJobs()
}

const refreshAll = async () => {
  await Promise.all([loadLatestScan(), loadBatches(), loadJobs()])
}

const onGroupChange = async () => {
  clearSelection()
  selectedBatch.value = null
  jobs.value = []
  await Promise.all([loadLatestScan(), loadBatches()])
}

const createBatch = async () => {
  if (!selectedProfileIds.value.length) return
  creating.value = true
  try {
    const payload = {
      name: createForm.value.name || null,
      group_title: selectedGroupTitle.value,
      profile_ids: selectedProfileIds.value,
      scroll_count: Number(createForm.value.scroll_count),
      like_probability: Number(createForm.value.like_probability),
      follow_probability: Number(createForm.value.follow_probability),
      max_follows_per_profile: Number(createForm.value.max_follows_per_profile),
      max_likes_per_profile: Number(createForm.value.max_likes_per_profile)
    }
    const batch = await createNurtureBatch(payload)
    ElMessage.success(`已创建任务组 #${batch?.batch_id}`)
    clearSelection()
    createForm.value.name = ''
    await loadBatches()
    if (batch?.batch_id) {
      const found = batches.value.find((b) => b.batch_id === batch.batch_id) || batch
      await selectBatch(found)
    }
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '创建任务组失败')
  } finally {
    creating.value = false
  }
}

const cancelBatch = async (row) => {
  if (!row?.batch_id) return
  try {
    await ElMessageBox.confirm(`确认取消任务组 #${row.batch_id}？`, '取消确认', {
      confirmButtonText: '取消任务组',
      cancelButtonText: '返回',
      type: 'warning'
    })
  } catch {
    return
  }

  try {
    await cancelNurtureBatch(row.batch_id)
    ElMessage.success('已提交取消')
    await refreshAll()
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '取消失败')
  }
}

const batchStatusTag = (status) => {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'running') return 'warning'
  return 'info'
}

const batchStatusText = (status) => {
  if (status === 'queued') return '排队中'
  if (status === 'running') return '运行中'
  if (status === 'completed') return '成功'
  if (status === 'failed') return '失败'
  if (status === 'canceled') return '已取消'
  return status || '-'
}

const jobStatusTag = (status) => {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'running') return 'warning'
  if (status === 'skipped') return 'info'
  return 'info'
}

const progressPct = (row) => {
  const done = Number(row?.scroll_done || 0)
  const total = Number(row?.scroll_target || 0)
  if (!total) return 0
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)))
}

const shorten = (text, maxLen) => {
  const raw = (text || '').toString()
  if (!raw) return '-'
  if (raw.length <= maxLen) return raw
  return `${raw.slice(0, maxLen)}...`
}

const formatTime = (value) => {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return String(value)
  }
}

const startPolling = () => {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    await loadBatches()
    if (selectedBatch.value?.batch_id) {
      await loadJobs()
      const updated = batches.value.find((b) => b.batch_id === selectedBatch.value.batch_id)
      if (updated) selectedBatch.value = updated
    }
  }, 2500)
}

const stopPolling = () => {
  if (pollTimer) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(async () => {
  await loadGroups()
  await Promise.all([loadLatestScan(), loadBatches()])
  startPolling()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<style scoped>
.nurture-page {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
}

.content-grid {
  display: grid;
  grid-template-columns: minmax(420px, 1fr) minmax(420px, 1fr);
  gap: var(--page-gap);
  align-items: start;
}

.right-stack {
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
}

.create-panel {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px 0;
}

.create-form {
  flex: 1;
  min-width: 0;
}

.selection-tip {
  padding-top: 8px;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}

.advanced-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(140px, 1fr));
  gap: 10px 12px;
  width: 100%;
  padding: 10px 12px;
  border: 1px dashed rgba(148, 163, 184, 0.35);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.8);
}

.advanced-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.advanced-label {
  font-size: 12px;
  color: var(--muted);
  font-weight: 600;
}

.window-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.window-name {
  font-weight: 700;
  color: var(--ink);
}

.window-meta {
  font-size: 12px;
  color: var(--muted);
}

.account-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.account-text {
  font-weight: 600;
  color: #1f2937;
}

.plan-badge {
  padding: 2px 10px;
  border-radius: 999px;
  background: rgba(14, 165, 164, 0.14);
  border: 1px solid rgba(14, 165, 164, 0.22);
  color: var(--accent-strong);
  font-weight: 800;
  font-size: 11px;
}

.batch-name {
  font-weight: 700;
  color: var(--ink);
}

.batch-meta {
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
}

.stat-ok {
  color: #16a34a;
  font-weight: 800;
}

.stat-fail {
  color: #dc2626;
  font-weight: 800;
}

.stat-cancel {
  color: #64748b;
  font-weight: 800;
}

.stat-split {
  color: rgba(15, 23, 42, 0.35);
  margin: 0 4px;
}

.progress-cell {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.progress-text {
  font-size: 12px;
  color: var(--muted);
  text-align: right;
}

.error-text {
  color: rgba(15, 23, 42, 0.65);
}

.w-140 {
  width: 140px;
}

.w-180 {
  width: 180px;
}

@media (max-width: 1080px) {
  .content-grid {
    grid-template-columns: 1fr;
  }
}
</style>

