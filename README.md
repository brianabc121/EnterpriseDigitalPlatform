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
make dev-up          # PostgreSQL（含 pgvector）、Redis 和对象存储 MinIO（自动创建 edp-files 桶）
make im-up           # OpenIM 及其依赖（MongoDB、Kafka、etcd、MinIO）；首次需要拉取约 3 GB 镜像
make backend-install
make migrate

# 2. 创建平台运营账号（不带 --password 时交互输入密码）
(cd backend && uv run python -m app.cli create-platform-admin --username ops)

# 3. 安装前端依赖，然后分别在不同终端启动
make frontend-install
make backend-dev     # 后端接口：http://localhost:8000/docs（监听 0.0.0.0，供 OpenIM 回调）
make worker-dev      # 实时消费进程：消息归入会话、排队与分配
make scheduler-dev   # 调度进程：按 seq 对账、会话超时与断线处理、IM 操作重试
make console-dev     # 控制台：http://localhost:5173
make platform-dev    # 运营后台：http://localhost:5174
make widget-dev      # 访客 Widget：http://localhost:5175/?key=<渠道 key>
```

在运营后台开通租户（企业代码 + 首个管理员），然后用"企业代码 / 用户名 / 密码"登录控制台。
控制台"设置"页列出本租户的接入渠道，点"打开访客测试页"即可以访客身份与服务群对话；
访客消息会进入平台消息库（`GET /api/v1/rooms`、`GET /api/v1/rooms/{id}/messages`）。

在网站中嵌入访客 Widget：在"设置 → 接入渠道 → 设置"中复制嵌入代码，放到页面的 `</body>` 之前：

```html
<script src="http://localhost:5175/embed.js" data-key="<渠道 key>" async></script>
```

同一处可以设置窗口标题、欢迎语、隐私提示和允许嵌入的网站，并启用实名访客：网站后端用渠道的签名密钥
为登录用户计算 `HMAC-SHA256(密钥, "<external_id>:<name>:<timestamp>")`，在加载 `embed.js` 之前设置
`window.EDPWidgetConfig = { user: { external_id, name, timestamp, signature } }`（示例代码见设置页）。
聊天中的图片和文件保存在对象存储里；其他环境首次部署时执行
`cd backend && uv run python -m app.cli storage-init` 创建存储桶。

访客的第一条消息会开启一个会话，按路由策略排队并分配给在线坐席：坐席登录控制台后进入"工作台"即自动上线，
在工作台里接待、使用快捷话术、编辑客户资料、转接或结束会话。管理员在"设置"里配置技能组、路由策略
（工作时间、排队超时、空闲结束、会话续接）和坐席并发，在"会话记录""留言""报表"里查看服务情况，
在"设置 → 用量"里查看每日用量；运营后台的租户列表显示各租户用量。用量由调度进程每 10 分钟汇总，
也可以执行 `cd backend && uv run python -m app.cli usage-rollup` 立即汇总（`--day`、`--to` 补算历史日期）。

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
- `backend/tests/test_storage_contract.py` 用真实的 S3 兼容服务验证对象存储签名与接口：
  `make dev-up` 之后执行 `cd backend && EDP_TEST_STORAGE_URL=http://localhost:9000 uv run pytest tests/test_storage_contract.py`。
- `backend/tests/test_authz_matrix.py` 是越权矩阵：新增带 ID 的接口需要加入其中的 `MATRIX`，否则测试失败。

### 浏览器验收

先安装 Playwright：`npm i -g playwright && playwright install chromium`。脚本每次运行都会开通新的租户，可以重复执行；
截图和 `summary.json` 写入 `e2e-shots/`，任一检查失败时以非 0 退出。

- **P0**（`scripts/e2e/p0-acceptance.cjs`）：开通两个租户；管理员创建坐席和客户；坐席只看到自己的菜单和客户；
  另一个租户看不到这些数据。需要后端、控制台和运营后台。

  ```bash
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p0-acceptance.cjs
  ```

- **P1 M4**（`scripts/e2e/m4-workbench-acceptance.cjs`）：访客在 Widget 里咨询，会话实时分配给工作台里的坐席，
  双方实时对话、快捷话术、客户面板、结束会话，并检查消息入库不重复。需要后端、实时消费进程、调度进程、控制台、
  Widget 和 OpenIM。
- **P1 M5**（`scripts/e2e/m5-transfer-acceptance.cjs`）：坐席 A 把会话转接给坐席 B，B 接受后看到完整历史，
  A 不再看到这个客户、已被移出服务群。前置同上。
- **P1 M6**（`scripts/e2e/m6-admin-acceptance.cjs`）：管理员在界面上配置技能组、路由策略（含工作时间）、
  渠道策略和坐席并发；访客按技能组和并发上限分配；处理留言；检查会话记录、用量、报表、首页实时数据和
  运营后台的租户用量。前置同上，另外还需要运营后台（汇总用量时会执行 `app.cli usage-rollup`）。
- **P1 M3**（`scripts/e2e/m3-widget-acceptance.cjs`）：管理员在控制台完成 Widget 设置；脚本起一个"客户网站"
  （端口 5176）用 `embed.js` 嵌入 Widget 并为会员签名。检查实名访客与换设备续接、欢迎语与隐私提示、
  双方收发图片和文件、收起时的未读角标、满意度评价、留言、未授权网站被拒绝。前置同上，另需 MinIO。

  ```bash
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m4-workbench-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m5-transfer-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m3-widget-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m6-admin-acceptance.cjs
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
