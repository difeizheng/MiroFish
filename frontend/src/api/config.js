import service from './index'

// 注意：必须走同源相对路径（经 vite proxy 转发），不能继承 index.js 里
// 写死的 baseURL 'http://localhost:5001'。Docker 部署下浏览器直连
// localhost:5001 会出现连接池挂起（首次加载图谱大响应后尤为明显），
// 导致 modal「加载中...」卡死。同源请求由 vite proxy 代理，已验证稳定。
const sameOrigin = { baseURL: window.location.origin }

/**
 * 获取当前 LLM 配置
 * @returns {Promise} { success, data: { LLM_MODEL_NAME, LLM_API_KEY: {configured, preview}, ... } }
 */
export const getLLMConfig = () => {
  return service.get('/api/config/llm', sameOrigin)
}

/**
 * 热更新 LLM 配置（只传需要改的字段）
 * @param {Object} data - { LLM_MODEL_NAME?, LLM_MAX_CONCURRENCY?, ... }
 * @returns {Promise} { success, data, updated, persisted, message }
 */
export const updateLLMConfig = (data) => {
  return service.post('/api/config/llm', data, sameOrigin)
}
