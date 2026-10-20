"""数据库模型。

命名带 Row 后缀以区分域模型（agent.messages.ChatMessage 等）。
JSON 列在 PostgreSQL 上自动使用 JSONB，SQLite 上退化为 JSON（测试/轻量部署）。
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonColumn = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """此时间之前的消息不再进入上下文（reset 指令写入）"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conv_id_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column(JsonColumn)
    """预留：工具调用记录、用量统计、消息树分支等"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class MemoryRow(Base):
    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_scope_key", "scope_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(128))
    """记忆归属：user:<id> 或 group:<id>"""
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class GroupMessageRow(Base):
    """群消息归档：群聊总结与统计的数据源（仅文本，图片记占位符）"""

    __tablename__ = "group_messages"
    __table_args__ = (Index("ix_group_messages_group_id_id", "group_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    user_name: Mapped[str] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ScheduledJobRow(Base):
    """统一定时任务：定时总结/孤独日报/提醒等都是一种 kind 的 job"""

    __tablename__ = "scheduled_jobs"
    __table_args__ = (Index("ix_scheduled_jobs_enabled_next", "enabled", "next_run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    """handler 自定义参数（JSON）"""
    session_key: Mapped[str] = mapped_column(String(128))
    """投递目标会话（str(SessionKey)）"""
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recurrence: Mapped[str | None] = mapped_column(String(32))
    """None = 一次性；"daily@HH:MM" = 每天定点"""
    enabled: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    """发起用户（提醒类任务的归属）"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SocialUserRow(Base):
    """签到/好感度/金币的用户档案（按用户全局，不分群）"""

    __tablename__ = "social_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_name: Mapped[str] = mapped_column(String(128), default="")
    """最近一次互动时的昵称（排行榜展示用）"""
    impression: Mapped[float] = mapped_column(default=0.0)
    coins: Mapped[int] = mapped_column(default=0)
    sign_count: Mapped[int] = mapped_column(default=0)
    streak: Mapped[int] = mapped_column(default=0)
    """连续签到天数"""
    last_sign_date: Mapped[str | None] = mapped_column(String(10))
    """上次签到日期 YYYY-MM-DD（Asia/Shanghai）"""
    pendant: Mapped[str | None] = mapped_column(String(32))
    """当前佩戴的头像挂件商品 id（shop.GOODS），None = 未佩戴"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class CoinLogRow(Base):
    """金币流水：为商店/银行/决斗等后续玩法预留的账本"""

    __tablename__ = "coin_logs"
    __table_args__ = (Index("ix_coin_logs_user_id_id", "user_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column()
    """正为收入，负为支出"""
    reason: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class UserItemRow(Base):
    """用户背包：商店购得的挂件与消耗品（挂件 count 恒为 1）"""

    __tablename__ = "user_items"
    __table_args__ = (
        Index("ix_user_items_user_item", "user_id", "item_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(String(32))
    count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class GameEscrowRow(Base):
    """对局中的入场费托管：入座时记一行，正常终局整组删除。

    入场费可以由游戏券和金币混合支付。对局状态在内存中，进程崩溃/重启会丢局，
    启动时把残留托管全数退款。
    """

    __tablename__ = "game_escrows"
    __table_args__ = (Index("ix_game_escrows_game_group", "game", "group_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game: Mapped[str] = mapped_column(String(16))
    """duel / game24 / blackjack"""
    group_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    tickets: Mapped[int] = mapped_column()
    coins: Mapped[int] = mapped_column(default=0)
    """本次入场直接扣除的金币"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class UsageStatRow(Base):
    """功能调用流水：WebUI 统计面板的数据源（按消息/调用逐条记录）"""

    __tablename__ = "usage_stats"
    __table_args__ = (
        Index("ix_usage_stats_day", "day"),
        Index("ix_usage_stats_name", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    """command / tool / chat"""
    name: Mapped[str] = mapped_column(String(64))
    """功能开关名或工具名"""
    user_id: Mapped[str] = mapped_column(String(64))
    user_name: Mapped[str] = mapped_column(String(128), default="")
    group_id: Mapped[str] = mapped_column(String(64), default="")
    """私聊为空串"""
    day: Mapped[str] = mapped_column(String(10))
    """记录日 YYYY-MM-DD（Asia/Shanghai，写入时计算便于按日聚合）"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PendingRequestRow(Base):
    """待审批请求：好友申请 / 拉群邀请，超管经「同意请求 <id>」等指令处理"""

    __tablename__ = "pending_requests"
    __table_args__ = (Index("ix_pending_requests_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    """friend / group"""
    sub_type: Mapped[str] = mapped_column(String(16), default="")
    """group 细分：invite（邀请 bot 入群）/ add（申请加入 bot 管理的群）"""
    flag: Mapped[str] = mapped_column(String(128))
    """协议端审批凭据（同意/拒绝时原样带回，可能过期）"""
    user_id: Mapped[str] = mapped_column(String(64))
    user_name: Mapped[str] = mapped_column(String(128), default="")
    group_id: Mapped[str] = mapped_column(String(64), default="")
    group_name: Mapped[str] = mapped_column(String(128), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    """pending / approved / rejected / ignored"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class UserLevelRow(Base):
    """数值权限等级授予：group_id 为 None 表示全局等级。

    生效等级 = max(全局授予, 本群授予, 群内身份隐式等级)，超管恒为最高。
    """

    __tablename__ = "user_levels"
    __table_args__ = (
        Index("ix_user_levels_user_group", "user_id", "group_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    group_id: Mapped[str | None] = mapped_column(String(64))
    level: Mapped[int] = mapped_column()
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class BanRow(Base):
    """封禁：user+group = 群内封禁；仅 user = 全局封禁；仅 group = 封整群"""

    __tablename__ = "bans"
    __table_args__ = (Index("ix_bans_user_group", "user_id", "group_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64))
    group_id: Mapped[str | None] = mapped_column(String(64))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """None = 永久"""
    reason: Mapped[str] = mapped_column(String(255), default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    by_superuser: Mapped[bool] = mapped_column(default=False)
    """超管下的封禁，群管无法解除"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class GroupSettingRow(Base):
    """群级设置：休眠状态与功能禁用列表（普通/超管两条，群管解不开后者）"""

    __tablename__ = "group_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(default=True)
    """False = 休眠：除权限指令外不响应本群任何消息"""
    blocked: Mapped[list | None] = mapped_column(JsonColumn, default=None)
    su_blocked: Mapped[list | None] = mapped_column(JsonColumn, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GroupGreetingRow(Base):
    """群迎新文案：欢迎语与退群提示的自定义覆盖（空 = 用默认文案）"""

    __tablename__ = "group_greetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    welcome: Mapped[str | None] = mapped_column(Text)
    farewell: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PersonaBindingRow(Base):
    __tablename__ = "persona_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    assistant: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
