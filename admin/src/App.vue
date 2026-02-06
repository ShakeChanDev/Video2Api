<template>
  <div class="console-root">
    <template v-if="isLoginPage">
      <router-view />
    </template>
    <template v-else>
      <div class="bg-orb orb-a" />
      <div class="bg-orb orb-b" />
      <div class="bg-orb orb-c" />

      <aside class="sidebar glass-panel">
        <div class="brand-block">
          <div class="brand-title">Video2Api</div>
        </div>

        <nav class="menu-wrap">
          <div v-for="group in navGroups" :key="group.title" class="menu-group">
            <div class="menu-title">{{ group.title }}</div>
            <button
              v-for="item in group.items"
              :key="item.path"
              class="menu-item"
              :class="{ active: route.path === item.path }"
              @click="go(item.path)"
            >
              <span class="menu-icon" />
              <span class="menu-label">{{ item.label }}</span>
            </button>
          </div>
        </nav>

        <div class="sidebar-foot">
          <span>桌面端 V1</span>
        </div>
      </aside>

      <main class="main-area">
        <div class="topbar-shell">
          <header class="topbar glass-panel">
            <div>
              <div class="page-title">{{ currentPageTitle }}</div>
            </div>
            <div class="top-actions">
              <span class="user-pill">{{ currentUser?.username || 'Admin' }}</span>
              <button class="logout-btn" @click="logout">退出</button>
            </div>
          </header>
        </div>

        <section class="page-body">
          <div class="page-shell">
            <div class="page-scroll">
              <router-view />
            </div>
          </div>
        </section>
      </main>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const navGroups = [
  {
    title: '账号',
    items: [{ path: '/sora-accounts', label: 'Sora 账号管理' }]
  },
  {
    title: '任务',
    items: [
      { path: '/tasks', label: '任务管理' },
      { path: '/nurture', label: '养号任务' }
    ]
  },
  {
    title: '系统',
    items: [
      { path: '/users', label: '用户管理' },
      { path: '/settings', label: '系统设置' },
      { path: '/logs', label: '日志中心' }
    ]
  }
]

const isLoginPage = computed(() => route.path === '/login')
const currentPageTitle = computed(() => route.meta?.title || '后台管理')
const currentUser = computed(() => {
  try {
    return JSON.parse(localStorage.getItem('user') || '{}')
  } catch {
    return null
  }
})

const go = (path) => {
  if (route.path !== path) {
    router.push(path)
  }
}

const logout = () => {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
  router.push('/login')
}
</script>

<style scoped>
.console-root {
  min-height: 100vh;
  display: flex;
  background: transparent;
  position: relative;
  overflow: hidden;
}

.bg-orb {
  position: fixed;
  border-radius: 999px;
  pointer-events: none;
  filter: blur(22px);
  opacity: 0.5;
}

.orb-a {
  width: 320px;
  height: 320px;
  left: 54%;
  top: -140px;
  background: rgba(14, 165, 164, 0.18);
}

.orb-b {
  width: 280px;
  height: 280px;
  left: 2%;
  bottom: 24px;
  background: rgba(245, 158, 11, 0.16);
}

.orb-c {
  width: 300px;
  height: 300px;
  right: -90px;
  bottom: 10%;
  background: rgba(59, 130, 246, 0.12);
}

.glass-panel {
  border: 1px solid var(--border);
  background: var(--card);
  box-shadow: var(--shadow-xs);
}

.sidebar {
  width: 236px;
  margin: 16px;
  border-radius: var(--radius-xl);
  padding: 18px 14px;
  display: flex;
  flex-direction: column;
  z-index: 1;
  background: var(--card);
  box-shadow: var(--shadow-soft);
}

.brand-block {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 2px 6px 14px;
  margin-bottom: 14px;
  border-bottom: 1px dashed rgba(148, 163, 184, 0.28);
}

.brand-title {
  color: var(--ink);
  font-weight: 800;
  letter-spacing: 0.4px;
  font-size: 18px;
}

.menu-wrap {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.menu-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.menu-title {
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: rgba(15, 23, 42, 0.4);
  padding: 0 8px;
}

.menu-item {
  border: 1px solid transparent;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  font-size: 14px;
  background: rgba(255, 255, 255, 0.65);
  color: var(--ink);
  cursor: pointer;
  transition: all 0.2s ease;
  position: relative;
}

.menu-icon {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.28);
}

.menu-item:hover {
  transform: translateX(2px);
  background: rgba(255, 255, 255, 0.9);
  border-color: rgba(14, 165, 164, 0.4);
}

.menu-item.active {
  color: var(--accent-strong);
  background: rgba(255, 255, 255, 0.95);
  border-color: rgba(14, 165, 164, 0.45);
  box-shadow: var(--shadow-xs);
}

.menu-item.active .menu-icon {
  background: var(--accent);
}

.sidebar-foot {
  margin-top: auto;
  color: rgba(15, 23, 42, 0.6);
  font-size: 12px;
  padding: 8px 4px 0;
}

.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 0 16px 16px 0;
  min-width: 0;
  gap: 12px;
}

.topbar-shell {
  padding: var(--page-padding) var(--page-padding) 0;
}

.topbar {
  min-height: 74px;
  border-radius: var(--radius-lg);
  padding: 14px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  z-index: 1;
}

.page-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--ink);
}


.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.user-pill {
  padding: 8px 12px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.04);
  border: 1px solid rgba(15, 23, 42, 0.12);
  color: var(--ink);
  font-weight: 600;
}

.logout-btn {
  border: 0;
  border-radius: 10px;
  padding: 9px 13px;
  cursor: pointer;
  color: #fff;
  background: linear-gradient(140deg, #0f172a 0%, #1f2937 100%);
}

.page-body {
  flex: 1;
  overflow: visible;
  margin-top: 0;
  z-index: 1;
}

.page-shell {
  height: 100%;
  overflow: visible;
}

.page-scroll {
  height: 100%;
  overflow: auto;
  padding: var(--page-padding);
}

@media (max-width: 1080px) {
  .sidebar {
    width: 208px;
  }
}
</style>
