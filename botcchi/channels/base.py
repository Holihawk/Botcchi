"""Channel 抽象：agent 核心只认识"来自某个会话的一条消息" """

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

ChatType = Literal["private", "group", "web"]


@dataclass(frozen=True, slots=True)
class SessionKey:
    """会话标识：群聊按群共享一份对话，私聊/网页按用户隔离"""

    channel: str
    chat_type: ChatType
    chat_id: str

    def __str__(self) -> str:
        return f"{self.channel}:{self.chat_type}:{self.chat_id}"

    @classmethod
    def parse(cls, value: str) -> "SessionKey":
        """从 str(SessionKey) 还原（定时任务等持久化场景）"""
        channel, chat_type, chat_id = value.split(":", 2)
        return cls(channel=channel, chat_type=chat_type, chat_id=chat_id)  # type: ignore[arg-type]


@dataclass(slots=True)
class IncomingMessage:
    session: SessionKey
    user_id: str
    user_name: str
    text: str
    images: list[str] = field(default_factory=list)
    image_keys: list[str] = field(default_factory=list)
    """图片内容指纹（OneBot image 段的 file 字段，QQ 为 md5 文件名），
    与 images 一一对应，用于复读等内容一致性判定"""
    at_me: bool = False
    sender_role: str | None = None
    """发送者的群内身份：owner / admin / member（私聊为 None），权限判定用"""
    mentions: list[str] = field(default_factory=list)
    """消息中 @ 到的其他用户 id（不含 bot 自身），封禁/授权等指令的目标解析用"""
    message_id: str | None = None
    self_id: str | None = None
    reply_to_id: str | None = None
    """本条消息引用（回复）的消息 id，channel 适配器负责解析其内容"""
    raw: Any = None


@dataclass(slots=True)
class IncomingRequest:
    """好友申请 / 拉群邀请等待办请求（超管审批用）"""

    kind: str
    """friend = 好友申请；group = 群相关请求"""
    flag: str
    """协议端的请求凭据，审批时原样带回"""
    user_id: str
    user_name: str = ""
    sub_type: str = ""
    """group 请求细分：invite = 邀请 bot 入群；add = 申请加入 bot 管理的群；
    friend 请求为 doubt 时表示「可疑好友申请」——协议端只能同意不能拒绝"""
    group_id: str = ""
    group_name: str = ""
    comment: str = ""
    """验证消息/备注"""


class Channel(ABC):
    name: str

    @property
    def is_connected(self) -> bool:
        """当前是否有可用连接。无连接概念的渠道恒为 True。

        反向 WS 这类"等对端来连"的渠道启动后有一段空窗期，主动发起调用的
        后台任务应先问一句，否则只会得到一串「没有可用连接」。
        """
        return True

    async def prepare_incoming(self, incoming: IncomingMessage) -> None:
        """在消息交给模型前准备渠道资源；默认无需处理。"""

    @abstractmethod
    async def send_text(
        self,
        session: SessionKey,
        text: str,
        *,
        reply_to: str | None = None,
        at_user: str | None = None,
    ) -> str | None:
        """向会话发送一段文本，返回已发送的 message_id（channel 支持时）。

        reply_to 为被回复的 message_id；at_user 为要 @ 的用户 id（提醒等场景，
        置于消息最前）。均在 channel 支持时生效。
        """
        ...

    async def send_image(self, session: SessionKey, file: str) -> str | None:
        """发送图片（URL / base64 / 本地路径，随 channel 支持），返回
        message_id（channel 支持时）。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持发送图片")

    async def send_sticker(self, session: SessionKey, file: str) -> None:
        """以表情包形式发送图片（QQ 中显示为小表情而非大图）。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持发送表情")

    async def send_video(self, session: SessionKey, file: str) -> None:
        """发送视频（base64 / 本地路径 / URL）。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持发送视频")

    async def send_record(self, session: SessionKey, file: str) -> None:
        """发送语音/音频（base64 / 本地路径 / URL）。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持发送音频")

    async def send_forward(
        self, session: SessionKey, nodes: list[tuple[str, str, str | None]]
    ) -> None:
        """发送合并转发消息：nodes 为 (昵称, 文本, 图片URL或None) 列表。

        用于一次性发多张图/多条内容而不刷屏（QQ 聊天记录卡片）。默认不支持，
        调用方应捕获 NotImplementedError 后退化为逐条发送。
        """
        raise NotImplementedError(f"channel {self.name} 不支持合并转发")

    async def recall(self, message_id: str) -> bool:
        """撤回一条消息（通常为 bot 自己发的），返回是否成功。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持撤回消息")

    async def poke(self, session: SessionKey, user_id: str) -> bool:
        """QQ 戳一戳：群里戳群成员，私聊戳好友。返回是否成功。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持戳一戳")

    async def send_music(
        self, session: SessionKey, platform: str, music_id: str
    ) -> None:
        """发送音乐分享卡片（platform 如 163=网易云、qq）。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持音乐卡片")

    async def send_like(self, user_id: str, times: int = 10) -> bool:
        """给用户资料卡点赞（QQ 特有），返回是否成功。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持点赞")

    async def is_friend(
        self, user_id: str, *, refresh: bool = False
    ) -> bool | None:
        """用户是否为 bot 好友；None 表示无法判定（默认）。

        refresh=True 跳过一切缓存重新查询——刚加上好友时缓存还是旧的。
        """
        return None

    async def list_friends(self) -> list[tuple[str, str]]:
        """好友列表 [(user_id, 昵称)]。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持好友列表")

    async def list_groups(self) -> list[tuple[str, str]]:
        """已加入的群列表 [(group_id, 群名)]。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持群组列表")

    async def leave_group(self, group_id: str) -> bool:
        """退出指定群，返回是否成功。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持退群")

    async def set_request_result(
        self, req: "IncomingRequest", approve: bool
    ) -> bool:
        """审批一条好友/群请求，返回是否调用成功。默认不支持。"""
        raise NotImplementedError(f"channel {self.name} 不支持请求审批")

    async def fetch_pending_requests(self) -> list["IncomingRequest"]:
        """主动查询协议端侧的待办申请。默认无（只靠推送事件）。

        推送是不可靠的：bot 离线期间到达的申请收不到，QQ 判定为「可疑」的
        好友申请更是压根不推事件。所以补一条主动补捞的路。
        """
        return []
