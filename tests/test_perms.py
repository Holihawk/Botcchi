"""权限体系：等级判定、封禁矩阵、群功能开关、管理指令与工具过滤。"""

from datetime import timedelta

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import PermsSettings
from botcchi.db.engine import Database
from botcchi.perms import (
    SUPERUSER_LEVEL,
    PermCommands,
    PermissionGate,
    PermStore,
    feature_for_command,
    tools_of,
)
from botcchi.perms.store import utcnow

SU = "900000"


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/perms.db")
    await database.init()
    yield database
    await database.aclose()


@pytest.fixture
def store(db):
    return PermStore(db)


@pytest.fixture
def gate(store):
    return PermissionGate(PermsSettings(), [SU], store)


def msg(
    user_id="10001",
    chat_type="group",
    chat_id="777",
    text="",
    role=None,
    mentions=None,
) -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type=chat_type, chat_id=chat_id),
        user_id=user_id,
        user_name=f"用户{user_id}",
        text=text,
        at_me=True,
        sender_role=role,
        mentions=mentions or [],
    )


# ---- 等级判定 -----------------------------------------------------------


async def test_effective_level_sources(gate, store):
    assert await gate.effective_level(SU) == SUPERUSER_LEVEL
    # 群内身份的隐式等级
    assert await gate.effective_level("1", "777", "owner") == 8
    assert await gate.effective_level("1", "777", "admin") == 5
    assert await gate.effective_level("1", "777", "member") == 0
    # 显式授予：全局与按群取 max，写入后缓存即时失效
    await store.set_level("1", None, 3)
    await store.set_level("1", "777", 6)
    assert await gate.effective_level("1", "777") == 6
    assert await gate.effective_level("1", "888") == 3
    assert await gate.effective_level("1", None) == 3
    # 隐式与显式取 max
    assert await gate.effective_level("1", "888", "owner") == 8


async def test_can_manage(gate, store):
    assert await gate.can_manage(msg(user_id=SU))
    assert await gate.can_manage(msg(role="admin"))
    assert not await gate.can_manage(msg())
    await store.set_level("10001", "777", 5)
    assert await gate.can_manage(msg())


# ---- 封禁矩阵 -----------------------------------------------------------


async def test_ban_matrix(gate, store):
    # 全局封禁：处处生效
    await store.add_ban("1", None, None, "", SU, True)
    assert await gate.is_banned("1", "777")
    assert await gate.is_banned("1", None)
    # 群内封禁：只在该群生效
    await store.add_ban("2", "777", None, "", "op", False)
    assert await gate.is_banned("2", "777")
    assert not await gate.is_banned("2", "888")
    assert not await gate.is_banned("2", None)
    # 封整群：群内所有人生效
    await store.add_ban(None, "666", None, "", SU, True)
    assert await gate.is_banned("3", "666")
    # 超管不可能被封
    await store.add_ban(SU, None, None, "", "x", False)
    assert not await gate.is_banned(SU, None)


async def test_ban_expiry_and_unban_rules(gate, store):
    await store.add_ban("1", "777", utcnow() - timedelta(minutes=1), "", "op", False)
    assert not await gate.is_banned("1", "777")  # 过期自动清理

    await store.add_ban("2", "777", None, "", SU, True)
    # 群管解不开超管下的封禁
    assert await store.remove_ban("2", "777", allow_su=False) is False
    assert await store.remove_ban("2", "777", allow_su=True) is True
    assert await store.remove_ban("2", "777", allow_su=True) is None  # 已不存在


# ---- 管理指令 -----------------------------------------------------------


async def test_ban_command_flow(gate, store):
    commands = PermCommands(gate)
    channel = FakeChannel()

    # 无权限用户被拒
    handled = await commands.handle(
        msg(text="封禁 @路人 3天", mentions=["2"]), channel
    )
    assert handled and "权限" in channel.sent[-1][1]
    assert not await gate.is_banned("2", "777")

    # 群管封禁成员（带时长与原因）
    await commands.handle(
        msg(user_id="100", role="admin", text="封禁 @路人 3天 刷屏", mentions=["2"]),
        channel,
    )
    assert "已本群封禁 2" in channel.sent[-1][1] and "3 天" in channel.sent[-1][1]
    assert await gate.is_banned("2", "777")
    (ban,) = await store.list_bans()
    assert ban.reason == "刷屏" and not ban.by_superuser

    # 平级/更高等级封不动；超管不可封
    await commands.handle(
        msg(user_id="100", role="admin", text="封禁 @群主", mentions=["101"]),
        channel,
    )
    await store.set_level("101", "777", 5)
    await commands.handle(
        msg(user_id="100", role="admin", text=f"封禁 {SU}"), channel
    )
    assert "不可以封禁管理者" in channel.sent[-1][1]

    # 群管解封自己封的
    await commands.handle(
        msg(user_id="100", role="admin", text="解封 @路人", mentions=["2"]), channel
    )
    assert "已解封 2" in channel.sent[-1][1]
    assert not await gate.is_banned("2", "777")

    # 超管封禁后群管解不开
    await commands.handle(msg(user_id=SU, text="封禁 @路人", mentions=["2"]), channel)
    await commands.handle(
        msg(user_id="100", role="admin", text="解封 @路人", mentions=["2"]), channel
    )
    assert "解不开" in channel.sent[-1][1]
    assert await gate.is_banned("2", "777")


async def test_grant_command_flow(gate, store):
    commands = PermCommands(gate)
    channel = FakeChannel()

    # 群管（等级5）只能授予低于自己的等级
    await commands.handle(
        msg(user_id="100", role="admin", text="授权 @小明 5", mentions=["2"]), channel
    )
    assert "低于自己等级" in channel.sent[-1][1]
    await commands.handle(
        msg(user_id="100", role="admin", text="授权 @小明 3", mentions=["2"]), channel
    )
    assert "已授予 2 本群权限等级 3" in channel.sent[-1][1]
    assert await gate.effective_level("2", "777") == 3

    # 私聊全局授权仅超管可用
    await commands.handle(
        msg(
            user_id=SU, chat_type="private", chat_id=SU,
            text="授权 @小明 7", mentions=["2"],
        ),
        channel,
    )
    assert "全局权限等级 7" in channel.sent[-1][1]
    assert await gate.effective_level("2", "888") == 7

    # 取消授权
    await commands.handle(
        msg(user_id=SU, text="取消授权 @小明", mentions=["2"]), channel
    )
    assert await gate.effective_level("2", "777") == 7  # 群授予已删，剩全局


async def test_feature_toggle_and_query_commands(gate, store):
    commands = PermCommands(gate)
    channel = FakeChannel()

    await commands.handle(
        msg(user_id="100", role="admin", text="关闭功能 签到"), channel
    )
    assert "已在本群关闭「签到」" in channel.sent[-1][1]
    assert await gate.feature_blocked("777", "签到")

    # 超管关闭的功能群管开不了
    await commands.handle(msg(user_id=SU, text="关闭功能 点赞"), channel)
    await commands.handle(
        msg(user_id="100", role="admin", text="开启功能 点赞"), channel
    )
    assert "做不了主" in channel.sent[-1][1]
    assert await gate.feature_blocked("777", "点赞")
    await commands.handle(msg(user_id=SU, text="开启功能 点赞"), channel)
    assert not await gate.feature_blocked("777", "点赞")

    # 功能列表与权限查询
    await commands.handle(msg(user_id="1", text="功能列表"), channel)
    assert "🚫 签到" in channel.sent[-1][1]
    await commands.handle(msg(user_id="100", role="owner", text="我的权限"), channel)
    assert "8（群主）" in channel.sent[-1][1]

    # 未知功能名给出可选列表
    await commands.handle(
        msg(user_id=SU, text="关闭功能 打劫"), channel
    )
    assert "没有叫「打劫」的功能" in channel.sent[-1][1]


# ---- 功能注册表与工具过滤 -----------------------------------------------


def test_feature_registry():
    assert feature_for_command("签到") == "签到"
    assert feature_for_command("好感度排行") == "签到"
    assert feature_for_command("日报定时 8") == "日报"
    assert feature_for_command("reset") is None
    assert tools_of({"签到", "点赞"}) == {
        "sign_in", "get_user_social", "send_like",
    }


async def test_disabled_tools_and_registry_refusal(gate, store):
    await store.update_group("777", su_blocked={"签到"})
    disabled = await gate.disabled_tools("777")
    assert disabled == {"sign_in", "get_user_social"}
    assert await gate.disabled_tools(None) == frozenset()

    from botcchi.agent.messages import ToolCall
    from botcchi.tools.registry import Tool, ToolContext, ToolRegistry

    registry = ToolRegistry()

    async def sign_in() -> str:
        return "signed"

    registry.register(Tool(sign_in, "sign_in", "签到", None))
    ctx = ToolContext(
        incoming=msg(), channel=FakeChannel(), disabled_tools=frozenset({"sign_in"})
    )
    result = await registry.execute(
        ToolCall(id="1", name="sign_in", arguments=""), ctx
    )
    assert "已被关闭" in result


# ---- ChatService 端到端 -------------------------------------------------


def _db_settings(tmp_path, superusers=(SU,)):
    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = list(superusers)
    return settings


async def test_banned_user_fully_ignored(mock_openai, tmp_path):
    from botcchi.app import build_context

    ctx = build_context(_db_settings(tmp_path), client=mock_openai.client())
    await ctx.db.init()
    await ctx.perms_store.add_ban("10001", None, None, "", SU, True)

    channel = FakeChannel()
    await ctx.service.handle(
        msg(user_id="10001", chat_type="private", chat_id="10001", text="在吗"),
        channel,
    )
    assert channel.sent == [] and mock_openai.requests == []
    await ctx.shutdown()


async def test_sleep_wake_end_to_end(mock_openai, tmp_path):
    from conftest import text_chunks

    from botcchi.app import build_context

    ctx = build_context(_db_settings(tmp_path), client=mock_openai.client())
    await ctx.db.init()
    channel = FakeChannel()

    await ctx.service.handle(msg(user_id="100", role="admin", text="休眠"), channel)
    assert "睡" in channel.sent[-1][1]
    # 休眠中：@ 也不回，指令也不响应
    await ctx.service.handle(msg(user_id="1", text="签到"), channel)
    await ctx.service.handle(msg(user_id="1", text="在吗"), channel)
    assert len(channel.sent) == 1 and mock_openai.requests == []
    # 无权限用户唤不醒
    await ctx.service.handle(msg(user_id="1", text="唤醒"), channel)
    assert "权限" in channel.sent[-1][1]
    await ctx.service.handle(msg(user_id="100", role="admin", text="唤醒"), channel)
    assert "醒" in channel.sent[-1][1]

    mock_openai.enqueue(text_chunks("在的！"))
    await ctx.service.handle(msg(user_id="1", text="在吗"), channel)
    assert channel.sent[-1][1] == "在的！"
    await ctx.shutdown()


async def test_blocked_feature_command_and_tool_schema(mock_openai, tmp_path):
    """关闭「签到」后：指令被拦、sign_in 不进 LLM 工具 schema。"""
    from conftest import text_chunks

    from botcchi.app import build_context

    settings = _db_settings(tmp_path)
    # 在 _db_settings（其内部 make_settings 会写 test.yaml）之后覆写，带上工具
    (tmp_path / "test.yaml").write_text(
        "name: test\n"
        "display_name: 波奇\n"
        "model: mock/m\n"
        "prompt: 你是波奇\n"
        "tools: [current_time, sign_in]\n"
        "policy:\n  split: false\n",
        encoding="utf-8",
    )
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    channel = FakeChannel()

    await ctx.service.handle(msg(user_id=SU, text="关闭功能 签到"), channel)
    await ctx.service.handle(msg(user_id="1", text="签到"), channel)
    assert "「签到」在本群已被关闭" in channel.sent[-1][1]

    mock_openai.enqueue(text_chunks("好哦"))
    await ctx.service.handle(msg(user_id="1", text="波奇在吗"), channel)
    tool_names = [
        t["function"]["name"] for t in mock_openai.requests[-1].get("tools", [])
    ]
    assert "current_time" in tool_names and "sign_in" not in tool_names

    # 私聊不受群开关影响
    mock_openai.enqueue(text_chunks("帮你签啦"))
    await ctx.service.handle(
        msg(user_id="1", chat_type="private", chat_id="1", text="在吗"), channel
    )
    tool_names = [
        t["function"]["name"] for t in mock_openai.requests[-1].get("tools", [])
    ]
    assert "sign_in" in tool_names
    await ctx.shutdown()


async def test_group_admin_can_use_manage_commands(mock_openai, tmp_path):
    """群管理员（OneBot role=admin）现在能用定时设置等管理指令。"""
    from botcchi.app import build_context
    from botcchi.summary import KIND_GROUP_SUMMARY

    ctx = build_context(_db_settings(tmp_path), client=mock_openai.client())
    await ctx.db.init()
    channel = FakeChannel()

    await ctx.service.handle(
        msg(user_id="100", role="admin", text="总结定时 22 50"), channel
    )
    assert "每天 22 点" in channel.sent[-1][1]
    assert len(await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY)) == 1
    # 普通成员仍然不行
    await ctx.service.handle(msg(user_id="1", text="总结定时取消"), channel)
    assert "只有管理者" in channel.sent[-1][1]
    await ctx.shutdown()


def test_perms_api_routes(mock_openai, tmp_path):
    from fastapi.testclient import TestClient

    from botcchi.app import create_app

    app = create_app(_db_settings(tmp_path), client=mock_openai.client())
    with TestClient(app) as client:
        features = client.get("/api/perms/features").json()
        assert {"key": "签到", "description": "签到、好感度与金币"} in features

        assert client.put(
            "/api/perms/levels", json={"user_id": "42", "level": 6}
        ).status_code == 200
        (grant,) = client.get("/api/perms/levels").json()
        assert grant["user_id"] == "42" and grant["level"] == 6
        assert grant["group_id"] is None

        assert client.put(
            "/api/perms/groups/777", json={"su_blocked": ["签到"], "active": False}
        ).status_code == 200
        (group,) = client.get("/api/perms/groups").json()
        assert group["su_blocked"] == ["签到"] and group["active"] is False

        assert client.get("/api/perms/bans").json() == []
        assert (
            client.delete(f"/api/perms/levels/{grant['id']}").status_code == 200
        )
        assert client.get("/api/perms/levels").json() == []
