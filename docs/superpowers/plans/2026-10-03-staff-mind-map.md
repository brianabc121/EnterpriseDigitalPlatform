# 员工方向分支 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 企业和每张员工卡片能向左、向右、向下创建员工，布局跨登录和电脑持久保存。

**Architecture:** 员工记录新增可空的来源卡片与方向字段，创建接口在同一事务中保存员工与布局。前端纯函数生成稳定坐标与连线，图形组件负责尺寸测量和展示，员工页面复用现有创建表单与权限逻辑。

**Tech Stack:** FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL；Vue 3、TypeScript、Element Plus、Vitest。

**Spec:** `docs/superpowers/specs/2026-10-03-staff-mind-map-design.md`

## Global Constraints

- 方向仅为 `left`、`right`、`down`，卡片连线不改变角色、权限和客户可见范围。
- 无 `staff:manage` 的用户不能新增；不得分配超出操作者自身的权限。
- 现有员工和旧创建请求兼容；原员工管理操作保留。
- 本机启动与数据库迁移由用户自行执行；GitHub 推送须由用户明确要求。
- 执行方式：在当前会话由主代理直接实现，不并行委派。

## Review Focus

- 长姓名、多角色卡片高度变化：测量后连线和节点仍不重叠。
- 同方向连续新增：稳定排序、每人仅一张卡片。
- 非当前租户来源：创建拒绝且事务无残留。
- 原节点停用或来源不可见：节点不丢失，权限不继承。
- 保存失败或取消：保留合理表单状态，不插入虚假节点。

### Task 1: 持久布局与员工创建接口

**Files:**
- Create: `backend/alembic/versions/0037_staff_diagram.py`
- Modify: `backend/app/modules/iam/models.py`, `schemas.py`, `service.py`
- Test: `backend/tests/test_staff_diagram.py`

**Interface:** `StaffCreate` 增加可空 `diagram_parent_id: UUID` 和 `diagram_direction: Literal['left', 'right', 'down']`，`StaffOut` 返回同名字段。模型保存对应字段。

- [ ] 写失败测试：企业来源 null 配合三个合法方向均能创建；已有员工来源可创建；旧请求不带字段仍成功；读取列表保留字段。
- [ ] 写失败测试：非法方向返回 422；有来源无方向返回 422；不存在及其他租户来源被拒绝；拒绝后用户名查不到；权限仍按角色与 access 计算。
- [ ] 运行 `pytest tests/test_staff_diagram.py` 确认新增行为缺失。
- [ ] 创建 0037（down_revision 0036）迁移，两字段默认 null；添加方向及来源/方向配对约束，同租户来源检查，downgrade 删除约束与字段。
- [ ] 在现有 `create_staff` 事务内验证来源、保存字段、记录审计；在 `staff_out` 输出布局。不改权限计算。
- [ ] 运行新增后端测试与既有员工管理测试；环境缺依赖时明确记录，不能对本机数据库执行迁移。
- [ ] 更新接口 OpenAPI 和 `frontend/packages/api-client/src/schema.d.ts`，使用仓库既有导出/生成流程。

### Task 2: 稳定布局算法

**Files:**
- Create: `frontend/apps/console/src/staffDiagram.ts`, `staffDiagram.test.ts`
- Modify: `frontend/apps/console/src/components/staff/StaffTree.vue`

**Interface:** `layoutStaffDiagram(staff, sizes)` 输出企业与员工节点的边界坐标、来源连线和画布大小；sizes 以节点 ID 索引实际卡片宽高。

- [ ] 写失败测试：三个方向位于来源相应侧；同方向多个节点稳定且不相交；显式分支不重复出现在默认角色层；无管理员、停用成员和异常来源均保留节点。
- [ ] 写失败测试：至少三层分支、长卡片尺寸、空数据和重复调用得到合法且稳定布局；循环异常采用企业备用分支而不无限递归。
- [ ] 运行 `pnpm --filter @edp/console test src/staffDiagram.test.ts` 确认行为缺失。
- [ ] 实现分支占位与不相交布局；已有无布局员工仍按管理员层与成员层显示；企业来源方向分支按显式方向展开；同方向按创建时间和 ID 排序。
- [ ] 图形组件使用定位卡片与 SVG 连线，连接卡片边界；ResizeObserver 更新实际尺寸，卸载清理；图形区域可双向滚动且键盘可访问。
- [ ] 运行布局测试及 console 类型检查。

### Task 3: 卡片新增入口与创建流程

**Files:**
- Modify: `frontend/apps/console/src/views/StaffView.vue`, `components/staff/StaffTree.vue`
- Modify: `scripts/e2e/p0-acceptance.cjs`, `g3-compliance.cjs`, `p20-staff-access-acceptance.cjs`
- Create: `scripts/e2e/staff-diagram-acceptance.cjs`

**Interface:** StaffTree 发出 `add-branch(parentId: string | null, direction: 'left' | 'right' | 'down')`；页面缓存创建来源与方向，提交现有 POST `/api/v1/staff` 的新增字段。

- [ ] 先写验收：企业和员工的三个按钮打开创建弹窗；弹窗展示来源与方向；取消不新增；创建后位置正确，刷新保持；只读账号看不到新增按钮。
- [ ] 所有卡片添加三个新增入口，仅 `canManage` 显示；复用现有创建弹窗与 access 编辑器，禁止从来源继承权限。
- [ ] 顶部普通“新建员工”清空来源和方向；来源创建提交布局字段；提交失败保持弹窗，无乐观插入；成功重新加载并滚动定位新节点。
- [ ] 保留卡片编辑、重置、启停、交接和测试 ID；更新验收中依赖表格结构的定位。
- [ ] 浏览器检查三个方向、多个管理员、多层与窄屏；没有可用登录会话时记录实际阻塞，不假称验收通过。

### Task 4: 集成验证与说明

**Files:** Modify `README.md`，补充本次升级需要先执行迁移与重启后端的提示。

- [ ] 执行前端 `pnpm test`、console `typecheck`、`build`，对修改文件执行 eslint；运行后端可用的测试与静态检查。
- [ ] 检查迁移、旧请求、布局来源校验、角色权限不变和空数据处理；`git diff --check`。
- [ ] README 明确按原 Windows 迁移步骤升级数据库，用户自行执行并重启后端；交付时报告通过的检查及未执行的真实浏览器/数据库检查。
- [ ] 只在用户要求时推送 GitHub。

## 实施记录

- 已实现 0037 员工布局字段及 0038 独立导图节点迁移，草稿节点不创建账号、不占员工席位。
- 用户最新确认的流程替代 Task 3 的先弹表单方案：悬停卡片边缘显示圆形＋；点击立即保存对应方向的待完善卡片；随后点击卡片编辑资料、角色和权限。
- 草稿转员工在同一事务中绑定账号，保留卡片 ID 和子分支；租户隔离、来源校验、重复转换锁与权限校验已实现。
- 已实现尺寸测量、稳定分支布局和障碍绕行；旧员工默认布局兼容，多个管理员独立配置权限。
- 前端 283 项测试通过；后端独立 unittest 5 项通过，Ruff 和三个服务文件 mypy 通过；六项 PostgreSQL 接口测试已成功收集。
- 浏览器使用临时测试数据验证悬停显示、点击直接新增、不弹表单、后续编辑、卡片绑定后子分支保留、实际尺寸无重叠及只读入口隐藏。临时文件和标签页已清理。
- 独立只读审查通过；此前发现的连线穿卡片回退缺陷已修复并添加回归测试。
- PostgreSQL 接口集成未完成运行。误用 pytest 运行独立单测触发测试库 fixture，并在读取 alembic.ini 时遇到 Windows GBK 编码错误；随后改用 unittest 验证通过。未迁移用户应用数据库。
- 用户自行按 README 迁移到 0038 并重启后端后，可运行真实创建/刷新验收脚本。
- 后续已增加卡片/账号删除、企业根保护、独立财务/出纳角色、移除知识管理员及统一角色显示名称。
- 最新前端 283 项测试、后端 6 项独立测试、类型及静态检查通过；0039 pending trigger events 已通过 SET CONSTRAINTS ALL IMMEDIATE 修复并在 PostgreSQL 临时表验证。用户确认本地迁移成功。
- 推送前发现 origin/main 已新增合同、意向客户、企业资料，其 0037–0039 迁移与本分支编号冲突；本次先发布独立分支，未合并主分支。
