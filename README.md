# EnterpriseDigitalPlatform

企业数字化转型平台（多租户 SaaS）。一期建设全渠道智能客服：

- **接入**：Web 访客、企业微信（服务商接入：客户联系、微信客服、聊天侧边栏），后续加入飞书、钉钉、WhatsApp、Telegram。
- **接待**：AI 先接待，必要时自动转人工。
- **管理**：坐席与管理员分级管理客户数据，支持转接。
- **沉淀**：从聊天记录中持续提炼企业知识库。

技术栈：FastAPI、Vue 3、OpenIM、PostgreSQL（pgvector）、国内大模型（OpenAI 兼容协议）。

## 文档

- [设计文档（草案）](docs/plans/2026-09-28-enterprise-digital-platform-design.md)：架构、关键决策、分期路线图和待确认问题。
- [P0 / P1 实施计划](docs/plans/2026-09-28-p0-p1-implementation-plan.md)：任务拆分、验收结果、OpenIM 实测结论和后续事项。

## 快速开始

需要 Docker（含 Compose）、[uv](https://docs.astral.sh/uv/)、Node.js 22 和 pnpm 10（执行 `corepack enable` 即可获得）。

```bash
# 1. 启动依赖，安装后端依赖并执行迁移
make dev-up          # PostgreSQL（含 pgvector）和 Redis
make im-up           # OpenIM 及其依赖（MongoDB、Kafka、etcd、MinIO）；首次需要拉取约 3 GB 镜像
make backend-install
make migrate

# 2. 创建平台运营账号（不带 --password 时交互输入密码）
(cd backend && uv run python -m app.cli create-platform-admin --username ops)

# 3. 安装前端依赖，然后分别在不同终端启动
make frontend-install
make backend-dev     # 后端接口：http://localhost:8000/docs（监听 0.0.0.0，供 OpenIM 回调）
make scheduler-dev   # 调度进程：每分钟按 seq 对账，补录回调丢失的消息
make console-dev     # 控制台：http://localhost:5173
make platform-dev    # 运营后台：http://localhost:5174
make widget-dev      # 访客 Widget：http://localhost:5175/?key=<渠道 key>
```

在运营后台开通租户（企业代码 + 首个管理员），然后用"企业代码 / 用户名 / 密码"登录控制台。
控制台"设置"页列出本租户的接入渠道，点"打开访客测试页"即可以访客身份与服务群对话；
访客消息会进入平台消息库（`GET /api/v1/rooms`、`GET /api/v1/rooms/{id}/messages`）。

默认配置适用于本地环境；需要修改时，把 `backend/.env.example` 复制为 `backend/.env`。
OpenIM 的镜像名都可以用环境变量替换（见 `deploy/compose/openim/docker-compose.yml`），便于使用镜像加速地址。

### 检查与测试

```bash
make backend-lint    # ruff + mypy
make test            # 后端测试需要 make dev-up 启动的 PostgreSQL 和 Redis；OpenIM 用内存版
make frontend-build
```

- 后端接口变更后执行 `make openapi`，重新导出 `openapi.json` 并生成前端类型（CI 会检查两者是否一致）。
- `backend/tests/test_openim_contract.py` 同时验证内存版 OpenIM 和真实 OpenIM 的行为是否一致：
  `make im-up` 之后执行 `cd backend && EDP_TEST_OPENIM_URL=http://localhost:10002 uv run pytest tests/test_openim_contract.py`。

### 浏览器验收

先安装 Playwright：`npm i -g playwright && playwright install chromium`。两个脚本每次运行都会开通新的租户，可以重复执行；
截图和 `summary.json` 写入 `e2e-shots/`，任一检查失败时以非 0 退出。

- **P0**（`scripts/e2e/p0-acceptance.cjs`）：开通两个租户；管理员创建坐席和客户；坐席只看到自己的菜单和客户；
  另一个租户看不到这些数据。需要后端、控制台和运营后台。

  ```bash
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p0-acceptance.cjs
  ```

- **P1 M1**（`scripts/e2e/m1-im-acceptance.cjs`）：访客在 Widget 里发消息、实时收到机器人回复，消息经回调入库；
  刷新后仍是同一个访客。需要 OpenIM、后端和 Widget。提供停止/启动后端和对账的命令时，还会验证
  "后端停机期间回调丢失的消息由对账补录"。

  ```bash
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> \
    BACKEND_STOP_CMD="<停止后端的命令>" BACKEND_START_CMD="<启动后端的命令>" \
    RECONCILE_CMD="cd backend && uv run python -m app.cli im-reconcile" \
    node scripts/e2e/m1-im-acceptance.cjs
  ```
