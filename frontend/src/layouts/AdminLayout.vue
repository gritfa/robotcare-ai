<script setup lang="ts">
import { ArrowLeft, DataAnalysis, Lock } from '@element-plus/icons-vue'
</script>

<template>
  <div class="admin-shell">
    <aside>
      <div class="title">
        <span>RC</span>
        <div><b>管理后台</b><small>RobotCare AI</small></div>
      </div>
      <router-link to="/admin"><el-icon><DataAnalysis /></el-icon>运营与安全概览</router-link>
      <div class="rbac-note">
        <el-icon><Lock /></el-icon>
        <div><b>后端 RBAC</b><small>所有数据与管理操作均需管理员令牌。</small></div>
      </div>
      <router-link class="back" to="/"><el-icon><ArrowLeft /></el-icon>返回用户端</router-link>
    </aside>
    <main><router-view /></main>
  </div>
</template>

<style scoped>
.admin-shell{min-height:100vh;display:grid;grid-template-columns:230px 1fr}.admin-shell aside{position:fixed;inset:0 auto 0 0;width:230px;background:#102a23;color:#c6d8d2;padding:24px 16px;display:flex;flex-direction:column;gap:6px}.title{display:flex;gap:10px;align-items:center;margin:0 8px 28px}.title>span{background:#24a77a;color:#fff;border-radius:9px;padding:9px;font-weight:800}.title b,.title small{display:block}.title small{font-size:11px;opacity:.65;margin-top:3px}a{padding:12px;border-radius:8px;display:flex;gap:10px;align-items:center;font-size:14px}a:hover,a.router-link-exact-active{background:#20463b;color:#fff}.rbac-note{margin-top:20px;padding:14px 12px;border:1px solid #315348;border-radius:10px;display:flex;gap:9px;color:#a9c3ba}.rbac-note .el-icon{margin-top:2px}.rbac-note b,.rbac-note small{display:block}.rbac-note b{font-size:12px;color:#d5e4df}.rbac-note small{font-size:10px;line-height:1.55;margin-top:5px}.back{margin-top:auto}main{grid-column:2;min-width:0;background:#f4f7f6}
</style>
