-- 平台使用的两个数据库登录角色（仅用于开发环境；生产环境由运维创建并使用强密码）。
-- edp_app：处理租户员工请求，受 RLS 租户隔离约束。
-- edp_platform：平台运营与租户开通，通过专用 RLS 策略访问全部行。
CREATE ROLE edp_app LOGIN PASSWORD 'edp_app';
CREATE ROLE edp_platform LOGIN PASSWORD 'edp_platform';
GRANT CONNECT ON DATABASE edp TO edp_app, edp_platform;

-- 知识库检索（P3）使用。
CREATE EXTENSION IF NOT EXISTS vector;
