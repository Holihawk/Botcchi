"""M2：数据库存储、伪人模式、长期记忆、人格绑定。"""

import pytest
from conftest import text_chunks
from test_service import FakeChannel, make_settings

from botcchi.agent.messages import assistant, user
from botcchi.app import build_context
from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import ProviderSettings, TriggerSettings
from botcchi.db.engine import Database
from botcchi.db.store import DBConversationStore, PersonaBindingStore
from botcchi.memory.long_term import _parse_facts
from botcchi.providers.registry import ProviderRegistry


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.init()
    yield database
    await database.aclose()


SESSION = SessionKey(channel="t", chat_type="private", chat_id="1")


# ---- 数据库存储 -----------------------------------------------------------


async def test_db_store_append_get_reset(db):
    store = DBConversationStore(db, max_messages=40)
    await store.append(SESSION, user("你好"), assistant("你好呀"))
    history = await store.get(SESSION)
    assert [(m.role, m.text()) for m in history] == [
        ("user", "你好"),
        ("assistant", "你好呀"),
    ]

    await store.reset(SESSION)
    assert await store.get(SESSION) == []

    # reset 后新消息正常进入
    await store.append(SESSION, user("新的开始"))
    assert [m.text() for m in await store.get(SESSION)] == ["新的开始"]


async def test_db_store_respects_max_messages(db):
    store = DBConversationStore(db, max_messages=4)
    for i in range(6):
        await store.append(SESSION, user(f"消息{i}"))
    history = await store.get(SESSION)
    assert [m.text() for m in history] == ["消息2", "消息3", "消息4", "消息5"]


async def test_persona_binding_upsert(db):
    bindings = PersonaBindingStore(db)
    assert await bindings.get(SESSION) is None
    await bindings.set(SESSION, "bocchi")
    assert await bindings.get(SESSION) == "bocchi"
    await bindings.set(SESSION, "kita")
    assert await bindings.get(SESSION) == "kita"
    await bindings.clear(SESSION)
    assert await bindings.get(SESSION) is None


# ---- provider 引用解析 ----------------------------------------------------


def test_resolve_model_without_prefix():
    registry = ProviderRegistry.from_settings(
        [
            ProviderSettings(
                name="relay",
                api_base="http://x/v1",
                api_keys=["k"],
                models=[{"name": "gpt-5.6-terra", "temperature": 0.8}],
            )
        ]
    )
    provider, model, detail = registry.resolve("gpt-5.6-terra")
    assert provider.name == "relay"
    assert model == "gpt-5.6-terra"
    assert detail.temperature == 0.8


# ---- 伪人模式 -------------------------------------------------------------


async def test_bym_reply_uses_group_window(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    settings.trigger = TriggerSettings(group_reply_rate=1.0)  # 必定触发
    ctx = build_context(settings, client=mock_openai.client())
    channel = FakeChannel()
    group = SessionKey(channel="fake", chat_type="group", chat_id="777")

    # 群里先积累两条没触发回复的消息不现实（rate=1 每条都回），直接依次发送
    mock_openai.enqueue(text_chunks("弹吉他很好玩的"))
    await ctx.service.handle(
        IncomingMessage(
            session=group, user_id="2", user_name="小虹", text="有人会乐器吗", at_me=False
        ),
        channel,
    )

    body = mock_openai.requests[0]
    user_content = body["messages"][-1]["content"]
    assert "群聊最近的消息" in user_content
    assert "小虹(2): 有人会乐器吗" in user_content
    assert channel.sent, "伪人模式应发出回复"

    # 机器人自己的回复也进入窗口，下一轮上下文可见
    window = await ctx.window.recent(group)
    assert [e.text for e in window][-1] == "弹吉他很好玩的"


async def test_wake_word_triggers_direct_reply(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    settings.trigger = TriggerSettings(group_reply_rate=0.0, wake_words=["波奇"])
    ctx = build_context(settings, client=mock_openai.client())
    channel = FakeChannel()
    group = SessionKey(channel="fake", chat_type="group", chat_id="777")

    # 不含唤醒词：忽略
    await ctx.service.handle(
        IncomingMessage(
            session=group, user_id="2", user_name="小虹", text="随便说说", at_me=False
        ),
        channel,
    )
    assert mock_openai.requests == []

    # 含唤醒词：触发，且视为直接对话（与 @ 同等，可用上下文工具）
    mock_openai.enqueue(text_chunks("叫我干嘛啦"))
    await ctx.service.handle(
        IncomingMessage(
            session=group, user_id="2", user_name="小虹", text="波奇在吗", at_me=False
        ),
        channel,
    )
    assert channel.sent


# ---- 长期记忆 -------------------------------------------------------------


def test_parse_facts_tolerates_code_fence():
    assert _parse_facts('["a", "b"]') == ["a", "b"]
    assert _parse_facts('```json\n["吉他手", "高中生"]\n```') == ["吉他手", "高中生"]
    assert _parse_facts("以下是提取结果：[\"喜欢猫\"]") == ["喜欢猫"]
    assert _parse_facts("[]") == []
    assert _parse_facts("没有值得记忆的内容") == []


async def test_memory_extract_and_recall(mock_openai, tmp_path, db):
    from botcchi.config import MemorySettings
    from botcchi.memory.long_term import MemoryService

    registry = ProviderRegistry.from_settings(
        [ProviderSettings(name="mock", api_base="http://mock/v1", api_keys=["k"])],
        client=mock_openai.client(),
    )
    memory = MemoryService(
        MemorySettings(extract_every=2, max_inject=5), db, registry, "mock/m"
    )
    incoming = IncomingMessage(
        session=SESSION, user_id="1", user_name="hhk", text="我养了只猫叫年糕"
    )

    mock_openai.enqueue(text_chunks('["hhk 养了一只叫年糕的猫"]'))
    added = await memory.extract(
        incoming,
        [user("我养了只猫叫年糕"), assistant("好可爱的名字呀")],
    )
    assert added == 1
    assert await memory.recall(incoming) == ["hhk 养了一只叫年糕的猫"]

    block = memory.render_block(await memory.recall(incoming))
    assert "你记得的事" in block and "年糕" in block

    # extract_every=2：第一轮 False，第二轮 True
    assert memory.should_extract(incoming) is False
    assert memory.should_extract(incoming) is True


async def test_memory_injected_into_system_prompt(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/svc.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.startup()
    channel = FakeChannel()

    # 预置一条记忆
    from botcchi.db.models import MemoryRow

    async with ctx.db.session() as s:
        s.add(MemoryRow(scope_key="user:10001", content="hhk 养了一只叫年糕的猫"))
        await s.commit()

    mock_openai.enqueue(text_chunks("年糕最近还好吗"))
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    await ctx.service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="早上好", at_me=True
        ),
        channel,
    )

    system_prompt = mock_openai.requests[0]["messages"][0]["content"]
    assert "你记得的事" in system_prompt and "年糕" in system_prompt
    await ctx.shutdown()


async def test_clear_memory_command(mock_openai, tmp_path):
    """私聊清本人记忆；群聊仅超管可清群记忆。"""
    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/svc.db"
    settings.onebot.superusers = ["900000"]
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.startup()
    channel = FakeChannel()

    from botcchi.db.models import MemoryRow

    async with ctx.db.session() as s:
        s.add(MemoryRow(scope_key="user:10001", content="hhk 养了一只叫年糕的猫"))
        s.add(MemoryRow(scope_key="group:777", content="这个群在合租服务器"))
        await s.commit()

    private = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    group = SessionKey(channel="fake", chat_type="group", chat_id="777")

    # 私聊：清掉本人维度记忆，群记忆不受影响
    await ctx.service.handle(
        IncomingMessage(
            session=private, user_id="10001", user_name="hhk",
            text="清除记忆", at_me=True,
        ),
        channel,
    )
    assert "1 条记忆" in channel.sent[-1][1]
    memory = ctx.service.memory
    incoming_group = IncomingMessage(
        session=group, user_id="10001", user_name="hhk", text="", at_me=True
    )
    assert await memory.recall(incoming_group) == ["这个群在合租服务器"]
    # 再清一次：已无记忆
    await ctx.service.handle(
        IncomingMessage(
            session=private, user_id="10001", user_name="hhk",
            text="清除记忆", at_me=True,
        ),
        channel,
    )
    assert "还没有" in channel.sent[-1][1]

    # 群聊：普通人（含群管理员）不能清，超管可清
    await ctx.service.handle(
        IncomingMessage(
            session=group, user_id="10001", user_name="hhk",
            text="清除记忆", at_me=True, sender_role="admin",
        ),
        channel,
    )
    assert "只有管理者" in channel.sent[-1][1]
    await ctx.service.handle(
        IncomingMessage(
            session=group, user_id="900000", user_name="su",
            text="清除记忆", at_me=True,
        ),
        channel,
    )
    assert "本群的 1 条记忆" in channel.sent[-1][1]
    assert await memory.recall(incoming_group) == []
    await ctx.shutdown()


# ---- 人格切换指令 ----------------------------------------------------------


async def test_persona_switch_command(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    (tmp_path / "kita.yaml").write_text(
        "name: kita\ndisplay_name: 喜多\nmodel: mock/m\nprompt: 你是喜多\n",
        encoding="utf-8",
    )
    ctx = build_context(settings, client=mock_openai.client())
    channel = FakeChannel()
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")

    def msg(text):
        return IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text=text, at_me=True
        )

    await ctx.service.handle(msg("人格列表"), channel)
    assert "kita" in channel.sent[-1][1] and "test" in channel.sent[-1][1]

    await ctx.service.handle(msg("切换人格 kita"), channel)
    assert "喜多" in channel.sent[-1][1]
    assert (await ctx.service.resolve_assistant(session)).name == "kita"

    # 切换后对话使用新人格的 prompt
    mock_openai.enqueue(text_chunks("我是喜多酱！"))
    await ctx.service.handle(msg("你是谁"), channel)
    assert mock_openai.requests[-1]["messages"][0]["content"].startswith("你是喜多")


async def test_group_persona_switch_requires_superuser(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    ctx = build_context(settings, client=mock_openai.client())
    channel = FakeChannel()
    group = SessionKey(channel="fake", chat_type="group", chat_id="777")

    await ctx.service.handle(
        IncomingMessage(
            session=group, user_id="10001", user_name="hhk",
            text="切换人格 test", at_me=True,
        ),
        channel,
    )
    assert "管理者" in channel.sent[-1][1]
