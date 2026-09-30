/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址；留空表示与页面同源（开发环境由 Vite 代理转发）。 */
  readonly VITE_API_BASE?: string
  /** 传输加密的服务器公钥（后端 app.cli transport-public-key 输出）；部署时也可以用运行时配置。 */
  readonly VITE_TRANSPORT_PUBLIC_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
