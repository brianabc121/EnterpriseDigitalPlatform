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

AI 接待需要在 `backend/.env` 中配置大模型（OpenAI 兼容协议：DeepSeek、通义千问、智谱、豆包、Kimi、自建 vLLM
等，见 `backend/.env.example`）。租户管理员在"知识库"里录入或批量导入问答和文档并发布，在"AI 接待"里启用 AI、
设置名称与转人工规则，用"试一试"和"评测"检验效果；再把路由策略的接待方式改为"AI 优先"，访客就先由 AI 依据
知识库回答，客户要求人工、敏感诉求、AI 把握不足或模型故障时自动转人工，并给坐席写好交接摘要。坐席在工作台用
"AI 建议"和知识库检索回复客户。没有模型 Key 时可以用模拟服务联调：

```bash
cd backend && uv run python -m tests.fake_llm --port 8900
# 后端、实时消费进程和调度进程启动前设置：
export EDP_LLM_BASE_URL=http://127.0.0.1:8900/v1 EDP_LLM_CHAT_MODEL=fake-chat EDP_LLM_EMBED_MODEL=fake-embed
```

更换向量模型后执行 `cd backend && uv run python -m app.cli kb-reindex` 重建知识检索单元。

知识沉淀：调度进程每小时从已结束的会话里提炼问答和没有解答的问题（先脱敏），管理员在"知识库 → 审核台"
编辑后通过、合并或驳回，通过的知识 AI 与坐席立即可用；知识有版本历史、可以回滚，可以设为必读（坐席在工作台
"动态"里确认），"运营数据"和"周报"跟踪命中率、缺口、通过率和采纳率。需要立即处理时执行
`cd backend && uv run python -m app.cli kb-extract`（提炼）或 `kb-digest`（生成本周周报）。

企业微信（服务商代开发应用，只用官方接口）：平台运营在 `backend/.env` 中配置代开发应用模板
（`EDP_WECOM_*`，见 `backend/.env.example`），租户管理员在控制台"企业微信"页扫码授权。授权后平台自动同步
成员、客户（客户联系）、企业标签、客户群和微信客服账号：

- **微信客服**：每个客服账号是一个接入渠道（在"设置 → 接入渠道"里绑定路由策略、设置欢迎语）。微信用户在客服
  入口的咨询进入平台，与网页访客一样由 AI 或坐席接待；坐席的回复经企业微信送达。工作台显示"剩余 N 条 / 截止
  hh:mm"（客户最后一次发消息后 48 小时内最多 5 条），AI 回复带"【AI】"标识和「转人工」按钮（菜单消息），
  人工接待的会话结束时客户收到满意度评价按钮。客户发来的语音转成 MP3 供网页播放（需要 ffmpeg），配置了
  语音转文字（`EDP_ASR_*`）时转写成文字，AI 据此理解。
- **客户联系与客户群**：外部联系人写入客户档案（添加人绑定的员工成为归属坐席），企业标签双向同步，
  客户群及成员关联到客户；员工添加新客户时可以自动发送附带客服链接的欢迎语。转移客户、离职交接时可以勾选
  "同时变更企业微信里的添加人"（原成员在职时在职继承，已离职时离职继承）和"同时转移客户群"，结果由调度进程
  回收。"企业微信 → 离职继承"列出离职成员的待分配客户，分配给接手的员工；"客户群活码"生成"加入群聊"
  二维码（群满自动建新群）并统计进群人数。
- **群发**："群发"页按标签、归属坐席给客户，或按客户群、群主创建群发任务；企业微信不允许直接给客户发消息，
  任务由员工或群主在企业微信里确认后发出，发送结果由调度进程回收（也可以在详情里立即刷新、提醒、停止）。
- **员工**：在"成员绑定"里把企业成员绑定到平台员工后，员工可以在登录页用企业微信扫码登录，在企业微信内
  打开控制台免登，并通过应用消息收到新会话分配、转接请求、必读知识和知识周报提醒。在企业微信手机端打开
  工作台时进入手机版（`/m`），可以直接回复客户。
- **聊天工具栏侧边栏**：把 `{控制台地址}/wecom/sidebar?corp=<CorpID>` 配置到企业微信聊天工具栏，员工在客户
  单聊、客户群里查看客户档案、修改标签，粘贴客户的问题获取 AI 建议，或用快捷话术、知识检索，一键发送；
  单聊里可以一键拉上接单员建群。不在企业微信里打开时可以用 `?external_userid=` 调试（只记录，不发送）；
  联调时可以把控制台的 `VITE_WECOM_JSSDK_URLS` 指向模拟企业微信的 JS-SDK（`http://127.0.0.1:8901/jssdk/jwxwork.js`）。
- **数据与智能专区（可选）**：企业购买会话存档并授权专区后，在"企业微信 → 设置"里填写专区程序 ID，
  调度进程每小时取回群聊摘要、情绪和问答候选（问答进入知识审核台），群聊原文不出专区。

需要立即同步或回收在职继承结果时执行 `cd backend && uv run python -m app.cli wecom-sync`（或 `wecom-transfers`）。
没有服务商资质时可以用模拟企业微信联调（`uv run python -m tests.fake_wecom --port 8901 --platform http://127.0.0.1:8000`，
环境变量见 `backend/.env.example`）。

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

- **P3**（`scripts/e2e/p3-ai-acceptance.cjs`）：管理员维护知识库（新建、CSV 导入、检索测试）、启用 AI 接待并
  试一试和评测；访客得到 AI 依据知识的回答，要求人工后 AI 写好摘要转给坐席；坐席用 AI 建议和知识检索回复；
  检查会话记录里的 AI 判定、报表的 AI 指标和运营后台的 AI 额度。前置同 M6，后端、实时消费进程和调度进程
  需要接到大模型（可以用上面的模拟服务）。
- **P4**（`scripts/e2e/p4-knowledge-acceptance.cjs`）：访客与坐席对话后提炼知识，管理员在审核台通过、补充、
  驳回候选并对比冲突答案，AI 立即使用新知识；版本回滚、必读确认、坐席评价、运营数据与周报。前置同 P3
  （提炼命令的环境变量同样要接到大模型）。
- **P2**（`scripts/e2e/p2-wecom-acceptance.cjs`）：管理员扫码授权企业微信（模拟授权页）并绑定成员；微信客户进入
  客服会话收到欢迎语，咨询分配给坐席，工作台显示回复额度，坐席和 AI 的回复经企业微信送达；员工添加新客户后
  自动发送欢迎语，标签写回企业微信；在职继承；企业微信扫码登录；侧边栏 AI 建议。前置同 P3，另外需要模拟企业微信
  （`uv run python -m tests.fake_wecom --port 8901 --platform http://127.0.0.1:8000`），后端、实时消费进程、调度进程
  以及运行脚本的终端都要设置 `EDP_WECOM_*`（见 `backend/.env.example`，脚本会执行 `app.cli wecom-transfers`）。

  ```bash
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m4-workbench-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m5-transfer-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m3-widget-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/m6-admin-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p3-ai-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p4-knowledge-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/p2-wecom-acceptance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g1-wecom-extras.cjs
  ```

- **企业微信补充**（`scripts/e2e/g1-wecom-extras.cjs`）：群发任务与结果回收、客户群活码、侧边栏（模拟 JS-SDK）
  改标签和一键建群、手机版工作台（语音转写、回复、满意度按钮）、离职继承与客户群继承。前置同 P2，另外控制台以
  `VITE_WECOM_JSSDK_URLS=http://127.0.0.1:8901/jssdk/jwxwork.js` 启动，后端配置语音转文字
  （`EDP_ASR_BASE_URL=http://127.0.0.1:8900/v1 EDP_ASR_MODEL=fake-asr`，模拟大模型提供），并安装 ffmpeg。

- **P1 M1**（`scripts/e2e/m1-im-acceptance.cjs`）：访客在 Widget 里发消息、实时收到机器人回复，消息经回调入库；
  刷新后仍是同一个访客。需要 OpenIM、后端和 Widget。提供停止/启动后端和对账的命令时，还会验证
  "后端停机期间回调丢失的消息由对账补录"。

  ```bash
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> \
    BACKEND_STOP_CMD="<停止后端的命令>" BACKEND_START_CMD="<启动后端的命令>" \
    RECONCILE_CMD="cd backend && uv run python -m app.cli im-reconcile" \
    node scripts/e2e/m1-im-acceptance.cjs
  ```
