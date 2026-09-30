import './styles.css'

import { createApp } from 'vue'

import App from './App.vue'
import OrderTracking from './OrderTracking.vue'
import { trackToken } from './orders'

// 订单跟踪链接（?track=…）只显示订单进度，不连接客服（设计文档 §25.6）。
const track = trackToken(location.search)
const app = track === null ? createApp(App) : createApp(OrderTracking, { token: track })
app.mount('#app')
