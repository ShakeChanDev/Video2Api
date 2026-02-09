import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  }
)

export const login = async (username, password) => {
  const formData = new FormData()
  formData.append('username', username)
  formData.append('password', password)
  const response = await api.post('/auth/login', formData)
  return response.data
}

export const getMe = async () => {
  const response = await api.get('/auth/me')
  return response.data
}

export const getIxBrowserGroupWindows = async () => {
  const response = await api.get('/ixbrowser/group-windows')
  return response.data
}

export const listProxies = async (params) => {
  const response = await api.get('/proxies', { params })
  return response.data
}

export const batchImportProxies = async (data) => {
  const response = await api.post('/proxies/batch-import', data, { timeout: 60000 })
  return response.data
}

export const syncPullProxies = async () => {
  const response = await api.post('/proxies/sync/pull', null, { timeout: 120000 })
  return response.data
}

export const syncPushProxies = async (proxyIds = null) => {
  const payload = Array.isArray(proxyIds) && proxyIds.length ? { proxy_ids: proxyIds } : { proxy_ids: null }
  const response = await api.post('/proxies/sync/push', payload, { timeout: 180000 })
  return response.data
}

export const batchUpdateProxies = async (data) => {
  const response = await api.post('/proxies/batch-update', data, { timeout: 180000 })
  return response.data
}

export const batchCheckProxies = async (data) => {
  const response = await api.post('/proxies/batch-check', data, { timeout: 300000 })
  return response.data
}

export const openIxBrowserProfileWindow = async (profileId, groupTitle = 'Sora') => {
  const response = await api.post(`/ixbrowser/profiles/${profileId}/open`, null, {
    params: { group_title: groupTitle }
  })
  return response.data
}

export const scanIxBrowserSoraSessionAccounts = async (groupTitle = 'Sora', profileIds = null) => {
  const payload = Array.isArray(profileIds) && profileIds.length ? { profile_ids: profileIds } : null
  const response = await api.post('/ixbrowser/sora-session-accounts', payload, {
    params: { group_title: groupTitle, with_fallback: true },
    timeout: 180000
  })
  return response.data
}

export const getLatestIxBrowserSoraSessionAccounts = async (groupTitle = 'Sora', withFallback = true) => {
  const response = await api.get('/ixbrowser/sora-session-accounts/latest', {
    params: { group_title: groupTitle, with_fallback: withFallback }
  })
  return response.data
}

export const getIxBrowserSoraSessionScanHistory = async (groupTitle = 'Sora', limit = 10) => {
  const response = await api.get('/ixbrowser/sora-session-accounts/history', {
    params: { group_title: groupTitle, limit }
  })
  return response.data
}

export const getIxBrowserSoraSessionByRun = async (runId, withFallback = false) => {
  const response = await api.get(`/ixbrowser/sora-session-accounts/history/${runId}`, {
    params: { with_fallback: withFallback }
  })
  return response.data
}

export const createIxBrowserSoraGenerateJob = async (data) => {
  const response = await api.post('/ixbrowser/sora-generate', data, {
    timeout: 60000
  })
  return response.data
}

export const createSoraJob = async (data) => {
  const response = await api.post('/sora/jobs', data, {
    timeout: 60000
  })
  return response.data
}

export const listSoraJobs = async (params) => {
  const response = await api.get('/sora/jobs', { params })
  return response.data
}

export const getSoraJob = async (jobId) => {
  const response = await api.get(`/sora/jobs/${jobId}`)
  return response.data
}

export const retrySoraJob = async (jobId) => {
  const response = await api.post(`/sora/jobs/${jobId}/retry`)
  return response.data
}

export const cancelSoraJob = async (jobId) => {
  const response = await api.post(`/sora/jobs/${jobId}/cancel`)
  return response.data
}

export const listSoraJobEvents = async (jobId) => {
  const response = await api.get(`/sora/jobs/${jobId}/events`)
  return response.data
}

export const buildSoraJobStreamUrl = (params = {}) => {
  const token = localStorage.getItem('token')
  const query = new URLSearchParams()
  if (params?.group_title) query.set('group_title', params.group_title)
  if (params?.profile_id !== undefined && params?.profile_id !== null) query.set('profile_id', String(params.profile_id))
  if (params?.status) query.set('status', params.status)
  if (params?.phase) query.set('phase', params.phase)
  if (params?.keyword) query.set('keyword', params.keyword)
  if (params?.limit !== undefined && params?.limit !== null) query.set('limit', String(params.limit))
  if (params?.with_events !== undefined && params?.with_events !== null) {
    query.set('with_events', params.with_events ? 'true' : 'false')
  }
  if (token) query.set('token', token)
  return `/api/v1/sora/jobs/stream?${query.toString()}`
}

export const retrySoraJobWatermark = async (jobId) => {
  const response = await api.post(`/sora/jobs/${jobId}/watermark/retry`)
  return response.data
}

export const parseSoraWatermarkLink = async (data) => {
  const response = await api.post('/sora/watermark/parse', data)
  return response.data
}

export const getSoraAccountWeights = async (groupTitle = 'Sora', limit = 100) => {
  const response = await api.get('/sora/accounts/weights', {
    params: { group_title: groupTitle, limit }
  })
  return response.data
}

export const getIxBrowserSoraGenerateJob = async (jobId) => {
  const response = await api.get(`/ixbrowser/sora-generate-jobs/${jobId}`)
  return response.data
}

export const publishIxBrowserSoraGenerateJob = async (jobId) => {
  const response = await api.post(`/ixbrowser/sora-generate-jobs/${jobId}/publish`)
  return response.data
}

export const fetchIxBrowserSoraGenerationId = async (jobId) => {
  const response = await api.post(`/ixbrowser/sora-generate-jobs/${jobId}/genid`)
  return response.data
}

export const listIxBrowserSoraGenerateJobs = async (params) => {
  const response = await api.get('/ixbrowser/sora-generate-jobs', { params })
  return response.data
}

export const listAdminUsers = async () => {
  const response = await api.get('/admin/users')
  return response.data
}

export const createAdminUser = async (data) => {
  const response = await api.post('/admin/users', data)
  return response.data
}

export const updateAdminUser = async (userId, data) => {
  const response = await api.patch(`/admin/users/${userId}`, data)
  return response.data
}

export const resetAdminUserPassword = async (userId, data) => {
  const response = await api.post(`/admin/users/${userId}/reset-password`, data)
  return response.data
}

export const getSystemSettings = async () => {
  const response = await api.get('/admin/settings/system')
  return response.data
}

export const updateSystemSettings = async (data) => {
  const response = await api.put('/admin/settings/system', data)
  return response.data
}

export const getScanSchedulerConfig = async () => {
  const response = await api.get('/admin/settings/scheduler/scan')
  return response.data
}

export const updateScanSchedulerConfig = async (data) => {
  const response = await api.put('/admin/settings/scheduler/scan', data)
  return response.data
}

export const getWatermarkFreeConfig = async () => {
  const response = await api.get('/admin/settings/watermark-free')
  return response.data
}

export const updateWatermarkFreeConfig = async (data) => {
  const response = await api.put('/admin/settings/watermark-free', data)
  return response.data
}

export const listSystemLogsV2 = async (params) => {
  const response = await api.get('/admin/logs', { params })
  return response.data
}

export const getSystemLogStats = async (params) => {
  const response = await api.get('/admin/logs/stats', { params })
  return response.data
}

export const buildSystemLogStreamUrl = (params = {}) => {
  const token = localStorage.getItem('token')
  const query = new URLSearchParams()
  if (params?.source) query.set('source', params.source)
  if (token) query.set('token', token)
  return `/api/v1/admin/logs/stream?${query.toString()}`
}

export const createNurtureBatch = async (data) => {
  const response = await api.post('/nurture/batches', data, {
    timeout: 60000
  })
  return response.data
}

export const listNurtureBatches = async (params) => {
  const response = await api.get('/nurture/batches', { params })
  return response.data
}

export const getNurtureBatch = async (batchId) => {
  const response = await api.get(`/nurture/batches/${batchId}`)
  return response.data
}

export const listNurtureJobs = async (batchId, params) => {
  const response = await api.get(`/nurture/batches/${batchId}/jobs`, { params })
  return response.data
}

export const getNurtureJob = async (jobId) => {
  const response = await api.get(`/nurture/jobs/${jobId}`)
  return response.data
}

export const cancelNurtureBatch = async (batchId) => {
  const response = await api.post(`/nurture/batches/${batchId}/cancel`)
  return response.data
}
