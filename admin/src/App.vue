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
          <div class="brand-mark">V2</div>
          <div>
            <div class="brand-title">Video2Api</div>
            <div class="brand-subtitle">Admin Console</div>
          </div>
        </div>

        <nav class="menu-wrap">
          <button
            v-for="item in navItems"
            :key="item.path"
            class="menu-item"
            :class="{ active: route.path === item.path }"
            @click="go(item.path)"
          >
            <span class="menu-dot" />
            <span>{{ item.label }}</span>
          </button>
        </nav>

        <div class="sidebar-foot">
          <span>桌面端 V1</span>
        </div>
      </aside>

      <main class="main-area">
        <header class="topbar glass-panel">
          <div>
            <div class="page-title">{{ currentPageTitle }}</div>
            <div class="page-subtitle">Glassmorphism + Ethereal Gradient</div>
          </div>
          <div class="top-actions">
            <span class="user-pill">{{ currentUser?.username || 'Admin' }}</span>
            <button class="logout-btn" @click="logout">退出</button>
          </div>
        </header>

        <section class="page-body">
          <router-view />
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

const navItems = [
  { path: '/sora-accounts', label: 'Sora 账号管理' },
  { path: '/tasks', label: '任务管理' },
  { path: '/users', label: '用户管理' },
  { path: '/settings', label: '系统设置' }
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
  background:
    radial-gradient(1200px 600px at 12% -8%, rgba(39, 164, 243, 0.34), transparent 68%),
    radial-gradient(900px 520px at 92% 4%, rgba(19, 196, 170, 0.2), transparent 62%),
    linear-gradient(155deg, #ebf8ff 0%, #edf7f6 50%, #f6f9ff 100%);
  position: relative;
  overflow: hidden;
}

.bg-orb {
  position: fixed;
  border-radius: 999px;
  pointer-events: none;
  filter: blur(18px);
  opacity: 0.6;
}

.orb-a {
  width: 320px;
  height: 320px;
  left: 52%;
  top: -120px;
  background: rgba(45, 152, 236, 0.22);
}

.orb-b {
  width: 280px;
  height: 280px;
  left: 2%;
  bottom: 24px;
  background: rgba(33, 208, 174, 0.17);
}

.orb-c {
  width: 300px;
  height: 300px;
  right: -90px;
  bottom: 10%;
  background: rgba(98, 148, 244, 0.19);
}

.glass-panel {
  border: 1px solid rgba(255, 255, 255, 0.45);
  background: linear-gradient(140deg, rgba(255, 255, 255, 0.52) 0%, rgba(255, 255, 255, 0.24) 100%);
  backdrop-filter: blur(14px) saturate(140%);
  -webkit-backdrop-filter: blur(14px) saturate(140%);
  box-shadow: 0 18px 38px rgba(16, 24, 40, 0.1);
}

.sidebar {
  width: 252px;
  margin: 14px;
  border-radius: 20px;
  padding: 18px 14px;
  display: flex;
  flex-direction: column;
  z-index: 1;
}

.brand-block {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
}

.brand-mark {
  width: 42px;
  height: 42px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  color: #f8fafc;
  font-weight: 700;
  background: linear-gradient(155deg, #0c4a6e 0%, #0f766e 100%);
  box-shadow: 0 8px 20px rgba(12, 74, 110, 0.34);
}

.brand-title {
  color: #0f172a;
  font-weight: 700;
  letter-spacing: 0.2px;
}

.brand-subtitle {
  font-size: 12px;
  color: #475569;
}

.menu-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.menu-item {
  border: 1px solid transparent;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 12px;
  border-radius: 12px;
  font-size: 14px;
  background: rgba(255, 255, 255, 0.38);
  color: #0f172a;
  cursor: pointer;
  transition: all 0.2s ease;
}

.menu-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.35);
}

.menu-item:hover {
  transform: translateX(2px);
  background: rgba(255, 255, 255, 0.58);
  border-color: rgba(148, 163, 184, 0.35);
}

.menu-item.active {
  color: #083344;
  background: linear-gradient(120deg, rgba(255, 255, 255, 0.7) 0%, rgba(207, 250, 254, 0.52) 100%);
  border-color: rgba(103, 232, 249, 0.42);
}

.menu-item.active .menu-dot {
  background: #0e7490;
}

.sidebar-foot {
  margin-top: auto;
  color: #475569;
  font-size: 12px;
  padding: 8px 4px 0;
}

.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 14px 14px 14px 0;
  min-width: 0;
}

.topbar {
  min-height: 74px;
  border-radius: 18px;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  z-index: 1;
}

.page-title {
  font-size: 18px;
  font-weight: 700;
  color: #0f172a;
}

.page-subtitle {
  font-size: 12px;
  color: #475569;
  margin-top: 4px;
}

.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.user-pill {
  padding: 8px 12px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.52);
  border: 1px solid rgba(148, 163, 184, 0.35);
  color: #0f172a;
  font-weight: 600;
}

.logout-btn {
  border: 0;
  border-radius: 10px;
  padding: 9px 13px;
  cursor: pointer;
  color: #f8fafc;
  background: linear-gradient(140deg, #0f172a 0%, #1f2937 100%);
}

.page-body {
  flex: 1;
  overflow: auto;
  margin-top: 12px;
  z-index: 1;
}

@media (max-width: 1080px) {
  .sidebar {
    width: 208px;
  }
}
</style>
