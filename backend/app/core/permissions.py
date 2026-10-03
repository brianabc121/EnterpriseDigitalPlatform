"""权限点与系统角色。前端菜单也按这些权限点显示（frontend/apps/console/src/menu.ts）。"""

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    DASHBOARD_VIEW = "dashboard:view"
    WORKBENCH_USE = "workbench:use"  # 接待会话（P1 工作台）
    CUSTOMER_READ = "customer:read"  # 查看客户（受数据范围约束）
    CUSTOMER_READ_ALL = "customer:read_all"  # 查看本租户全部客户
    CUSTOMER_CREATE = "customer:create"
    CUSTOMER_ASSIGN = "customer:assign"  # 指定或变更客户的归属坐席
    CUSTOMER_VIEW_SENSITIVE = "customer:view_sensitive"  # 查看手机号、邮箱明文（每次记审计）
    CUSTOMER_EXPORT = "customer:export"  # 导出可见范围内的客户名单（需要再次输入密码）
    CUSTOMER_MANAGE = "customer:manage"  # 合并重复客户，处理个人信息查询与删除请求
    SESSION_READ_ALL = "session:read_all"  # 查看本租户全部会话（及其客户）
    SESSION_READ_TEAM = "session:read_team"  # 组长查看所带技能组的会话、组员的客户
    SESSION_TRANSFER = "session:transfer"  # 转接自己接待中的会话
    SESSION_TRANSFER_ANY = "session:transfer_any"  # 转接或强制转接可见范围内的任意会话
    SESSION_MONITOR = "session:monitor"  # 旁听可见范围内的会话（客户看不到旁听者）
    ROUTING_MANAGE = "routing:manage"  # 技能组、路由策略、坐席并发
    STAFF_READ = "staff:read"
    STAFF_MANAGE = "staff:manage"
    QUICK_REPLY_MANAGE = "quick_reply:manage"  # 维护全员共享的快捷话术
    KB_READ = "kb:read"
    KB_MANAGE = "kb:manage"
    KB_PUBLISH = "kb:publish"
    FORM_KB_MANAGE = "form_kb:manage"  # 表单知识：新增、修改、确认、停用，设置（§25.18）
    REPORT_VIEW = "report:view"
    SETTINGS_MANAGE = "settings:manage"
    BROADCAST_MANAGE = "broadcast:manage"  # 企业微信群发任务（发给可见范围内的客户或客户群）
    TENANT_MANAGE = "tenant:manage"  # 数据导出、注销、授权平台运维访问（只有租户管理员）
    AUDIT_READ = "audit:read"  # 查看本租户的操作日志
    TODO_READ = "todo:read"  # 查看待办（受数据范围约束）
    TODO_HANDLE = "todo:handle"  # 认领、处理、转交自己的待办，确认或驳回交给自己确认的待办
    TODO_ASSIGN = "todo:assign"  # 分派和改派任何人的待办，确认或驳回数据范围内的全部待确认
    TODO_CONFIG = "todo:config"  # 待办类型与待办设置
    TODO_EXPORT = "todo:export"  # 导出待办
    ORDER_READ = "order:read"  # 查看订单（受数据范围约束）
    ORDER_CREATE = "order:create"  # 新建订单、提交审核
    ORDER_REVIEW = "order:review"  # 审核（确认、取消）和跟进订单
    ORDER_PRICE = "order:price"  # 改价和优惠
    ORDER_PAYMENT = "order:payment"  # 登记和作废收款、登记退款
    ORDER_CREDIT = "order:credit"  # 主管审批：同意暂欠、超过折扣上限的优惠
    ORDER_EXPORT = "order:export"  # 导出订单
    ORDER_CONFIG = "order:config"  # 订单设置、仓库设置（仓管）
    PRODUCT_MANAGE = "product:manage"  # 维护商品库（新建、编辑、Excel 导入）
    PRODUCT_VIEW_COST = "product:view_cost"  # 查看和导出成本价
    INVENTORY_MANAGE = "inventory:manage"  # 仓库：查看和调整库存、维护材料、开领料单和入库单
    WAREHOUSE_CONFIRM = "warehouse:confirm"  # 仓管：确认或退回领料单和入库单
    PRODUCTION_WORK = "production:work"  # 加工：领取订单，标记商品完成或缺货，完成加工
    PRODUCTION_ASSIGN = "production:assign"  # 指派和改派加工人，查看全部加工进度
    INTEGRATION_MANAGE = "integration:manage"  # 企业系统对接：接口密钥与事件推送
    TASK_USE = "task:use"  # 个人待办：自己的事项（设计文档 §27.2）
    TASK_ASSIGN = "task:assign"  # 给别人布置个人待办
    TASK_READ_ALL = "task:read_all"  # 查看全员的个人待办，代为处理
    ASSISTANT_USE = "assistant:use"  # 与 AI 公司助理对话、绑定 IM 账号（设计文档 §27.3）
    FINANCE_VIEW = "finance:view"  # 应收账款：查看全公司的应收、账龄和对账单（设计文档 §28.5）
    FINANCE_MANAGE = "finance:manage"  # 应收账款：跟进、催收、导出
    PRINT_MANAGE = "print:manage"  # 云打印机：打印机设置、全部打印记录、重新发送（设计文档 §29.4）
    PROFIT_VIEW = "profit:view"  # 盈利报表：全公司的收入、成本、毛利、费用、净利润和导出（§30.5）
    PROFIT_MANAGE = "profit:manage"  # 盈利报表：登记、修改、删除费用和其他收入
    CONTRACT_USE = "contract:use"  # 合同：起草和处理自己负责的合同，上传模板（设计文档 §34.6）
    CONTRACT_MANAGE = "contract:manage"  # 合同：全部合同、模板和分类，合同设置
    MATERIAL_USE = "material:use"  # 企业资料：查看、上传、分享，修改自己上传的（设计文档 §36.5）
    MATERIAL_MANAGE = "material:manage"  # 企业资料：文件夹，修改和删除全部资料，停用别人的分享
    TOKEN_VIEW = "token:view"  # Token 计费：企业 AI 用掉的 tokens 和费用、近 7 天的明细（§37.6）
    OPPORTUNITY_READ = "opportunity:read"  # 商机：自己负责的和能看到的客户的（设计文档 §40.11）
    OPPORTUNITY_READ_ALL = "opportunity:read_all"  # 商机：全部
    OPPORTUNITY_MANAGE = "opportunity:manage"  # 商机：新建、修改、跟进、换阶段、赢单 / 输单
    OPPORTUNITY_ASSIGN = "opportunity:assign"  # 商机：把负责人改成别人，商机设置和阶段
    OPPORTUNITY_EXPORT = "opportunity:export"  # 商机：导出


ALL_PERMISSIONS = frozenset(Permission)

# 权限点的名称和分组（角色编辑页面按分组展示）。
PERMISSION_INFO: dict[Permission, tuple[str, str]] = {
    Permission.DASHBOARD_VIEW: ("查看首页概览", "基础"),
    Permission.WORKBENCH_USE: ("接待会话", "接待"),
    Permission.SESSION_TRANSFER: ("转接自己接待的会话", "接待"),
    Permission.SESSION_TRANSFER_ANY: ("转接或强制转接任意会话", "接待"),
    Permission.SESSION_READ_TEAM: ("查看所带技能组的会话和组员的客户", "接待"),
    Permission.SESSION_READ_ALL: ("查看全部会话", "接待"),
    Permission.SESSION_MONITOR: ("旁听会话", "接待"),
    Permission.QUICK_REPLY_MANAGE: ("维护全员共享的快捷话术", "接待"),
    Permission.CUSTOMER_READ: ("查看客户（自己的和正在接待的）", "客户"),
    Permission.CUSTOMER_READ_ALL: ("查看全部客户", "客户"),
    Permission.CUSTOMER_CREATE: ("新建客户", "客户"),
    Permission.CUSTOMER_ASSIGN: ("分配和转移客户归属", "客户"),
    Permission.CUSTOMER_VIEW_SENSITIVE: ("查看客户手机号和邮箱", "客户"),
    Permission.CUSTOMER_EXPORT: ("导出客户名单", "客户"),
    Permission.CUSTOMER_MANAGE: ("合并客户、处理个人信息请求", "客户"),
    Permission.BROADCAST_MANAGE: ("企业微信群发", "客户"),
    Permission.OPPORTUNITY_READ: ("查看商机（自己负责的和能看到的客户的）", "商机"),
    Permission.OPPORTUNITY_READ_ALL: ("查看全部商机", "商机"),
    Permission.OPPORTUNITY_MANAGE: ("新建、修改、跟进商机，换阶段，赢单和输单", "商机"),
    Permission.OPPORTUNITY_ASSIGN: ("把商机的负责人改成别人，商机设置和阶段", "商机"),
    Permission.OPPORTUNITY_EXPORT: ("导出商机", "商机"),
    Permission.TODO_READ: ("查看待办", "待办"),
    Permission.TODO_HANDLE: ("处理自己的待办、确认 AI 生成的待办", "待办"),
    Permission.TODO_ASSIGN: ("分派和改派待办", "待办"),
    Permission.TODO_CONFIG: ("待办类型和待办设置", "待办"),
    Permission.TODO_EXPORT: ("导出待办", "待办"),
    Permission.TASK_USE: ("个人待办：自己的事项", "待办"),
    Permission.TASK_ASSIGN: ("给别人布置个人待办", "待办"),
    Permission.TASK_READ_ALL: ("查看全员的个人待办", "待办"),
    Permission.ASSISTANT_USE: ("与 AI 公司助理对话、绑定 IM 账号", "基础"),
    Permission.ORDER_READ: ("查看订单", "订单"),
    Permission.ORDER_CREATE: ("新建订单", "订单"),
    Permission.ORDER_REVIEW: ("审核和跟进订单", "订单"),
    Permission.ORDER_PRICE: ("改价和优惠", "订单"),
    Permission.ORDER_PAYMENT: ("登记收款和退款", "订单"),
    Permission.ORDER_CREDIT: ("同意暂欠、审批超过折扣上限的优惠", "订单"),
    Permission.ORDER_EXPORT: ("导出订单", "订单"),
    Permission.ORDER_CONFIG: ("订单设置和仓库设置", "订单"),
    Permission.PRODUCT_MANAGE: ("维护商品库", "订单"),
    Permission.PRODUCT_VIEW_COST: ("查看和导出成本价", "订单"),
    Permission.FINANCE_VIEW: ("查看应收账款、账龄和对账单（能看到全部订单）", "财务"),
    Permission.FINANCE_MANAGE: ("应收的跟进、催收和导出", "财务"),
    Permission.PROFIT_VIEW: ("查看和导出盈利报表（全公司的收入、成本、毛利和净利润）", "财务"),
    Permission.PROFIT_MANAGE: ("登记、修改和删除费用与其他收入", "财务"),
    Permission.TOKEN_VIEW: (
        "查看企业 AI 用掉的 tokens 和费用、近 7 天的调用明细，导出明细",
        "财务",
    ),
    Permission.CONTRACT_USE: (
        "起草和处理自己负责的合同（AI 生成、编辑、定稿、签署），上传合同模板",
        "合同",
    ),
    Permission.CONTRACT_MANAGE: ("管理全部合同、模板和分类，合同设置", "合同"),
    Permission.MATERIAL_USE: (
        "查看、下载和上传企业资料，写文字资料，分享给客户，修改和删除自己上传的",
        "资料",
    ),
    Permission.MATERIAL_MANAGE: ("管理资料文件夹，修改和删除全部资料，停用别人的分享链接", "资料"),
    Permission.PRINT_MANAGE: ("设置云打印机、查看全部打印记录、重新发送", "管理"),
    Permission.PRODUCTION_WORK: ("领取订单加工，标记商品完成或缺货", "加工"),
    Permission.PRODUCTION_ASSIGN: ("指派加工人，查看全部加工进度", "加工"),
    Permission.INVENTORY_MANAGE: ("查看和调整库存（盘点、入库、出库、导入），维护材料", "仓库"),
    Permission.WAREHOUSE_CONFIRM: ("确认或退回领料单和入库单（仓管）", "仓库"),
    Permission.KB_READ: ("查看知识库", "知识库"),
    Permission.KB_MANAGE: ("编辑知识和审核候选", "知识库"),
    Permission.KB_PUBLISH: ("发布知识", "知识库"),
    Permission.FORM_KB_MANAGE: ("维护表单知识（开单学到的叫法、用量、搭配）", "知识库"),
    Permission.REPORT_VIEW: ("查看报表", "管理"),
    Permission.ROUTING_MANAGE: ("技能组、路由策略和坐席并发", "管理"),
    Permission.STAFF_READ: ("查看员工和角色", "管理"),
    Permission.STAFF_MANAGE: ("管理员工和角色", "管理"),
    Permission.SETTINGS_MANAGE: ("渠道、AI 和企业设置", "管理"),
    Permission.AUDIT_READ: ("查看操作日志", "管理"),
    Permission.INTEGRATION_MANAGE: ("企业系统对接：接口密钥和事件推送", "管理"),
    Permission.TENANT_MANAGE: ("数据导出、注销和运维授权", "管理"),
}


@dataclass(frozen=True)
class RoleSpec:
    code: str
    name: str
    permissions: frozenset[Permission]


# 开通租户时创建的系统角色。系统角色的权限以这里为准（见 iam.service.role_permissions），
# 新增权限点后现有租户自动生效，不需要数据迁移。
DEFAULT_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec("tenant_admin", "企业所有者", ALL_PERMISSIONS),
    RoleSpec(
        "agent",
        "客服",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.WORKBENCH_USE,
                Permission.CUSTOMER_READ,
                Permission.CUSTOMER_CREATE,
                Permission.OPPORTUNITY_READ,
                Permission.OPPORTUNITY_MANAGE,
                Permission.KB_READ,
                Permission.SESSION_TRANSFER,
                Permission.TODO_READ,
                Permission.TODO_HANDLE,
                Permission.ORDER_READ,
                Permission.ORDER_CREATE,
                Permission.ORDER_REVIEW,
                Permission.ORDER_PAYMENT,
                Permission.CONTRACT_USE,
                Permission.MATERIAL_USE,
                Permission.TASK_USE,
                Permission.ASSISTANT_USE,
            }
        ),
    ),
    RoleSpec(
        "supervisor",
        "主管",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.WORKBENCH_USE,
                Permission.CUSTOMER_READ,
                Permission.CUSTOMER_CREATE,
                Permission.OPPORTUNITY_READ,
                Permission.OPPORTUNITY_READ_ALL,
                Permission.OPPORTUNITY_MANAGE,
                Permission.OPPORTUNITY_ASSIGN,
                Permission.OPPORTUNITY_EXPORT,
                Permission.KB_READ,
                Permission.REPORT_VIEW,
                Permission.SESSION_READ_TEAM,
                Permission.SESSION_TRANSFER,
                Permission.SESSION_TRANSFER_ANY,
                Permission.SESSION_MONITOR,
                Permission.QUICK_REPLY_MANAGE,
                Permission.BROADCAST_MANAGE,
                Permission.TODO_READ,
                Permission.TODO_HANDLE,
                Permission.TODO_ASSIGN,
                Permission.TODO_EXPORT,
                Permission.ORDER_READ,
                Permission.ORDER_CREATE,
                Permission.ORDER_REVIEW,
                Permission.ORDER_PRICE,
                Permission.ORDER_PAYMENT,
                Permission.ORDER_CREDIT,
                Permission.ORDER_EXPORT,
                Permission.PRODUCTION_ASSIGN,
                Permission.INVENTORY_MANAGE,
                Permission.FORM_KB_MANAGE,
                Permission.CONTRACT_USE,
                Permission.CONTRACT_MANAGE,
                Permission.MATERIAL_USE,
                Permission.MATERIAL_MANAGE,
                Permission.TASK_USE,
                Permission.TASK_ASSIGN,
                Permission.ASSISTANT_USE,
            }
        ),
    ),
    RoleSpec(
        "finance",
        "财务",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.FINANCE_VIEW,
                Permission.FINANCE_MANAGE,
                Permission.ORDER_READ,
                Permission.ORDER_PAYMENT,
                Permission.CUSTOMER_READ,
                Permission.OPPORTUNITY_READ,
                Permission.OPPORTUNITY_READ_ALL,
                Permission.PROFIT_VIEW,
                Permission.PROFIT_MANAGE,
                Permission.TOKEN_VIEW,
                Permission.TASK_USE,
                Permission.ASSISTANT_USE,
            }
        ),
    ),
    RoleSpec(
        "cashier",
        "出纳",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.FINANCE_VIEW,
                Permission.ORDER_READ,
                Permission.ORDER_PAYMENT,
                Permission.CUSTOMER_READ,
                Permission.TASK_USE,
                Permission.ASSISTANT_USE,
            }
        ),
    ),
    # 工人只做加工（设计文档 §25.11）：看不到金额、客户电话和地址，也进不了订单中心。
    RoleSpec(
        "worker",
        "工厂工人",
        frozenset({Permission.PRODUCTION_WORK, Permission.TASK_USE, Permission.ASSISTANT_USE}),
    ),
    # 仓管（设计文档 §25.15）：确认领料单和入库单，管理材料和成品的库存。
    RoleSpec(
        "keeper",
        "仓管",
        frozenset(
            {
                Permission.DASHBOARD_VIEW,
                Permission.INVENTORY_MANAGE,
                Permission.WAREHOUSE_CONFIRM,
                Permission.TASK_USE,
                Permission.ASSISTANT_USE,
            }
        ),
    ),
)

TENANT_ADMIN_ROLE = "tenant_admin"
