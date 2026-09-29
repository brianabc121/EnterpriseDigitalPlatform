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

套餐、计费与租户生命周期：

- **套餐与额度**：运营后台"套餐"维护试用版、标准版、旗舰版等套餐（月费、坐席数、每月 AI 回复条数、知识条目数、
  渠道数、功能开关、超额策略）。开通租户时选择套餐（有试用天数的先试用），也可以开放企业在登录页"免费试用"
  自助注册（每个 IP 每小时 5 次）。超出坐席、知识条目、渠道额度时操作被拒绝；AI 回复额度用完后按套餐的策略
  转人工或继续回复并按条计费；套餐不含的功能（AI、企业微信、群发、知识提炼、专区）自动关闭，控制台隐藏相应入口。
  运营可以在租户详情里单独调整某个租户的额度和功能。启用计费之前开通的租户不按套餐限制。
- **订阅与账单**：租户详情里开始新订阅（试用转正式、升级、降级）、续费或取消。调度进程每小时把到期的订阅标记为
  已到期，宽限期（默认 7 天）后停用租户，续费后自动恢复；每天生成上个月的账单（按天折算月费、超额 AI 回复），
  运营在"账单"里标记已付款。控制台"设置 → 套餐与账单"显示额度用量、账单和可选套餐，试用或即将到期时顶部提醒。
- **数据导出与注销**：租户管理员在"设置 → 数据与注销"里导出全部业务数据（ZIP，每张表一个 JSON Lines 文件和
  聊天文件，由调度进程生成，保留 7 天）；申请注销时核对密码和企业代码，自动导出一次，保留期（默认 30 天）内
  可以撤销，之后删除全部业务数据（IM 群、对象存储文件、各租户表）并生成带 SHA-256 摘要的删除记录。
- **平台访问授权**：平台运营默认看不到租户的业务数据；租户在"设置 → 平台访问授权"里授权一段时间后，
  运营可以在租户详情里只读查看会话和消息，每次查看都记入审计日志，租户可以看到访问记录。

运营后台另外提供：渠道授权状态、模型供应商（OpenAI 兼容接口，设为默认后取代环境变量里的配置，也可以按场景
路由或给大客户单独指定；租户也可以在"AI 接待 → 大模型接口"里使用自带的接口密钥）、全局敏感词（AI 转人工、
拦截 AI 回复和坐席消息）、系统健康（数据库、Redis、OpenIM、对象存储、大模型、企业微信、发件箱、实时消费与调度
进程）、审计日志、删除记录、平台设置（自助注册、宽限期、保留期）。运营账号可以在"账号安全"里启用二次验证
（TOTP 验证器应用）；生产环境强制要求（`EDP_PLATFORM_MFA_REQUIRED` 可以覆盖）。需要立即处理时执行
`cd backend && uv run python -m app.cli billing-lifecycle`、`billing-invoices --month 2026-09` 或 `tenant-jobs`
（生成导出、删除到期的租户数据）；`provision-tenant` 可以用 `--plan` 指定套餐。

权限、合规与审计：

- **员工与角色**：租户管理员在"员工"里修改员工姓名和角色、停用或启用、重置密码；停用后立即退出登录并下线，
  接待中的会话退回队列（名下客户用"交接客户"转给别人）。不能停用自己，至少保留一名启用的租户管理员，
  启用时检查坐席额度。"员工 → 角色"里新建自定义角色（按分组勾选权限点，不能超出自己拥有的权限）。
  每位员工可以在右上角菜单里修改自己的密码（其他设备上的登录随即失效）。
- **客户敏感信息**：客户的手机号、邮箱用租户数据密钥加密保存，列表和客户面板只显示掩码；有"查看客户手机号和邮箱"
  权限的员工可以查看完整内容，每次查看记入操作日志。客户列表可以按名称、公司搜索，手机号、邮箱需要完整输入
  （盲索引精确查找）。企业微信客户会带上员工备注的手机号和企业名称。
- **导出、合并与个人信息请求**：有导出权限的员工再次输入密码后导出数据范围内的客户名单（CSV；没有查看敏感信息的
  权限时导出掩码）。"更多 → 合并重复客户"把重复档案的渠道身份、会话、留言和归属记录并入一个客户。
  "个人信息请求"可以生成客户的个人信息副本（JSON），或删除客户及其会话、消息、留言和聊天文件并解散服务群；
  处理记录只保留掩码后的名称。
- **操作日志**：有"查看操作日志"权限的员工在"操作日志"里按类别和时间查看登录、员工与角色变更、客户导出、
  查看敏感信息、个人信息请求、平台运维访问等记录（只能查看）。
- **保留期与病毒扫描**："设置 → 数据保留"设置聊天消息和文件的保留天数，调度进程每小时删除到期的内容（文件到期后
  消息里显示"文件已过期"）。配置 `EDP_CLAMAV_HOST` 后，调度进程每分钟用 ClamAV 扫描新的聊天附件，含有病毒的
  文件被删除，消息显示"已被拦截"，下载链接返回 410。开发时可以用 `uv run python -m tests.fake_clamd --port 3310`
  （把 EICAR 测试串判为病毒）。OpenIM 里的消息副本按 OpenIM 自己的保留期清理（`EDP_IM_RETAIN_DAYS`，默认 365 天），应不长于各租户的保留期。
- **租户数据密钥**：每个租户一把数据密钥（用 `EDP_DATA_ENCRYPTION_KEY` 包装后保存），加密渠道凭证、自带的模型
  密钥和客户联系方式；运营后台租户详情的"数据密钥"里可以轮换（现有密文随即换成新版本加密）。更换主密钥时，
  把新密钥设为 `EDP_DATA_ENCRYPTION_KEY`、旧密钥放到另一个环境变量，执行
  `cd backend && uv run python -m app.cli rewrap-keys --old-key-env EDP_OLD_DATA_ENCRYPTION_KEY`；不带参数执行时
  只把早期直接用主密钥加密的租户密文换成租户密钥加密。租户注销删除数据时密钥一并删除（加密擦除）。
  需要立即清理或扫描时执行 `uv run python -m app.cli security-jobs`。

会话与路由：

- **排队优先级**：路由策略里设置 VIP 标签（默认"VIP"），带这些标签的客户排在最前；开启"投诉优先"时，
  说到投诉、退款等敏感诉求或情绪激动的客户排在普通客户前面。退回队列的会话在同一档内往前排。访客窗口显示
  前面还有几位，工作台"排队"里用 VIP、优先标记区分。
- **按意图分配**：路由策略里配置意图（如售前、售后、技术）和对应技能组，可以附带关键词。AI 接待时由大模型
  判断意图，客户的话里出现关键词时也会命中；转人工时分配到对应技能组，工作台显示识别出的意图。
- **技能组溢出**：技能组可以设置备用技能组和等待时间，排队超过这么久仍没有分配时改由备用技能组接待（只溢出一次）。
- **排队期间 AI 继续回答**：路由策略开启后，客户转人工排队时 AI 继续回答其他问题（不再重复转人工）。
  访客窗口排队时可以"取消排队"：AI 可以接待时回到 AI，否则结束会话。
- **交还 AI、主管转人工**：坐席可以把人工接待中的会话交还 AI（坐席退出服务群，名额立即用于分配）；
  主管在工作台"进行中"里查看 AI 接待和其他坐席接待中的会话，可以把 AI 会话直接转人工。
- **旁听与协助**：主管可以旁听组内的会话（加入服务群，只看不说，客户看不到）；接待坐席可以邀请在线同事协助，
  协助者能看到客户资料并直接回复，客户会看到"客服 X 加入了会话"。旁听、协助的会话带标记出现在"接待中"，
  会话结束或交还 AI 时一并退出。"已结束"页签列出自己最近结束的会话。
- **客户转移申请**：没有分配权限的坐席在客户列表"更多 → 申请转移"里申请把客户转给自己或在线同事，
  管理员在"转移申请"里批准或驳回（可以同时变更企业微信添加人），批准后归属记录显示"申请审批"。

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
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g2-commerce-ops.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g3-compliance.cjs
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> node scripts/e2e/g4-routing-collab.cjs
  ```

- **企业微信补充**（`scripts/e2e/g1-wecom-extras.cjs`）：群发任务与结果回收、客户群活码、侧边栏（模拟 JS-SDK）
  改标签和一键建群、手机版工作台（语音转写、回复、满意度按钮）、离职继承与客户群继承。前置同 P2，另外控制台以
  `VITE_WECOM_JSSDK_URLS=http://127.0.0.1:8901/jssdk/jwxwork.js` 启动，后端配置语音转文字
  （`EDP_ASR_BASE_URL=http://127.0.0.1:8900/v1 EDP_ASR_MODEL=fake-asr`，模拟大模型提供），并安装 ffmpeg。

- **套餐与运营后台**（`scripts/e2e/g2-commerce-ops.cjs`）：企业自助注册并试用；运营账号启用二次验证后登录要验证码；
  新建套餐、转正式订阅、单独调整坐席额度后新增员工被拒绝；生成账单并标记已付款；添加模型供应商并检查连通；
  全局敏感词让 AI 转人工；系统健康；授权平台运维访问与访问记录；导出数据并下载；申请注销后立即删除数据并生成
  删除记录；审计日志；关闭自助注册。前置同 P3（需要运营后台、实时消费进程和调度进程）；脚本结束时关闭运营账号的
  二次验证并删除测试用的供应商和敏感词。

- **权限与合规**（`scripts/e2e/g3-compliance.cjs`）：管理员编辑、停用、启用员工并重置密码，新建自定义角色；
  新建带手机号和邮箱的客户后列表只显示掩码，按完整手机号搜索、查看完整联系方式；输入密码导出 CSV；合并重复客户；
  生成个人信息副本并删除客户；设置保留期；操作日志；修改自己的密码；坐席发送含 EICAR 测试串的文件后被病毒扫描
  拦截；运营后台轮换数据密钥。前置同 M4，另外运行模拟 clamd（`uv run python -m tests.fake_clamd --port 3310`），
  后端和调度进程设置 `EDP_CLAMAV_HOST=127.0.0.1`。

- **会话与路由**（`scripts/e2e/g4-routing-collab.cjs`）：界面上设置技能组溢出和路由策略（VIP 标签、排队时 AI
  回答、按意图分配）；三位访客排队，VIP 和投诉的客户排在前面，按意图排到售后组；访客取消排队；1 分钟后溢出到
  备用技能组；主管旁听、坐席邀请同事协助；交还 AI 后 AI 继续回答，主管再把 AI 会话转人工；排队期间 AI 继续回答、
  取消排队回到 AI；坐席申请转移客户，管理员批准。前置同 P3（脚本等待溢出，约需 3 分钟）。

- **P1 M1**（`scripts/e2e/m1-im-acceptance.cjs`）：访客在 Widget 里发消息、实时收到机器人回复，消息经回调入库；
  刷新后仍是同一个访客。需要 OpenIM、后端和 Widget。提供停止/启动后端和对账的命令时，还会验证
  "后端停机期间回调丢失的消息由对账补录"。

  ```bash
  NODE_PATH=$(npm root -g) PLATFORM_PASSWORD=<平台账号密码> \
    BACKEND_STOP_CMD="<停止后端的命令>" BACKEND_START_CMD="<启动后端的命令>" \
    RECONCILE_CMD="cd backend && uv run python -m app.cli im-reconcile" \
    node scripts/e2e/m1-im-acceptance.cjs
  ```
