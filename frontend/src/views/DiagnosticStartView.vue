<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Camera, Close, InfoFilled, Plus, Warning } from '@element-plus/icons-vue'
import { apiError, deviceApi, diagnosticApi, modelApi, parseApiError, type ApiErrorInfo } from '../api'
import SafetyBlockCard from '../components/SafetyBlockCard.vue'
import { diagnosticOptionKey, robotModelIdForDevice, visibleDiagnosticOptions } from '../diagnosticOptions'
import type { Device, DiagnosticOption } from '../types'

interface SelectedImage {
  id: string
  file: File
  previewUrl: string
}

const MAX_IMAGES = 5
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const ACCEPTED_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])

const devices = ref<Device[]>([])
const diagnosticOptions = ref<DiagnosticOption[]>([])
const selectedOptionKey = ref('')
const selectedImages = ref<SelectedImage[]>([])
const fileInput = ref<HTMLInputElement>()
const submitting = ref(false)
const initialLoading = ref(true)
const optionsLoading = ref(false)
const optionsError = ref('')
const safetyError = ref<ApiErrorInfo | null>(null)
const categoryMismatch = ref<ApiErrorInfo | null>(null)
const route = useRoute()
const router = useRouter()
const form = reactive({ device_id: '', issue_category_code: '', issue_description: '', error_code: '' })
let optionsRequestId = 0

watch(() => form.device_id, async (deviceId) => {
  const requestId = ++optionsRequestId

  // Never carry an option from one physical device/model into another request.
  selectedOptionKey.value = ''
  form.issue_category_code = ''
  diagnosticOptions.value = []
  optionsError.value = ''

  const modelId = robotModelIdForDevice(devices.value, deviceId)
  if (modelId === null) {
    optionsLoading.value = false
    return
  }

  optionsLoading.value = true
  try {
    const options = await modelApi.diagnosticOptions(modelId)
    // Ignore a slow response from a device the user has already switched away from.
    if (requestId !== optionsRequestId) return
    diagnosticOptions.value = visibleDiagnosticOptions(options)
  } catch (error) {
    if (requestId !== optionsRequestId) return
    optionsError.value = apiError(error, '诊断流程加载失败，请稍后重试')
  } finally {
    if (requestId === optionsRequestId) optionsLoading.value = false
  }
})

onMounted(async () => {
  try {
    devices.value = await deviceApi.list()
    const requestedDeviceId = String(route.query.device || '')
    const requestedDevice = devices.value.find(device => String(device.id) === requestedDeviceId)
    form.device_id = String(requestedDevice?.id || devices.value[0]?.id || '')
  } catch (error) {
    ElMessage.error(apiError(error, '设备列表加载失败'))
  } finally {
    initialLoading.value = false
  }
})

onBeforeUnmount(() => selectedImages.value.forEach(image => URL.revokeObjectURL(image.previewUrl)))

function selectDiagnosticOption(option: DiagnosticOption) {
  selectedOptionKey.value = diagnosticOptionKey(option)
  form.issue_category_code = option.issue_category_code
  categoryMismatch.value = null
  safetyError.value = null
}

function openFilePicker() {
  fileInput.value?.click()
}

function selectImages(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length) return

  const available = MAX_IMAGES - selectedImages.value.length
  if (available <= 0) {
    ElMessage.warning(`最多上传 ${MAX_IMAGES} 张图片`)
    return
  }

  let rejected = 0
  const accepted = files.slice(0, available).filter((file) => {
    const valid = ACCEPTED_IMAGE_TYPES.has(file.type) && file.size <= MAX_IMAGE_BYTES
    if (!valid) rejected += 1
    return valid
  })
  if (files.length > available) rejected += files.length - available

  selectedImages.value.push(...accepted.map(file => ({
    id: `${file.name}-${file.size}-${file.lastModified}-${crypto.randomUUID()}`,
    file,
    previewUrl: URL.createObjectURL(file),
  })))
  if (rejected) ElMessage.warning('仅支持不超过 5 MB 的 JPG、PNG 或 WebP 图片，且最多上传 5 张')
}

function removeImage(id: string) {
  const index = selectedImages.value.findIndex(image => image.id === id)
  if (index < 0) return
  URL.revokeObjectURL(selectedImages.value[index].previewUrl)
  selectedImages.value.splice(index, 1)
}

function categoryLabel(code: string) {
  const option = diagnosticOptions.value.find(item => item.issue_category_code === code)
  return option ? `${option.issue_category_name} · ${option.title}` : code
}

async function retryWithCategory(code: string) {
  const option = diagnosticOptions.value.find(item => item.issue_category_code === code)
  if (!option) {
    ElMessage.error('建议的诊断类型当前不可用，请刷新页面或联系售后')
    return
  }
  selectedOptionKey.value = diagnosticOptionKey(option)
  form.issue_category_code = option.issue_category_code
  categoryMismatch.value = null
  await submit(false)
}

async function submit(confirmCategoryMismatch = false) {
  if (!form.device_id || !form.issue_category_code || form.issue_description.trim().length < 3) {
    return ElMessage.warning('请选择设备和问题类型，并至少输入 3 个字的问题描述')
  }

  submitting.value = true
  safetyError.value = null
  try {
    const diagnostic = await diagnosticApi.create({
      device_id: form.device_id,
      issue_category_code: form.issue_category_code,
      issue_description: form.issue_description.trim(),
      error_code: form.error_code.trim() || undefined,
      confirm_category_mismatch: confirmCategoryMismatch || undefined,
    })

    let failedUploads = 0
    for (const image of selectedImages.value) {
      try {
        await diagnosticApi.uploadAttachment(diagnostic.id, image.file)
      } catch {
        failedUploads += 1
      }
    }
    if (failedUploads) ElMessage.warning(`诊断已创建，但有 ${failedUploads} 张图片上传失败`)
    await router.push(`/diagnostics/${diagnostic.id}`)
  } catch (error) {
    const parsed = parseApiError(error, '诊断创建失败，请确认当前流程仍然可用')
    if (parsed.code === 'SAFETY_BLOCKED') {
      safetyError.value = parsed
      categoryMismatch.value = null
    } else if (parsed.code === 'ISSUE_CATEGORY_MISMATCH') {
      categoryMismatch.value = parsed
    } else {
      ElMessage.error(parsed.message)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="page narrow" v-loading="initialLoading">
    <div class="page-head">
      <div>
        <h1>开始故障排查</h1>
        <p>选择你的设备后，系统只展示该型号已经发布的安全诊断流程。</p>
      </div>
    </div>

    <SafetyBlockCard v-if="safetyError" :error="safetyError" />

    <div v-if="devices.length" class="form-grid">
      <section class="panel form-panel">
        <el-form label-position="top" size="large">
          <el-form-item label="选择设备" required>
            <el-select v-model="form.device_id" style="width: 100%" placeholder="请选择需要排查的设备">
              <el-option
                v-for="device in devices"
                :key="device.id"
                :value="String(device.id)"
                :label="`${device.nickname} · ${device.robot_model?.code || '未知型号'}`"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="问题类型" required>
            <div v-loading="optionsLoading" class="option-state">
              <el-alert v-if="optionsError" :title="optionsError" type="error" :closable="false" show-icon />
              <el-empty
                v-else-if="!optionsLoading && !diagnosticOptions.length"
                description="该型号暂未发布可用的诊断流程"
                :image-size="72"
              />
              <div v-else class="categories">
                <button
                  v-for="option in diagnosticOptions"
                  :key="diagnosticOptionKey(option)"
                  type="button"
                  :class="{ active: selectedOptionKey === diagnosticOptionKey(option) }"
                  @click="selectDiagnosticOption(option)"
                >
                  <span></span>
                  <b>{{ option.title }}</b>
                  <small>{{ option.issue_category_name }}</small>
                </button>
              </div>
            </div>
          </el-form-item>

          <el-form-item label="具体发生了什么？" required>
            <el-input
              v-model="form.issue_description"
              type="textarea"
              :rows="5"
              maxlength="4000"
              show-word-limit
              placeholder="例如：扫地机器人清扫途中突然停止，主刷图标闪烁，清理可见毛发后仍无法启动。"
            />
          </el-form-item>
          <el-form-item label="错误码（选填）">
            <el-input v-model="form.error_code" maxlength="100" placeholder="例如：E01；没有错误码可以留空" />
          </el-form-item>

          <div class="upload-box">
            <div class="upload-heading">
              <el-icon><Camera /></el-icon>
              <div>
                <b>故障图片附件（选填）</b>
                <p>图片仅用于保留故障证据并附入售后记录，不参与 AI 自动判断。</p>
              </div>
              <span>{{ selectedImages.length }}/{{ MAX_IMAGES }}</span>
            </div>
            <input ref="fileInput" class="file-input" type="file" accept="image/jpeg,image/png,image/webp" multiple @change="selectImages" />
            <div v-if="selectedImages.length" class="preview-grid">
              <div v-for="image in selectedImages" :key="image.id" class="preview-item">
                <img :src="image.previewUrl" :alt="image.file.name" />
                <button type="button" aria-label="移除图片" @click="removeImage(image.id)"><el-icon><Close /></el-icon></button>
                <small :title="image.file.name">{{ image.file.name }}</small>
              </div>
              <button v-if="selectedImages.length < MAX_IMAGES" class="add-image" type="button" @click="openFilePicker">
                <el-icon><Plus /></el-icon><span>继续添加</span>
              </button>
            </div>
            <button v-else class="upload-trigger" type="button" @click="openFilePicker">
              <el-icon><Plus /></el-icon>
              <b>选择故障照片或截图</b>
              <span>JPG、PNG、WebP，单张不超过 5 MB，最多 5 张</span>
            </button>
          </div>

          <section v-if="categoryMismatch" class="category-confirmation" role="alert">
            <div>
              <el-icon><Warning /></el-icon>
              <div>
                <h3>问题描述与所选类型可能不一致</h3>
                <p>为避免执行错误流程，请改选建议类型，或明确确认仍使用原类型。</p>
              </div>
            </div>
            <div v-if="categoryMismatch.suggestedCategories.length" class="suggestions">
              <span>系统候选</span>
              <el-button
                v-for="code in categoryMismatch.suggestedCategories"
                :key="code"
                plain
                type="warning"
                :disabled="submitting"
                @click="retryWithCategory(code)"
              >改选“{{ categoryLabel(code) }}”并重试</el-button>
            </div>
            <el-button
              v-if="categoryMismatch.requiresConfirmation"
              type="danger"
              plain
              :loading="submitting"
              @click="submit(true)"
            >我已核对，仍使用“{{ categoryLabel(categoryMismatch.selectedCategory || form.issue_category_code) }}”</el-button>
          </section>

          <el-button
            class="brand-button submit"
            type="primary"
            :loading="submitting"
            :disabled="optionsLoading || !diagnosticOptions.length"
            @click="submit()"
          >创建诊断并查看第一步</el-button>
        </el-form>
      </section>

      <aside>
        <div class="panel notice">
          <el-icon><InfoFilled /></el-icon>
          <h3>开始前请注意</h3>
          <p>一次只执行一个步骤，并在完成后反馈是否解决。请不要自行拆机或接触内部电路。</p>
        </div>
        <div class="stop">
          <el-icon><Warning /></el-icon>
          <div>
            <b>立即停止自助排查</b>
            <p>出现异味、冒烟、异常高温、液体进入机身或电池鼓包时，请断电并联系官方售后。</p>
          </div>
        </div>
      </aside>
    </div>

    <div v-else class="panel empty">
      <h3>请先添加设备</h3>
      <p>诊断流程需要根据具体型号匹配。</p>
      <el-button class="brand-button" type="primary" @click="router.push('/devices')">前往添加设备</el-button>
    </div>
  </div>
</template>

<style scoped>
.narrow{max-width:1220px}.form-grid{display:grid;grid-template-columns:1fr 300px;gap:20px}.form-panel{padding:30px}.option-state{width:100%;min-height:90px}.categories{display:grid;grid-template-columns:1fr 1fr;gap:9px;width:100%}.categories button{background:#fff;border:1px solid var(--line);padding:12px;text-align:left;border-radius:9px;color:#53655e;cursor:pointer;display:grid;grid-template-columns:16px 1fr;align-items:center}.categories button span{grid-row:1/3;display:inline-block;width:8px;height:8px;border-radius:50%;background:#cad6d1;margin-right:8px}.categories button b{font-size:13px}.categories button small{grid-column:2;color:var(--muted);margin-top:3px}.categories button.active{border-color:var(--brand);background:var(--soft);color:var(--brand);font-weight:700}.categories button.active span{background:var(--brand)}.upload-box{border:1px dashed #becdc7;border-radius:12px;padding:17px;background:#f8faf9}.upload-heading{display:grid;grid-template-columns:32px 1fr auto;gap:12px;align-items:center;color:#84908b}.upload-heading>.el-icon{font-size:24px}.upload-heading b{font-size:13px;color:#53655e}.upload-heading p{font-size:11px;margin:5px 0 0;line-height:1.5}.upload-heading>span{font-size:12px}.file-input{display:none}.upload-trigger{width:100%;min-height:105px;margin-top:15px;border:1px dashed #b8c8c1;border-radius:9px;background:#fff;color:var(--brand);cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px}.upload-trigger .el-icon{font-size:23px}.upload-trigger span{font-size:11px;color:var(--muted)}.preview-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:15px}.preview-item,.add-image{height:118px;border-radius:9px;overflow:hidden;position:relative;background:#fff;border:1px solid var(--line)}.preview-item img{width:100%;height:88px;object-fit:cover;display:block}.preview-item button{position:absolute;right:5px;top:5px;width:25px;height:25px;padding:0;border:0;border-radius:50%;background:rgba(20,32,27,.72);color:#fff;display:grid;place-items:center;cursor:pointer}.preview-item small{display:block;padding:7px 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}.add-image{border-style:dashed;color:var(--brand);cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px}.add-image .el-icon{font-size:22px}.add-image span{font-size:11px}.submit{width:100%;margin-top:24px}.notice{padding:24px}.notice>.el-icon{font-size:25px;color:var(--brand)}.notice h3{margin:15px 0 8px}.notice p,.stop p{font-size:12px;line-height:1.7;color:var(--muted)}.stop{margin-top:14px;padding:18px;border:1px solid #ffd1d1;background:#fff5f5;border-radius:12px;display:flex;gap:12px;color:#c92a2a}.stop p{color:#8f5555;margin:6px 0 0}@media(max-width:850px){.form-grid{grid-template-columns:1fr}.preview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.categories{grid-template-columns:1fr}}
.category-confirmation{margin-top:20px;padding:18px;border:1px solid #e6a23c;border-radius:12px;background:#fff8eb}.category-confirmation>div:first-child{display:flex;gap:10px;color:#9a5b00}.category-confirmation .el-icon{font-size:24px;flex:none}.category-confirmation h3{margin:0 0 6px;font-size:15px}.category-confirmation p{margin:0;color:#7a6545;font-size:12px;line-height:1.6}.suggestions{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.suggestions>span{width:100%;font-size:11px;font-weight:800;color:#8a682e}
</style>
