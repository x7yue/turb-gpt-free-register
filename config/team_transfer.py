# -*- coding: utf-8 -*-
"""
团队转移配置项。

流程：母号邀请子号 → 子号接受邀请 → 子号合并个人空间数据 → 母号踢出子号。
子号邮箱统一使用 mail API（如 mail.siderchn.com）收取验证码：
    GET {TEAM_MAIL_API_BASE}{TEAM_MAIL_FETCH_PATH}?email=<邮箱>&password=<收信密码>&limit=1
"""
from config.env_loader import env_str, apply_env_overrides

# ---- 子号邮箱收信 API（generic_api 源复用） ----

# 收信 API 基址，例如 https://mail.siderchn.com
TEAM_MAIL_API_BASE: str = "https://mail.siderchn.com"

# 收信 API 路径
TEAM_MAIL_FETCH_PATH: str = "/emails"

# 单次拉取邮件条数
TEAM_MAIL_FETCH_LIMIT: int = 1

# 收信 API 请求超时（秒）
TEAM_MAIL_REQUEST_TIMEOUT: int = 40

# ---- 邀请/转移参数（对照 remove_personal_space 参考实现） ----

# 邀请时的角色
TRANSFER_ROLE: str = "standard-user"

# 邀请时的 seat 类型
TRANSFER_SEAT_TYPE: str = "default"

# 子号接受邀请时提交的 TOS 版本
TRANSFER_ACCEPTED_TOS_VERSION: str = "2024-12-17"

# 兼容旧配置名；合并个人空间实际只提交 workspace_id
TRANSFER_PERSONAL: bool = True

# ---- 步间延时（秒） ----

# 邀请成功后等待
TEAM_DELAY_AFTER_INVITE: int = 3

# 接受邀请成功后等待
TEAM_DELAY_AFTER_ACCEPT: int = 2

# 子号合并个人空间成功后等待
TEAM_DELAY_AFTER_TRANSFER: int = 5

# ---- 执行控制 ----

# 子号转移并发线程数（母号相关步骤内部串行加锁）
TEAM_TRANSFER_WORKERS: int = 2

# 转移队列上限
TEAM_TRANSFER_QUEUE_LIMIT: int = 500

# 每步请求超时（秒）
TEAM_TRANSFER_REQUEST_TIMEOUT: int = 30

# 每步最大尝试次数（仅临时性网络错误重试）
TEAM_TRANSFER_MAX_ATTEMPTS: int = 3

# 每步重试基础退避（秒）
TEAM_TRANSFER_RETRY_DELAY: int = 2

# ---- 母号登录 ----

# 母号登录等待人工 OTP 的超时（秒）
TEAM_ADMIN_OTP_TIMEOUT: int = 300

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'TEAM_MAIL_API_BASE': 'str',
    'TEAM_MAIL_FETCH_PATH': 'str',
    'TEAM_MAIL_FETCH_LIMIT': 'int',
    'TEAM_MAIL_REQUEST_TIMEOUT': 'int',
    'TRANSFER_ROLE': 'str',
    'TRANSFER_SEAT_TYPE': 'str',
    'TRANSFER_ACCEPTED_TOS_VERSION': 'str',
    'TRANSFER_PERSONAL': 'bool',
    'TEAM_DELAY_AFTER_INVITE': 'int',
    'TEAM_DELAY_AFTER_ACCEPT': 'int',
    'TEAM_DELAY_AFTER_TRANSFER': 'int',
    'TEAM_TRANSFER_WORKERS': 'int',
    'TEAM_TRANSFER_QUEUE_LIMIT': 'int',
    'TEAM_TRANSFER_REQUEST_TIMEOUT': 'int',
    'TEAM_TRANSFER_MAX_ATTEMPTS': 'int',
    'TEAM_TRANSFER_RETRY_DELAY': 'int',
    'TEAM_ADMIN_OTP_TIMEOUT': 'int',
})
