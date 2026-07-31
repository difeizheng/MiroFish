/**
 * 实体类型名称展示层翻译 helper
 *
 * 设计说明：
 * - 图谱实体类型 (entity_type) 由后端 LLM 基于上传文档动态生成（如 Person、
 *   Organization、Event 等），其英文取值是 Zep 图谱的数据层标识符，禁止修改。
 * - 本 helper 只做【展示层】中文化：从 i18n 的 entityTypes 字典查中文译文，
 *   未命中时回退显示原始英文取值，确保动态生成的未知类型不会丢失展示。
 * - 新增类型只需在 locales/zh.json 与 locales/en.json 的 entityTypes 字典里
 *   补一条映射，无需改动任何组件。
 *
 * 实现说明：
 * - vue-i18n 的 t() 设计为返回字符串，对值为对象的 key 不可靠；故用
 *   getLocaleMessage 直接读取原始消息对象再从中取 entityTypes 字典。
 */

import i18n from '../i18n'

/**
 * 从当前 locale（回退到 fallbackLocale）的消息对象中取 entityTypes 字典。
 * @returns {Record<string, string>|null}
 */
function getEntityTypeDict() {
  const { locale, fallbackLocale } = i18n.global
  // locale / fallbackLocale 在 composition 模式下可能是 ref/computed，取其 value
  const cur = typeof locale === 'object' && locale !== null ? locale.value : locale
  const fb = typeof fallbackLocale === 'object' && fallbackLocale !== null
    ? fallbackLocale.value
    : fallbackLocale

  const tryPick = (l) => {
    if (!l) return null
    const msg = i18n.global.getLocaleMessage(l)
    const dict = msg && msg.entityTypes
    return dict && typeof dict === 'object' ? dict : null
  }

  return tryPick(cur) || tryPick(fb) || null
}

/**
 * 将实体类型英文取值翻译为当前 locale 下的展示文案。
 * 查找策略（大小写不敏感）：
 *   1. 原值精确匹配
 *   2. 原值小写匹配
 *   3. 未命中 -> 返回原值（不破坏展示）
 * @param {string} type 实体类型英文取值，例如 "Person"
 * @returns {string} 当前 locale 下的展示文案
 */
export function translateEntityType(type) {
  if (!type) return type
  const dict = getEntityTypeDict()
  if (dict) {
    if (Object.prototype.hasOwnProperty.call(dict, type)) {
      return dict[type]
    }
    const lower = String(type).toLowerCase()
    // 大小写不敏感：遍历字典键做一次小写比对
    for (const key of Object.keys(dict)) {
      if (key.toLowerCase() === lower) {
        return dict[key]
      }
    }
  }
  // 回退：未知动态类型保留原值，确保始终有展示
  return type
}

/**
 * 批量翻译（用于图例等列表场景）。
 * @param {Array<{name: string, count?: number, color?: string}>} types
 * @returns {Array} 与入参同结构，name 字段替换为译文
 */
export function translateEntityTypes(types) {
  if (!Array.isArray(types)) return types
  return types.map(t => (t && t.name ? { ...t, name: translateEntityType(t.name) } : t))
}

export default translateEntityType
