import { createRouter, createWebHistory } from 'vue-router'
import Login from '../views/Login.vue'
import IxBrowserGroups from '../views/IxBrowserGroups.vue'
import TaskManagement from '../views/TaskManagement.vue'
import UserManagement from '../views/UserManagement.vue'
import SystemSettings from '../views/SystemSettings.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: Login, meta: { public: true } },
    { path: '/', redirect: '/sora-accounts' },
    { path: '/ixbrowser-groups', redirect: '/sora-accounts' },
    { path: '/sora-accounts', component: IxBrowserGroups, meta: { title: 'Sora 账号管理' } },
    { path: '/tasks', component: TaskManagement, meta: { title: '任务管理' } },
    { path: '/users', component: UserManagement, meta: { title: '用户管理' } },
    { path: '/settings', component: SystemSettings, meta: { title: '系统设置' } }
  ]
})

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('token')
  if (to.meta.public) {
    if (token && to.path === '/login') {
      next('/sora-accounts')
      return
    }
    next()
    return
  }

  if (!token) {
    next('/login')
    return
  }

  next()
})

export default router
