/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址；留空表示与页面同源（开发环境由 Vite 代理转发）。 */
  readonly VITE_API_BASE?: string
  /** 访客 Widget 地址，默认 http://localhost:5175。 */
  readonly VITE_WIDGET_URL?: string
  /** 企业微信 JS-SDK 脚本地址（逗号分隔）；联调、验收时指向模拟服务。默认用腾讯的官方地址。 */
  readonly VITE_WECOM_JSSDK_URLS?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
