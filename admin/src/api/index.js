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

export const scanIxBrowserSoraSessionAccounts = async (groupTitle = 'Sora') => {
  const response = await api.post('/ixbrowser/sora-session-accounts', null, {
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
