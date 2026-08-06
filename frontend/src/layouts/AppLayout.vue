<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores'
import { House, Box, Reading, ChatDotRound, FirstAidKit, Clock, Setting, SwitchButton, Fold } from '@element-plus/icons-vue'
const route = useRoute(); const router = useRouter(); const auth = useAuthStore()
// 小屏下侧栏改为抽屉：固定 250px 侧栏在 390px 视口会吃掉 2/3 屏幕，
// 输入框直接看不见（2026-08-05 playwright 实测）
const navOpen = ref(false)
// 点导航跳转后必须自动收起，否则用户落在新页面却仍被遮罩挡住
watch(() => route.fullPath, () => { navOpen.value = false })
const menus = [
  { path: '/', label: '工作台', icon: House }, { path: '/devices', label: '我的设备', icon: Box },
  { path: '/guides', label: '使用指导', icon: Reading }, { path: '/chat', label: '智能客服', icon: ChatDotRound },
  { path: '/diagnostics/new', label: '开始诊断', icon: FirstAidKit },
  { path: '/history', label: '诊断历史', icon: Clock },
]
async function logout() { try { await auth.logout() } finally { await router.push('/login') } }
</script>
<template>
  <div class="shell" :class="{ 'nav-open': navOpen }">
    <div v-if="navOpen" class="nav-scrim" @click="navOpen = false" aria-hidden="true"></div>
    <aside class="sidebar">
      <router-link to="/" class="logo"><span class="logo-mark">R</span><span><strong>RobotCare</strong><small>AI 安全排障助手</small></span></router-link>
      <nav>
        <router-link v-for="item in menus" :key="item.path" :to="item.path" :class="{ active: route.path === item.path || (item.path !== '/' && route.path.startsWith(item.path)) }">
          <el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span>
        </router-link>
      </nav>
      <div class="safe-card"><span class="status-dot"></span><strong>安全模式已开启</strong><p>仅提供清洁、检查、配网和复位等非拆机操作。</p></div>
      <router-link v-if="auth.canViewOperations" to="/admin" class="admin-link"><el-icon><Setting /></el-icon>管理后台</router-link>
      <button class="profile" @click="logout"><span class="avatar">{{ (auth.user?.name || auth.user?.full_name || auth.user?.email || 'U')[0].toUpperCase() }}</span><span><strong>{{ auth.user?.name || auth.user?.full_name || '用户' }}</strong><small>{{ auth.user?.email }}</small></span><el-icon><SwitchButton /></el-icon></button>
    </aside>
    <main>
      <header class="topbar">
        <button class="nav-toggle" type="button" :aria-expanded="navOpen" aria-label="打开导航菜单" @click="navOpen = !navOpen">
          <el-icon><Fold /></el-icon>
        </button>
        <span class="topbar-title">第三方智能使用指导与故障排查平台</span>
        <span class="disclaimer">非海尔官方服务</span>
      </header>
      <router-view />
    </main>
  </div>
</template>
<style scoped>
.shell{min-height:100vh;display:grid;grid-template-columns:250px 1fr}.sidebar{position:fixed;inset:0 auto 0 0;width:250px;background:#fff;border-right:1px solid var(--line);padding:24px 16px;display:flex;flex-direction:column;z-index:3}.logo{display:flex;gap:11px;align-items:center;padding:0 9px 25px}.logo-mark{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:var(--brand);color:#fff;font-size:20px;font-weight:800}.logo strong{font-size:18px;display:block}.logo small{display:block;color:var(--muted);margin-top:2px;font-size:11px}nav{display:flex;flex-direction:column;gap:5px}nav a,.admin-link{height:44px;border-radius:10px;padding:0 13px;display:flex;align-items:center;gap:11px;color:#52635d;font-size:14px;font-weight:600}nav a:hover,.admin-link:hover{background:#f2f6f4;color:var(--brand)}nav a.active{background:var(--soft);color:var(--brand)}.safe-card{margin-top:auto;background:#f0faf6;border:1px solid #caeadc;padding:14px;border-radius:12px;font-size:12px}.safe-card strong{font-size:12px}.safe-card p{color:#63766e;line-height:1.55;margin:8px 0 0}.admin-link{margin-top:10px}.profile{margin:14px -4px -8px;padding:12px 8px;border:0;border-top:1px solid var(--line);background:white;display:grid;grid-template-columns:36px 1fr auto;gap:9px;align-items:center;text-align:left;cursor:pointer}.profile .avatar{width:34px;height:34px;border-radius:10px;background:#dff4eb;color:var(--brand);display:grid;place-items:center;font-weight:800}.profile strong,.profile small{display:block;max-width:130px;overflow:hidden;text-overflow:ellipsis}.profile strong{font-size:13px}.profile small{font-size:10px;color:var(--muted);margin-top:3px}main{grid-column:2;min-width:0}.topbar{height:55px;background:rgba(255,255,255,.88);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 36px;color:var(--muted);font-size:12px;position:sticky;top:0;z-index:2;backdrop-filter:blur(8px)}.disclaimer{padding:5px 9px;background:#fff4e6;color:#b35c00;border-radius:6px;font-weight:650}
.nav-toggle{display:none;align-items:center;justify-content:center;width:38px;height:38px;margin-right:10px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--ink);cursor:pointer;flex:none}
.topbar-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav-scrim{position:fixed;inset:0;background:rgba(12,32,25,.42);z-index:2}

/* 移动端：单列布局 + 侧栏抽屉化。
   此前 body{min-width:1100px} 让这些断点永远触发不到（已在 styles.css 移除）。 */
@media (max-width: 900px) {
  .shell{grid-template-columns:1fr}
  .sidebar{width:262px;transform:translateX(-100%);transition:transform .22s ease;box-shadow:0 12px 40px rgba(12,32,25,.18)}
  .shell.nav-open .sidebar{transform:none}
  main{grid-column:1}
  .topbar{padding:0 14px;gap:8px}
  .nav-toggle{display:inline-flex}
  .disclaimer{flex:none}
}
@media (max-width: 420px) {
  .topbar-title{font-size:11px}
  .disclaimer{padding:4px 7px;font-size:11px}
}
</style>
