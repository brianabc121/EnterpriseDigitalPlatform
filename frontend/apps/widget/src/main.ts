import './styles.css'

import { createApp, type Component } from 'vue'

import App from './App.vue'
import MaterialShare from './MaterialShare.vue'
import { shareToken } from './materials'
import OrderTracking from './OrderTracking.vue'
import { trackToken } from './orders'

// 订单跟踪链接（?track=…）只显示订单进度，企业资料的分享链接（?share=…）只显示分享的资料，都不连接
// 客服（设计文档 §25.6、§36.4）。
function page(): [Component, Record<string, unknown>?] {
  const track = trackToken(location.search)
  if (track !== null) return [OrderTracking, { token: track }]
  const share = shareToken(location.search)
  if (share !== null) return [MaterialShare, { token: share }]
  return [App]
}

const [root, props] = page()
createApp(root, props).mount('#app')
