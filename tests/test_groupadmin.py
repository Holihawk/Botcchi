"""群管理：进群欢迎/退群提醒、迎新文案管理、消息撤回。"""

from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.groupadmin import DEFAULT_WELCOME

SU = "900000"


class AdminChannel(FakeChannel):
    name = "fake"

    def __init__(self, recall_ok=True):
        super().__init__()
        self.recalled: list[str] = []
        self.recall_ok = recall_ok

    async def recall(self, message_id: str) -> bool:
        self.recalled.append(message_id)
        return self.recall_ok


def msg(
    text="", user_id="10001", role=None, reply_to=None, chat_type="group"
) -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type=chat_type, chat_id="777"),
        user_id=user_id,
        user_name=f"用户{user_id}",
        text=text,
        at_me=True,
        sender_role=role,
        reply_to_id=reply_to,
    )


async def make_ctx(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = [SU]
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    return ctx


async def test_welcome_and_farewell_defaults(mock_openai, tmp_path):
    ctx = await make_ctx(mock_openai, tmp_path)
    channel = AdminChannel()
    admin = ctx.service.groupadmin

    await admin.on_member_change(channel, "777", "20002", "", "join")
    assert channel.sent[-1][1] == DEFAULT_WELCOME
    assert channel.ats[-1] == "20002"  # 欢迎带 @ 新人

    await admin.on_member_change(channel, "777", "20002", "小虹", "leave")
    assert "小虹" in channel.sent[-1][1] and "离开" in channel.sent[-1][1]
    assert channel.ats[-1] is None

    # 被踢：带操作者用旧版文案，拿不到操作者时退化
    await admin.on_member_change(channel, "777", "20003", "", "kick", "群主大人")
    assert channel.sent[-1][1] == "20003 被 群主大人 送走了."
    await admin.on_member_change(channel, "777", "20004", "", "kick")
    assert "20004" in channel.sent[-1][1] and "请出" in channel.sent[-1][1]
    await ctx.shutdown()


async def test_custom_greeting_management(mock_openai, tmp_path):
    ctx = await make_ctx(mock_openai, tmp_path)
    channel = AdminChannel()
    service = ctx.service

    # 普通成员不能设置
    await service.handle(msg("设置欢迎语 欢迎光临小酒馆"), channel)
    assert "只有管理者" in channel.sent[-1][1]

    # 群管理员可以
    await service.handle(
        msg("设置欢迎语 欢迎光临小酒馆！", role="admin"), channel
    )
    assert "已更新" in channel.sent[-1][1]
    await service.handle(
        msg("设置退群提示 {名字} 跑路了", role="admin"), channel
    )
    assert "已更新" in channel.sent[-1][1]

    admin = service.groupadmin
    await admin.on_member_change(channel, "777", "2", "", "join")
    assert channel.sent[-1][1] == "欢迎光临小酒馆！"
    await admin.on_member_change(channel, "777", "2", "小绿", "leave")
    assert channel.sent[-1][1] == "小绿 跑路了"
    # kick 也走自定义模板
    await admin.on_member_change(channel, "777", "2", "小绿", "kick")
    assert channel.sent[-1][1] == "小绿 跑路了"

    # 查看与恢复默认
    await service.handle(msg("查看欢迎语", role="admin"), channel)
    assert "欢迎光临小酒馆" in channel.sent[-1][1]
    await service.handle(msg("设置欢迎语 默认", role="admin"), channel)
    assert "已恢复默认" in channel.sent[-1][1]
    await admin.on_member_change(channel, "777", "2", "", "join")
    assert channel.sent[-1][1] == DEFAULT_WELCOME
    await ctx.shutdown()


async def test_greeting_respects_feature_and_sleep(mock_openai, tmp_path):
    ctx = await make_ctx(mock_openai, tmp_path)
    channel = AdminChannel()
    admin = ctx.service.groupadmin

    await ctx.perms_store.update_group("777", su_blocked={"迎新"})
    await admin.on_member_change(channel, "777", "2", "", "join")
    assert channel.sent == []

    await ctx.perms_store.update_group("888", active=False)
    await admin.on_member_change(channel, "888", "2", "", "join")
    assert channel.sent == []
    await ctx.shutdown()


async def test_recall_command(mock_openai, tmp_path):
    ctx = await make_ctx(mock_openai, tmp_path)
    service = ctx.service
    channel = AdminChannel()

    # 非超管（含群管理员）不能撤回：收到拒绝且未执行
    await service.handle(msg("撤回", role="admin", reply_to="42"), channel)
    assert len(channel.sent) == 1
    assert channel.recalled == []

    # 超管：无引用给提示，有引用执行撤回（带引用块的 text 按末行匹配）
    await service.handle(msg("撤回", user_id=SU), channel)
    assert "回复要撤回" in channel.sent[-1][1]
    quoted = "【被引用的消息|波奇】呜呜\n撤回"
    await service.handle(msg(quoted, user_id=SU, reply_to="42"), channel)
    assert channel.recalled == ["42"]
    # 成功时保持安静（消息消失即反馈），无新增回复
    sent_before = len(channel.sent)
    assert "撤回" not in channel.sent[-1][1] or sent_before == 2

    # 撤回失败给出提示
    fail_channel = AdminChannel(recall_ok=False)
    await service.handle(msg(quoted, user_id=SU, reply_to="43"), fail_channel)
    assert "只能撤回" in fail_channel.sent[-1][1]
    await ctx.shutdown()


async def test_onebot_member_change_dispatch(monkeypatch):
    """适配器：group_increase/decrease 事件解析与昵称获取。"""
    import asyncio

    from botcchi.channels.onebot.adapter import OneBotAdapter
    from botcchi.config import OneBotSettings

    events: list[tuple] = []

    async def on_change(group_id, user_id, nickname, change, operator_name):
        events.append((group_id, user_id, nickname, change, operator_name))

    adapter = OneBotAdapter(
        OneBotSettings(), on_message=None, on_member_change=on_change
    )  # type: ignore[arg-type]

    async def fake_call_api(action, params):
        if action == "get_stranger_info":
            return {"data": {"nickname": "退群的人"}}
        assert action == "get_group_member_info"
        return {"data": {"card": "踢人的管理", "nickname": "x"}}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)

    adapter._dispatch_notice(
        {"notice_type": "group_increase", "group_id": 777, "user_id": 2,
         "self_id": 999, "sub_type": "approve"}
    )
    adapter._dispatch_notice(
        {"notice_type": "group_decrease", "group_id": 777, "user_id": 3,
         "self_id": 999, "sub_type": "kick", "operator_id": 100}
    )
    # bot 自身进群不触发
    adapter._dispatch_notice(
        {"notice_type": "group_increase", "group_id": 777, "user_id": 999,
         "self_id": 999}
    )
    await asyncio.sleep(0.05)
    assert ("777", "2", "", "join", "") in events
    assert ("777", "3", "退群的人", "kick", "踢人的管理") in events
    assert len(events) == 2
