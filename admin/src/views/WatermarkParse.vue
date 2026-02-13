<template>
  <div class="watermark-parse-page">
    <section class="command-bar">
      <div class="command-left">
        <div class="command-title">去水印解析</div>
        <div class="panel-subtitle">输入 Sora 分享链接，获取可直接访问的无水印链接。</div>
      </div>
    </section>

    <el-card class="table-card">
      <template #header>
        <div class="table-head stack">
          <span>分享链接解析</span>
          <span class="table-hint">严格按系统配置的解析方式执行（custom / third_party）</span>
        </div>
      </template>

      <el-form label-width="110px" @submit.prevent>
        <el-form-item label="分享链接">
          <el-input
            v-model="form.share_urls_text"
            class="share-textarea"
            type="textarea"
            :autosize="{ minRows: 6, maxRows: 14 }"
            clearable
            :placeholder="shareInputPlaceholder"
            @keydown.ctrl.enter.prevent="handleParse"
          />
        </el-form-item>
        <el-form-item>
          <div class="input-hint">每行一个链接，按 Ctrl+Enter 快速解析；空行和非法链接会自动跳过。</div>
        </el-form-item>
        <el-form-item>
          <div class="action-row">
            <el-button type="primary" :loading="parsing" @click="handleParse">解析去水印链接</el-button>
            <el-button :disabled="parsing || downloadingAll" @click="handleReset">重置</el-button>
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="results.length" class="table-card">
      <template #header>
        <div class="table-head stack">
          <div class="result-head-row">
            <span>解析结果</span>
            <el-button
              size="small"
              class="btn-soft"
              :disabled="parsing || downloadingAll || !successRows.length"
              :loading="downloadingAll"
              @click="downloadAll"
            >
              一键全部下载
            </el-button>
          </div>
          <div class="summary-row">
            <el-tag size="small" class="summary-tag" effect="plain">输入 {{ summary.input_count }}</el-tag>
            <el-tag size="small" class="summary-tag" effect="plain">有效 {{ summary.valid_count }}</el-tag>
            <el-tag size="small" class="summary-tag" effect="plain">去重 {{ summary.dedup_count }}</el-tag>
            <el-tag size="small" class="summary-tag" type="success" effect="plain">成功 {{ summary.success_count }}</el-tag>
            <el-tag size="small" class="summary-tag" type="danger" effect="plain">失败 {{ summary.failed_count }}</el-tag>
          </div>
        </div>
      </template>
      <el-table :data="results" stripe>
        <el-table-column type="index" label="#" width="60" />
        <el-table-column label="分享链接" min-width="220">
          <template #default="{ row }">
            <span class="ellipsis-text" :title="row.share_url || row.input_url">{{ row.share_url || row.input_url }}</span>
          </template>
        </el-table-column>
        <el-table-column label="解析方式" width="120">
          <template #default="{ row }">
            <span>{{ row.parse_method || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="getStatusType(row.status)">
              {{ getStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="无水印链接" min-width="280">
          <template #default="{ row }">
            <div v-if="row.watermark_url" class="result-link-row">
              <a class="result-link" href="#" :title="row.watermark_url" @click.prevent="openLink(row.watermark_url)">
                {{ row.watermark_url }}
              </a>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="错误信息" min-width="180">
          <template #default="{ row }">
            <span class="error-text" :title="row.error || '-'">{{ row.error || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <div class="op-row">
              <el-button size="small" class="btn-soft" :disabled="!row.watermark_url" @click="copyLink(row.watermark_url)">
                复制
              </el-button>
              <el-button
                size="small"
                class="btn-soft"
                :disabled="!row.watermark_url || parsing || downloadingAll"
                @click="downloadOne(row)"
              >
                下载
              </el-button>
              <el-button size="small" class="btn-soft" @click="openRowLink(row)">打开</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-alert
      v-if="errorText"
      class="error-alert"
      type="error"
      :closable="false"
      :title="errorText"
      show-icon
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { parseSoraWatermarkLink } from '../api'

const MAX_BATCH = 100
const PARSE_CONCURRENCY = 5
const DOWNLOAD_INTERVAL_MS = 200
const shareInputPlaceholder = `例如：
https://sora.chatgpt.com/p/s_xxxxxxxx
https://sora.chatgpt.com/p/s_yyyyyyyy`

const parsing = ref(false)
const downloadingAll = ref(false)
const errorText = ref('')
const results = ref([])
const form = ref({
  share_urls_text: ''
})
const summary = ref({
  input_count: 0,
  valid_count: 0,
  dedup_count: 0,
  success_count: 0,
  failed_count: 0
})

const copyText = async (text) => {
  const value = String(text || '')
  if (!value) return false
  if (navigator?.clipboard?.writeText) {
    await navigator.clipboard.writeText(value)
    return true
  }
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  const ok = document.execCommand('copy')
  document.body.removeChild(textarea)
  return ok
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const sanitizeFilename = (name) => String(name || '').replace(/[\\/:*?"<>|]+/g, '_').replace(/\s+/g, ' ').trim()

const getStatusLabel = (status) => {
  if (status === 'success') return '成功'
  if (status === 'failed') return '失败'
  return '解析中'
}

const getStatusType = (status) => {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  return 'info'
}

const parseFilenameFromDisposition = (contentDisposition) => {
  const text = String(contentDisposition || '').trim()
  if (!text) return ''
  const utf8Match = text.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)
  if (utf8Match?.[1]) {
    const encoded = utf8Match[1].trim().replace(/^"(.*)"$/, '$1')
    try {
      return decodeURIComponent(encoded)
    } catch {
      return encoded
    }
  }
  const asciiMatch = text.match(/filename\s*=\s*"?([^\";]+)"?/i)
  if (asciiMatch?.[1]) return asciiMatch[1].trim()
  return ''
}

const getExtensionFromContentType = (contentType) => {
  const normalized = String(contentType || '').split(';')[0].trim().toLowerCase()
  const map = {
    'video/mp4': '.mp4',
    'video/webm': '.webm',
    'video/quicktime': '.mov',
    'video/x-matroska': '.mkv'
  }
  return map[normalized] || ''
}

const getExtensionFromUrl = (url) => {
  try {
    const pathname = new URL(url).pathname || ''
    const match = pathname.match(/\.([a-z0-9]{2,5})$/i)
    if (match?.[1]) return `.${match[1].toLowerCase()}`
  } catch {
    return ''
  }
  return ''
}

const triggerBlobDownload = (blob, filename) => {
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  link.rel = 'noopener'
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 5000)
}

const buildDownloadFilename = (row, response) => {
  const contentDisposition = response.headers.get('content-disposition') || ''
  const fromHeader = sanitizeFilename(parseFilenameFromDisposition(contentDisposition))
  if (fromHeader) return fromHeader
  const baseName = sanitizeFilename(String(row.share_id || '').trim()) || `watermark_${Date.now()}`
  const extFromType = getExtensionFromContentType(response.headers.get('content-type') || '')
  const extFromUrl = getExtensionFromUrl(row.watermark_url || '')
  const ext = extFromType || extFromUrl || '.mp4'
  return `${baseName}${ext}`
}

const extractShareId = (pathname) => {
  const match = String(pathname || '').match(/\/p\/(s_[A-Za-z0-9_-]+)/i)
  return match?.[1] || ''
}

const normalizeShareUrl = (rawUrl) => {
  const text = String(rawUrl || '').trim()
  if (!text) return { valid: false }
  let parsedUrl
  try {
    parsedUrl = new URL(text)
  } catch {
    return { valid: false }
  }
  const protocol = String(parsedUrl.protocol || '').toLowerCase()
  if (protocol !== 'https:' && protocol !== 'http:') return { valid: false }
  const host = String(parsedUrl.hostname || '').toLowerCase()
  if (!host || !host.endsWith('sora.chatgpt.com')) return { valid: false }
  const shareId = extractShareId(parsedUrl.pathname)
  if (!shareId) return { valid: false }
  return {
    valid: true,
    share_id: shareId,
    share_url: `https://sora.chatgpt.com/p/${shareId}`
  }
}

const buildBatchItems = (inputText) => {
  const lines = String(inputText || '')
    .split(/\r?\n/)
    .map((line) => String(line || '').trim())
    .filter(Boolean)

  const validRows = []
  for (const line of lines) {
    const parsed = normalizeShareUrl(line)
    if (!parsed.valid) continue
    validRows.push({
      input_url: line,
      share_url: parsed.share_url,
      share_id: parsed.share_id,
      dedupe_key: parsed.share_id ? `share:${String(parsed.share_id).toLowerCase()}` : `url:${String(parsed.share_url).toLowerCase()}`
    })
  }

  const uniqueRows = []
  const dedupeKeys = new Set()
  for (const row of validRows) {
    if (dedupeKeys.has(row.dedupe_key)) continue
    dedupeKeys.add(row.dedupe_key)
    uniqueRows.push({
      input_url: row.input_url,
      share_url: row.share_url,
      share_id: row.share_id
    })
  }

  return {
    input_count: lines.length,
    valid_count: validRows.length,
    dedup_count: validRows.length - uniqueRows.length,
    items: uniqueRows
  }
}

const updateSummaryResultStats = (rows = results.value) => {
  let success = 0
  let failed = 0
  for (const row of rows) {
    if (row.status === 'success') success += 1
    if (row.status === 'failed') failed += 1
  }
  summary.value = {
    ...summary.value,
    success_count: success,
    failed_count: failed
  }
}

const runWithConcurrency = async (rows, limit, worker) => {
  if (!Array.isArray(rows) || rows.length === 0) return
  let cursor = 0
  const runner = async () => {
    while (true) {
      const index = cursor
      cursor += 1
      if (index >= rows.length) return
      await worker(rows[index], index)
    }
  }
  const workerCount = Math.min(limit, rows.length)
  await Promise.all(Array.from({ length: workerCount }, () => runner()))
}

const handleParse = async () => {
  const batch = buildBatchItems(form.value.share_urls_text)
  summary.value = {
    input_count: batch.input_count,
    valid_count: batch.valid_count,
    dedup_count: batch.dedup_count,
    success_count: 0,
    failed_count: 0
  }
  if (!batch.items.length) {
    results.value = []
    ElMessage.warning('未检测到有效 Sora 分享链接')
    return
  }
  if (batch.items.length > MAX_BATCH) {
    results.value = []
    ElMessage.warning(`单次最多解析 ${MAX_BATCH} 条有效链接，当前 ${batch.items.length} 条`)
    return
  }

  const rows = batch.items.map((item) => ({
    input_url: item.input_url,
    share_url: item.share_url,
    share_id: item.share_id,
    parse_method: '',
    watermark_url: '',
    error: '',
    status: 'pending'
  }))

  results.value = rows
  updateSummaryResultStats(rows)
  parsing.value = true
  errorText.value = ''
  try {
    await runWithConcurrency(rows, PARSE_CONCURRENCY, async (row) => {
      try {
        const data = await parseSoraWatermarkLink({ share_url: row.share_url })
        const watermarkUrl = String(data?.watermark_url || '').trim()
        if (!watermarkUrl) throw new Error('解析服务未返回无水印链接')
        row.share_url = String(data?.share_url || row.share_url).trim() || row.share_url
        row.share_id = String(data?.share_id || row.share_id).trim() || row.share_id
        row.parse_method = String(data?.parse_method || '').trim()
        row.watermark_url = watermarkUrl
        row.error = ''
        row.status = 'success'
      } catch (error) {
        row.status = 'failed'
        row.watermark_url = ''
        row.error = error?.response?.data?.detail || error?.message || '解析失败'
      } finally {
        results.value = [...rows]
        updateSummaryResultStats(rows)
      }
    })
    const successCount = summary.value.success_count
    const failedCount = summary.value.failed_count
    if (successCount > 0 && failedCount === 0) {
      ElMessage.success(`解析完成，共成功 ${successCount} 条`)
    } else if (successCount > 0) {
      ElMessage.warning(`解析完成：成功 ${successCount} 条，失败 ${failedCount} 条`)
    } else {
      ElMessage.error(`解析完成：失败 ${failedCount} 条`)
    }
  } catch (error) {
    errorText.value = error?.response?.data?.detail || '解析失败'
  } finally {
    parsing.value = false
  }
}

const handleReset = () => {
  form.value.share_urls_text = ''
  results.value = []
  summary.value = {
    input_count: 0,
    valid_count: 0,
    dedup_count: 0,
    success_count: 0,
    failed_count: 0
  }
  errorText.value = ''
}

const openLink = (url) => {
  if (!url) return
  window.open(url, '_blank', 'noopener')
}

const copyLink = async (url) => {
  if (!url) return
  try {
    const ok = await copyText(url)
    if (!ok) {
      ElMessage.error('复制失败，请手动复制')
      return
    }
    ElMessage.success('链接已复制')
  } catch {
    ElMessage.error('复制失败，请手动复制')
  }
}

const openRowLink = (row) => {
  const target = row?.watermark_url || row?.share_url || ''
  openLink(target)
}

const successRows = computed(() => results.value.filter((row) => row.status === 'success' && row.watermark_url))

const downloadOne = async (row, options = {}) => {
  const silent = Boolean(options.silent)
  if (!row?.watermark_url) {
    if (!silent) ElMessage.warning('该条目暂无可下载链接')
    return false
  }
  try {
    const response = await fetch(row.watermark_url, { method: 'GET' })
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }
    const blob = await response.blob()
    if (!blob || blob.size <= 0) {
      throw new Error('响应内容为空')
    }
    const filename = buildDownloadFilename(row, response)
    triggerBlobDownload(blob, filename)
    if (!silent) ElMessage.success(`已开始下载：${filename}`)
    return true
  } catch (error) {
    if (!silent) {
      ElMessage.error(error?.message ? `下载失败：${error.message}` : '下载失败')
    }
    return false
  }
}

const downloadAll = async () => {
  if (downloadingAll.value || parsing.value) return
  const rows = successRows.value
  if (!rows.length) {
    ElMessage.warning('暂无可下载结果')
    return
  }
  downloadingAll.value = true
  let successCount = 0
  let failedCount = 0
  try {
    for (const row of rows) {
      const ok = await downloadOne(row, { silent: true })
      if (ok) {
        successCount += 1
      } else {
        failedCount += 1
      }
      await delay(DOWNLOAD_INTERVAL_MS)
    }
    if (failedCount === 0) {
      ElMessage.success(`已触发 ${successCount} 条下载`)
    } else {
      ElMessage.warning(`已触发 ${successCount} 条下载，失败 ${failedCount} 条`)
    }
  } finally {
    downloadingAll.value = false
  }
}
</script>

<style scoped>
.watermark-parse-page {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
}

.action-row {
  display: flex;
  gap: 10px;
}

.share-textarea :deep(.el-textarea__inner) {
  line-height: 1.55;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
}

.input-hint {
  font-size: 12px;
  color: var(--muted);
}

.result-head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.summary-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.summary-tag {
  border-radius: 999px;
}

.result-link-row {
  min-width: 0;
}

.result-link {
  display: inline-block;
  max-width: 100%;
  min-width: 0;
  font-size: 13px;
  color: var(--accent-strong);
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.result-link:hover {
  text-decoration: underline;
}

.ellipsis-text {
  display: inline-block;
  max-width: 100%;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.error-text {
  display: inline-block;
  max-width: 100%;
  min-width: 0;
  color: var(--danger);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.op-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.error-alert {
  border-radius: var(--radius-md);
}

@media (max-width: 960px) {
  .result-head-row {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
