<script setup lang="ts">
import { WarningFilled } from '@element-plus/icons-vue'
import type { ApiErrorInfo } from '../api'
import { OFFICIAL_SUPPORT_URL } from '../supportChannels'

defineProps<{ error: ApiErrorInfo }>()
</script>

<template>
  <section class="safety-block" role="alert" aria-live="assertive">
    <el-icon><WarningFilled /></el-icon>
    <div>
      <p class="eyebrow">SAFETY BLOCK</p>
      <h2>立即停止操作</h2>
      <p class="reason">{{ error.reason || error.message }}</p>
      <ul>
        <li v-if="error.shouldPowerOff !== false">在确保人身安全的前提下断开设备电源。</li>
        <li>不要继续充电、拆机或重复测试。</li>
        <li>{{ error.officialServiceAdvice || '请联系海尔官方售后，由专业人员检查设备。' }}</li>
      </ul>
      <p v-if="error.riskLevel || error.category" class="meta">
        风险等级：{{ error.riskLevel || '高风险' }}<span v-if="error.category"> · {{ error.category }}</span>
      </p>
      <a :href="OFFICIAL_SUPPORT_URL" target="_blank" rel="noopener noreferrer">前往海尔官方联系入口</a>
    </div>
  </section>
</template>

<style scoped>
.safety-block{display:grid;grid-template-columns:44px 1fr;gap:16px;margin:0 0 20px;padding:22px;border:2px solid #d92d20;border-radius:14px;background:#fff1f0;color:#7a271a;box-shadow:0 8px 24px rgba(217,45,32,.1)}.safety-block>.el-icon{font-size:38px;color:#d92d20}.eyebrow{margin:0;color:#b42318;font-size:11px;font-weight:800;letter-spacing:1.2px}.safety-block h2{margin:4px 0 8px;font-size:22px;color:#b42318}.reason{margin:0;font-weight:700;line-height:1.65}.safety-block ul{margin:12px 0;padding-left:20px;line-height:1.75}.meta{font-size:12px;color:#9c3f34}.safety-block a{display:inline-block;margin-top:5px;color:#b42318;font-weight:800;text-decoration:underline}
</style>
