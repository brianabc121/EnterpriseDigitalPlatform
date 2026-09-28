/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址；留空表示与页面同源（开发环境由 Vite 代理转发）。 */
  readonly VITE_API_BASE?: string
  /** 访客 Widget 地址，默认 http://localhost:5175。 */
  readonly VITE_WIDGET_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
