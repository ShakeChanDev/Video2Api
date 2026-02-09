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
            placeholder="搜索 ip/备注/tag/ix_id"
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
        <el-table-column type="selection" width="46" align="center" reserve-selection />
        <el-table-column label="ID" width="86" align="center">
          <template #default="{ row }">
            <span class="mono">#{{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="ix_id" width="96" align="center">
          <template #default="{ row }">
            <span class="mono">{{ row.ix_id ?? '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="light" type="info">{{ (row.proxy_type || 'http').toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="地址" min-width="220">
          <template #default="{ row }">
            <div class="addr-cell">
              <span class="mono">{{ row.proxy_ip }}:{{ row.proxy_port }}</span>
              <span v-if="row.ix_country || row.ix_city" class="addr-meta">
                {{ [row.ix_country, row.ix_city].filter(Boolean).join(' / ') }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="账号" width="170">
          <template #default="{ row }">
            <span class="mono">{{ row.proxy_user || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="密码" width="190">
          <template #default="{ row }">
            <span class="mono">{{ row.proxy_password || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Tag" width="160">
          <template #default="{ row }">
            <span>{{ row.tag || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="220">
          <template #default="{ row }">
            <el-tooltip v-if="row.note" :content="row.note" placement="top" effect="dark">
              <span class="note-text">{{ shorten(row.note, 44) }}</span>
            </el-tooltip>
            <span v-else class="note-text note-empty">-</span>
          </template>
        </el-table-column>
        <el-table-column label="检测" min-width="280">
          <template #default="{ row }">
            <div class="check-cell">
              <el-tag
                size="small"
                effect="light"
                :type="row.check_status === 'success' ? 'success' : row.check_status === 'failed' ? 'danger' : 'info'"
              >
                {{ row.check_status || 'unknown' }}
              </el-tag>
              <div class="check-meta" v-if="row.check_status === 'success'">
                <span class="mono">{{ row.check_ip || '-' }}</span>
                <span class="check-split">·</span>
                <span>{{ [row.check_country, row.check_city].filter(Boolean).join(' / ') || '-' }}</span>
                <span v-if="row.check_timezone" class="check-split">·</span>
                <span v-if="row.check_timezone" class="mono">{{ row.check_timezone }}</span>
              </div>
              <el-tooltip v-else-if="row.check_error" :content="row.check_error" placement="top" effect="dark">
                <span class="check-error">{{ shorten(row.check_error, 48) }}</span>
              </el-tooltip>
              <span v-else class="check-error check-empty">-</span>
              <div class="check-at">{{ row.check_at || '-' }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170" align="center">
          <template #default="{ row }">
            <span class="mono">{{ row.updated_at }}</span>
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
        后端会直连探测（默认 {{ defaultCheckUrl }}），用于验证可用性与出口 IP/地区。ssh 类型不支持直连检测。
      </div>
      <el-form :model="checkForm" label-width="110px">
        <el-form-item label="检测 URL">
          <el-input v-model="checkForm.check_url" placeholder="留空使用默认" />
        </el-form-item>
        <el-form-item label="并发">
          <el-input-number v-model="checkForm.concurrency" :min="1" :max="100" />
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="checkForm.timeout_sec" :min="1" :max="60" :step="0.5" />
        </el-form-item>
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
const selectedIds = ref([])

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

const defaultCheckUrl = 'https://ipinfo.io/json'
const checkDialogVisible = ref(false)
const checkForm = ref({
  check_url: '',
  concurrency: 20,
  timeout_sec: 8.0
})

const shorten = (text, maxLen = 60) => {
  const raw = String(text || '')
  if (raw.length <= maxLen) return raw
  return raw.slice(0, maxLen - 1) + '…'
}

const loadList = async () => {
  loading.value = true
  try {
    const data = await listProxies({
      keyword: keyword.value || null,
      page: page.value,
      limit: pageSize.value
    })
    rows.value = Array.isArray(data?.items) ? data.items : []
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
      check_url: String(checkForm.value.check_url || '').trim() || null,
      concurrency: Number(checkForm.value.concurrency || 20),
      timeout_sec: Number(checkForm.value.timeout_sec || 8.0)
    }
    const resp = await batchCheckProxies(payload)
    const ok = (resp?.results || []).filter((r) => r.ok).length
    const fail = (resp?.results || []).filter((r) => !r.ok).length
    ElMessage.success(`检测完成：成功 ${ok}，失败 ${fail}`)
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
}

.addr-meta {
  font-size: 12px;
  color: var(--muted);
}

.note-text {
  color: #334155;
}

.note-empty {
  color: #94a3b8;
}

.check-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
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

.check-at {
  font-size: 12px;
  color: rgba(100, 116, 139, 1);
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

