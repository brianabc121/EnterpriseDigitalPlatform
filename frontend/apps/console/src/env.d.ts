/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址；留空表示与页面同源（开发环境由 Vite 代理转发）。 */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
