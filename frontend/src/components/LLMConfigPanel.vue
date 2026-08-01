<template>
  <teleport to="body">
    <div v-if="visible" class="llm-config-overlay" @click.self="close">
      <div class="llm-config-modal">
        <div class="modal-header">
          <h3>⚙️ LLM 配置（热更新）</h3>
          <button class="close-btn" @click="close">✕</button>
        </div>

        <div class="modal-body">
          <p class="hint">
            修改后点「保存」立即生效，<strong>无需重建容器</strong>。配置会持久化到 .env，重启后仍有效。
          </p>

          <div v-if="loading" class="loading-box">加载中...</div>
          <div v-else>
            <!-- 主 LLM -->
            <div class="config-section">
              <div class="section-title">主 LLM（模拟主体）</div>
              <div class="form-row">
                <label>模型名称</label>
                <input v-model="form.LLM_MODEL_NAME" placeholder="如 MiniMax-M2.7" :disabled="saving" />
              </div>
              <div class="form-row">
                <label>API Base URL</label>
                <input v-model="form.LLM_BASE_URL" placeholder="http://host.docker.internal:3005/v1" :disabled="saving" />
              </div>
              <div class="form-row">
                <label>API Key</label>
                <div class="key-row">
                  <input
                    :value="apiKeyDisplay('LLM_API_KEY')"
                    :placeholder="form._LLM_API_KEY_configured ? '已配置（输入新值可替换）' : '未配置'"
                    @input="onKeyInput($event, 'LLM_API_KEY')"
                    :disabled="saving"
                  />
                  <span v-if="form._LLM_API_KEY_configured && !form.LLM_API_KEY" class="key-badge">已配置</span>
                </div>
              </div>
            </div>

            <!-- 加速 LLM -->
            <div class="config-section">
              <div class="section-title">加速 LLM（BOOST，可选）</div>
              <div class="form-row">
                <label>模型名称</label>
                <input v-model="form.LLM_BOOST_MODEL_NAME" placeholder="留空则不用加速 LLM" :disabled="saving" />
              </div>
              <div class="form-row">
                <label>API Base URL</label>
                <input v-model="form.LLM_BOOST_BASE_URL" placeholder="同主 LLM 则留空" :disabled="saving" />
              </div>
              <div class="form-row">
                <label>API Key</label>
                <div class="key-row">
                  <input
                    :value="apiKeyDisplay('LLM_BOOST_API_KEY')"
                    :placeholder="form._LLM_BOOST_API_KEY_configured ? '已配置' : '未配置'"
                    @input="onKeyInput($event, 'LLM_BOOST_API_KEY')"
                    :disabled="saving"
                  />
                  <span v-if="form._LLM_BOOST_API_KEY_configured && !form.LLM_BOOST_API_KEY" class="key-badge">已配置</span>
                </div>
              </div>
            </div>

            <!-- 并发与限流 -->
            <div class="config-section">
              <div class="section-title">并发与限流</div>
              <div class="form-row">
                <label>最大并发数</label>
                <input v-model="form.LLM_MAX_CONCURRENCY" type="number" min="1" max="32" placeholder="4" :disabled="saving" />
              </div>
              <div class="form-row">
                <label>429 重试次数</label>
                <input v-model="form.LLM_RATE_LIMIT_RETRIES" type="number" min="0" max="20" placeholder="8" :disabled="saving" />
              </div>
            </div>

            <div v-if="message" class="message-box" :class="messageType">
              {{ message }}
            </div>
          </div>
        </div>

        <div class="modal-footer">
          <button class="btn-cancel" @click="close" :disabled="saving">取消</button>
          <button class="btn-save" @click="save" :disabled="saving || loading">
            {{ saving ? '保存中...' : '保存并应用' }}
          </button>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script setup>
import { ref, watch } from 'vue'
import { getLLMConfig, updateLLMConfig } from '../api/config'

const props = defineProps({
  visible: { type: Boolean, default: false }
})
const emit = defineEmits(['close'])

const loading = ref(false)
const saving = ref(false)
const message = ref('')
const messageType = ref('') // 'success' | 'error'
const form = ref({
  LLM_MODEL_NAME: '',
  LLM_BASE_URL: '',
  LLM_API_KEY: '',
  LLM_BOOST_MODEL_NAME: '',
  LLM_BOOST_BASE_URL: '',
  LLM_BOOST_API_KEY: '',
  LLM_MAX_CONCURRENCY: '',
  LLM_RATE_LIMIT_RETRIES: '',
  // 标记原始是否已配置（用于显示"已配置"徽章）
  _LLM_API_KEY_configured: false,
  _LLM_BOOST_API_KEY_configured: false,
})

// API Key 展示：用户没输入新值时显示空（配合 placeholder + 徽章）
function apiKeyDisplay(key) {
  return form.value[key] || ''
}

function onKeyInput(event, key) {
  form.value[key] = event.target.value
}

async function loadConfig() {
  loading.value = true
  message.value = ''
  try {
    const res = await getLLMConfig()
    const d = res.data
    form.value = {
      LLM_MODEL_NAME: d.LLM_MODEL_NAME || '',
      LLM_BASE_URL: d.LLM_BASE_URL || '',
      LLM_API_KEY: '', // 不回显真实 key，让用户按需重输
      LLM_BOOST_MODEL_NAME: d.LLM_BOOST_MODEL_NAME || '',
      LLM_BOOST_BASE_URL: d.LLM_BOOST_BASE_URL || '',
      LLM_BOOST_API_KEY: '',
      LLM_MAX_CONCURRENCY: d.LLM_MAX_CONCURRENCY || '',
      LLM_RATE_LIMIT_RETRIES: d.LLM_RATE_LIMIT_RETRIES || '',
      _LLM_API_KEY_configured: d.LLM_API_KEY?.configured || false,
      _LLM_BOOST_API_KEY_configured: d.LLM_BOOST_API_KEY?.configured || false,
    }
  } catch (e) {
    message.value = '加载配置失败：' + (e.response?.data?.error || e.message)
    messageType.value = 'error'
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  message.value = ''
  // 只提交有值且非空的字段；API Key 仅当用户输入了新值才提交
  const payload = {}
  const fields = [
    'LLM_MODEL_NAME', 'LLM_BASE_URL',
    'LLM_BOOST_MODEL_NAME', 'LLM_BOOST_BASE_URL',
    'LLM_MAX_CONCURRENCY', 'LLM_RATE_LIMIT_RETRIES',
  ]
  for (const f of fields) {
    if (form.value[f] && String(form.value[f]).trim()) {
      payload[f] = String(form.value[f]).trim()
    }
  }
  // API Key 单独处理：只有用户主动输入了才提交
  if (form.value.LLM_API_KEY) payload.LLM_API_KEY = form.value.LLM_API_KEY.trim()
  if (form.value.LLM_BOOST_API_KEY) payload.LLM_BOOST_API_KEY = form.value.LLM_BOOST_API_KEY.trim()

  if (Object.keys(payload).length === 0) {
    message.value = '没有要保存的改动'
    messageType.value = 'error'
    saving.value = false
    return
  }

  try {
    const res = await updateLLMConfig(payload)
    const d = res
    message.value = d.message || '保存成功'
    messageType.value = d.success ? 'success' : 'error'
    if (d.success) {
      // 清空已提交的 key 输入框
      form.value.LLM_API_KEY = ''
      form.value.LLM_BOOST_API_KEY = ''
      // 更新配置标记
      if (payload.LLM_API_KEY) form.value._LLM_API_KEY_configured = true
      if (payload.LLM_BOOST_API_KEY) form.value._LLM_BOOST_API_KEY_configured = true
    }
  } catch (e) {
    message.value = '保存失败：' + (e.response?.data?.error || e.message)
    messageType.value = 'error'
  } finally {
    saving.value = false
  }
}

function close() {
  if (saving.value) return
  emit('close')
}

// 面板打开时自动加载
watch(() => props.visible, (v) => {
  if (v) loadConfig()
})
</script>

<style scoped>
.llm-config-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}
.llm-config-modal {
  background: #1a1d24;
  border: 1px solid #2a2e38;
  border-radius: 12px;
  width: 520px;
  max-width: 90vw;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid #2a2e38;
}
.modal-header h3 {
  margin: 0;
  font-size: 16px;
  color: #e4e7ed;
}
.close-btn {
  background: none;
  border: none;
  color: #8a909c;
  cursor: pointer;
  font-size: 18px;
  padding: 4px 8px;
}
.close-btn:hover { color: #e4e7ed; }
.modal-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1;
}
.hint {
  font-size: 12px;
  color: #8a909c;
  margin: 0 0 16px;
  line-height: 1.5;
}
.hint strong { color: #4fc3f7; }
.loading-box {
  text-align: center;
  padding: 40px;
  color: #8a909c;
}
.config-section {
  margin-bottom: 20px;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  color: #4fc3f7;
  margin-bottom: 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid #2a2e38;
}
.form-row {
  display: flex;
  align-items: center;
  margin-bottom: 10px;
  gap: 12px;
}
.form-row label {
  width: 110px;
  font-size: 12px;
  color: #a0a6b2;
  flex-shrink: 0;
}
.form-row input {
  flex: 1;
  background: #14171e;
  border: 1px solid #2a2e38;
  border-radius: 6px;
  padding: 7px 10px;
  color: #e4e7ed;
  font-size: 13px;
  font-family: 'Consolas', monospace;
}
.form-row input:focus {
  outline: none;
  border-color: #4fc3f7;
}
.form-row input:disabled {
  opacity: 0.5;
}
.key-row {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
}
.key-badge {
  font-size: 10px;
  color: #67c23a;
  background: rgba(103,194,58,0.15);
  padding: 2px 6px;
  border-radius: 3px;
  white-space: nowrap;
}
.message-box {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
}
.message-box.success {
  background: rgba(103,194,58,0.12);
  color: #67c23a;
  border: 1px solid rgba(103,194,58,0.3);
}
.message-box.error {
  background: rgba(245,108,108,0.12);
  color: #f56c6c;
  border: 1px solid rgba(245,108,108,0.3);
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 20px;
  border-top: 1px solid #2a2e38;
}
.btn-cancel, .btn-save {
  padding: 8px 18px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  border: none;
}
.btn-cancel {
  background: #2a2e38;
  color: #a0a6b2;
}
.btn-cancel:hover:not(:disabled) {
  background: #353a46;
  color: #e4e7ed;
}
.btn-save {
  background: #4fc3f7;
  color: #14171e;
  font-weight: 600;
}
.btn-save:hover:not(:disabled) {
  background: #66d4ff;
}
.btn-cancel:disabled, .btn-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
