# EnterpriseDigitalPlatform

企业数字化转型平台（多租户 SaaS）。一期建设全渠道智能客服：

- **接入**：Web 访客、企业微信（服务商接入：客户联系、微信客服、聊天侧边栏），后续加入飞书、钉钉、WhatsApp、Telegram。
- **接待**：AI 先接待，必要时自动转人工。
- **管理**：坐席与管理员分级管理客户数据，支持转接。
- **沉淀**：从聊天记录中持续提炼企业知识库。

技术栈：FastAPI、Vue 3、OpenIM、PostgreSQL（pgvector）、国内大模型（OpenAI 兼容协议）。

## 文档

- [设计文档（草案）](docs/plans/2026-09-28-enterprise-digital-platform-design.md)：架构、关键决策、分期路线图和待确认问题。
- [P0 / P1 实施计划](docs/plans/2026-09-28-p0-p1-implementation-plan.md)：任务拆分、P0 验收结果和转入 P1 的事项。

## 快速开始

需要 Docker（含 Compose）、[uv](https://docs.astral.sh/uv/)、Node.js 22 和 pnpm 10（执行 `corepack enable` 即可获得）。

```bash
# 1. 启动 PostgreSQL（含 pgvector）和 Redis，安装后端依赖并执行迁移
make dev-up
make backend-install
make migrate

# 2. 创建平台运营账号（不带 --password 时交互输入密码）
(cd backend && uv run python -m app.cli create-platform-admin --username ops)

# 3. 安装前端依赖，然后在三个终端分别启动后端、控制台和运营后台
make frontend-install
make backend-dev     # 后端接口：http://localhost:8000/docs
make console-dev     # 控制台：http://localhost:5173
make platform-dev    # 运营后台：http://localhost:5174
```

在运营后台开通租户（企业代码 + 首个管理员），然后用"企业代码 / 用户名 / 密码"登录控制台。
默认配置适用于本地环境；需要修改时，把 `backend/.env.example` 复制为 `backend/.env`。

### 检查与测试

```bash
make backend-lint    # ruff + mypy
make test            # 后端测试需要 make dev-up 启动的 PostgreSQL
make frontend-build
```

后端接口变更后执行 `make openapi`，重新导出 `openapi.json` 并生成前端类型（CI 会检查两者是否一致）。

### 浏览器验收（P0）

`scripts/e2e/p0-acceptance.cjs` 在真实浏览器里走一遍 P0 验收标准：开通两个租户；管理员创建坐席和客户；坐席只看到自己的菜单和客户；另一个租户看不到这些数据。
后端、控制台、运营后台都启动后运行：

```bash
npm i -g playwright && playwright install chromium
NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p0-acceptance.cjs
```

每次运行都会开通新的租户，可以重复执行。截图和 `summary.json` 写入 `e2e-shots/`；任一检查失败时以非 0 退出。
