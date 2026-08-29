"""引用消息解析 / 输出清洗 / 模型降级链 / 人格与 Provider CRUD。"""

import pytest
from conftest import text_chunks
from fastapi.testclient import TestClient
from test_service import make_settings

from botcchi.agent.assistant import Assistant
from botcchi.agent.events import TurnComplete
from botcchi.agent.loop import AgentLoop
from botcchi.agent.messages import system, user
from botcchi.app import create_app
from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.channels.onebot.adapter import OneBotAdapter
from botcchi.config import OneBotSettings, ProviderSettings, load_settings
from botcchi.providers.base import ProviderError
from botcchi.providers.registry import ProviderRegistry
from botcchi.service import clean_reply
from botcchi.tools.registry import ToolRegistry

# ---- 输出清洗 -------------------------------------------------------------


def test_clean_reply_strips_mentions_and_prefix():
    assert clean_reply("@123456789 你好呀") == "你好呀"
    assert clean_reply("[波奇(10001)]: 我在呢") == "我在呢"
    assert clean_reply("@张三 @李四 别吵啦") == "别吵啦"
    assert clean_reply("你说的 @987654321 是谁") == "你说的 是谁"
    assert clean_reply("正常回复不受影响。") == "正常回复不受影响。"
    assert clean_reply("邮箱 a@b.com 保留") == "邮箱 a@b.com 保留"


# ---- 引用消息解析 ----------------------------------------------------------


def make_adapter(api_responses: dict[str, dict]) -> OneBotAdapter:
    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]

    async def fake_call_api(action, params):
        return api_responses[action]

    adapter.call_api = fake_call_api  # type: ignore[method-assign]
    return adapter


def quoted_event(reply_id: str = "555") -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123,
        "user_id": 456,
        "message_id": 1,
        "sender": {"nickname": "hhk"},
        "message": [
            {"type": "reply", "data": {"id": reply_id}},
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 这是什么"}},
        ],
    }


async def test_quoted_image_is_visible():
    adapter = make_adapter(
        {
            "get_msg": {
                "data": {
                    "sender": {"nickname": "小明"},
                    "message": [
                        {"type": "text", "data": {"text": "看这个"}},
                        {"type": "image", "data": {"url": "http://img/1.jpg"}},
                    ],
                }
            }
        }
    )
    incoming = adapter._parse_message_event(quoted_event(), self_id="999")
    assert incoming is not None
    assert incoming.reply_to_id == "555"
    assert incoming.at_me is True

    await adapter._resolve_quote(incoming)
    assert "【被引用的消息|小明】" in incoming.text
    assert "看这个" in incoming.text
    assert "这是什么" in incoming.text
    assert incoming.images == ["http://img/1.jpg"]


async def test_inbound_images_are_materialized_for_model():
    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]
    adapter._download_image = lambda url: _download_image_for_test(url)  # type: ignore[method-assign]
    incoming = IncomingMessage(
        session=SessionKey(channel="onebot", chat_type="group", chat_id="1"),
        user_id="2",
        user_name="u",
        text="看图",
        images=["https://qq.example/image.jpg"],
    )
    await adapter.prepare_incoming(incoming)
    assert incoming.images[0].startswith("data:image/jpeg;base64,")


async def _download_image_for_test(url: str) -> str:
    import base64

    return "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode()


async def test_unreachable_inbound_image_is_skipped():
    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]

    async def fail(_url: str) -> str:
        raise TimeoutError("image timeout")

    adapter._download_image = fail  # type: ignore[method-assign]
    incoming = IncomingMessage(
        session=SessionKey(channel="onebot", chat_type="group", chat_id="1"),
        user_id="2",
        user_name="u",
        text="【被引用的消息】[图片]\n评价一下",
        images=["https://qq.example/expired.jpg"],
    )
    await adapter.prepare_incoming(incoming)
    assert incoming.images == []
    assert "[图片]" in incoming.text


async def test_quoted_forward_records_are_visible():
    adapter = make_adapter(
        {
            "get_msg": {
                "data": {
                    "sender": {"nickname": "小明"},
                    "message": [{"type": "forward", "data": {"id": "fwd1"}}],
                }
            },
            "get_forward_msg": {
                "data": {
                    "messages": [
                        {
                            "sender": {"nickname": "张三"},
                            "message": [
                                {"type": "text", "data": {"text": "第一条"}}
                            ],
                        },
                        {
                            "sender": {"nickname": "李四"},
                            "content": [
                                {"type": "text", "data": {"text": "第二条"}},
                                {"type": "image", "data": {"url": "http://img/2.jpg"}},
                            ],
                        },
                    ]
                }
            },
        }
    )
    incoming = adapter._parse_message_event(quoted_event(), self_id="999")
    await adapter._resolve_quote(incoming)
    assert "[聊天记录开始]" in incoming.text
    assert "张三: 第一条" in incoming.text
    assert "李四: 第二条[图片]" in incoming.text
    assert incoming.images == ["http://img/2.jpg"]


async def test_quoted_nested_forward_expands():
    """转发里再套转发：内层记录随消息带出（content），应递归展开而非占位。"""
    adapter = make_adapter(
        {
            "get_msg": {
                "data": {
                    "sender": {"nickname": "小明"},
                    "message": [{"type": "forward", "data": {"id": "outer"}}],
                }
            },
            "get_forward_msg": {
                "data": {
                    "messages": [
                        {
                            "sender": {"nickname": "张三"},
                            "message": [
                                {"type": "text", "data": {"text": "看聊天记录"}},
                                {
                                    "type": "forward",
                                    "data": {
                                        "id": "inner",
                                        "content": [
                                            {
                                                "sender": {"nickname": "王五"},
                                                "message": [
                                                    {"type": "text",
                                                     "data": {"text": "内层内容"}},
                                                    {"type": "image",
                                                     "data": {"url": "http://img/3.jpg"}},
                                                ],
                                            }
                                        ],
                                    },
                                },
                            ],
                        }
                    ]
                }
            },
        }
    )
    incoming = adapter._parse_message_event(quoted_event(), self_id="999")
    await adapter._resolve_quote(incoming)
    assert "张三: 看聊天记录" in incoming.text
    assert "王五: 内层内容[图片]" in incoming.text  # 内层被展开
    assert incoming.text.count("[聊天记录开始]") == 2  # 外层 + 内层各一
    assert "http://img/3.jpg" in incoming.images


async def test_nested_forward_depth_limit():
    """无限自嵌套时到达深度上限就停下，用占位符收尾，不会爆栈。"""
    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]

    # 每次拉取都返回「一条文本 + 一个只带 id 的嵌套转发」，让适配器自己一层层钻
    async def fake_call_api(action, params):
        return {
            "data": {
                "messages": [
                    {
                        "sender": {"nickname": "套娃"},
                        "message": [
                            {"type": "text", "data": {"text": "再往里"}},
                            {"type": "forward", "data": {"id": "x"}},
                        ],
                    }
                ]
            }
        }

    adapter.call_api = fake_call_api  # type: ignore[method-assign]
    text, _ = await adapter._resolve_forward("root")
    assert "[更深层的聊天记录]" in text
    # 展开层数受 MAX_FORWARD_DEPTH 限制
    assert text.count("[聊天记录开始]") == adapter.MAX_FORWARD_DEPTH


# ---- 模型降级链 ------------------------------------------------------------


def two_provider_loop(mock) -> AgentLoop:
    registry = ProviderRegistry.from_settings(
        [
            ProviderSettings(name="primary", api_base="http://mock/v1", api_keys=["a"]),
            ProviderSettings(name="backup", api_base="http://mock/v1", api_keys=["b"]),
        ],
        client=mock.client(),
    )
    return AgentLoop(registry, ToolRegistry())


async def test_fallback_to_next_model(mock_openai):
    mock_openai.enqueue_status(500)
    mock_openai.enqueue(text_chunks("备用模型顶上"))
    loop = two_provider_loop(mock_openai)
    assistant = Assistant(
        name="t",
        prompt="p",
        model="primary/m1",
        fallback_models=["backup/m2"],
    )

    events = [e async for e in loop.run(assistant, [system("p"), user("hi")])]

    assert isinstance(events[-1], TurnComplete)
    assert events[-1].message.text() == "备用模型顶上"
    assert mock_openai.requests[0]["model"] == "m1"
    assert mock_openai.requests[1]["model"] == "m2"


async def test_all_models_fail_raises(mock_openai):
    mock_openai.enqueue_status(500)
    mock_openai.enqueue_status(500)
    loop = two_provider_loop(mock_openai)
    assistant = Assistant(
        name="t", prompt="p", model="primary/m1", fallback_models=["backup/m2"]
    )
    with pytest.raises(ProviderError):
        async for _ in loop.run(assistant, [system("p"), user("hi")]):
            pass


# ---- 人格 CRUD -------------------------------------------------------------


def test_assistant_create_and_delete(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, client=mock_openai.client())) as client:
        resp = client.post(
            "/api/assistants",
            json={"name": "kita", "display_name": "喜多", "model": "mock/m"},
        )
        assert resp.status_code == 201
        assert (tmp_path / "kita.yaml").exists()
        assert (tmp_path / "kita.prompt.md").exists()

        names = [a["name"] for a in client.get("/api/assistants").json()]
        assert set(names) == {"test", "kita"}

        # 非法名 / 重名 / 无效模型
        assert client.post(
            "/api/assistants", json={"name": "大写", "model": "mock/m"}
        ).status_code == 422
        assert client.post(
            "/api/assistants", json={"name": "kita", "model": "mock/m"}
        ).status_code == 409
        assert client.post(
            "/api/assistants", json={"name": "x1", "model": "ghost/m"}
        ).status_code == 422

        # 默认人格不可删；其余可删
        assert client.delete("/api/assistants/test").status_code == 409
        assert client.delete("/api/assistants/kita").status_code == 200
        assert not (tmp_path / "kita.yaml").exists()
        assert not (tmp_path / "kita.prompt.md").exists()


# ---- Provider CRUD ---------------------------------------------------------


def test_provider_crud_and_hot_reload(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    settings.providers_file = tmp_path / "providers.yaml"
    with TestClient(create_app(settings, client=mock_openai.client())) as client:
        # 新增 provider
        resp = client.post(
            "/api/providers",
            json={
                "name": "backup",
                "api_base": "http://mock/v1",
                "api_keys": ["sk-backup-1234567890"],
                "models": [{"name": "m2", "temperature": 0.5}],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["key_masks"] == ["sk-bac…7890"]
        assert "api_keys" not in resp.json()

        # 持久化到 providers.yaml
        assert settings.providers_file.exists()
        stored = settings.providers_file.read_text(encoding="utf-8")
        assert "backup" in stored and "sk-backup-1234567890" in stored

        # 热重载生效：人格立即可以引用新 provider 的模型
        resp = client.put(
            "/api/assistants/test", json={"fallback_models": ["backup/m2"]}
        )
        assert resp.status_code == 200

        # key 管理：加一个、按索引删一个
        resp = client.put(
            "/api/providers/backup",
            json={"add_keys": ["sk-second-abcdefghijk"]},
        )
        assert resp.json()["key_count"] == 2
        resp = client.put(
            "/api/providers/backup", json={"remove_key_indexes": [0]}
        )
        assert resp.json()["key_masks"] == ["sk-sec…hijk"]
        assert client.put(
            "/api/providers/backup", json={"remove_key_indexes": [0]}
        ).status_code == 422  # 不能删光

        # 被人格引用时禁止删除
        assert client.delete("/api/providers/backup").status_code == 409
        client.put("/api/assistants/test", json={"fallback_models": []})

        # 排序（优先级）
        resp = client.post(
            "/api/providers/reorder", json={"names": ["backup", "mock"]}
        )
        assert [p["name"] for p in resp.json()] == ["backup", "mock"]

        # 解除引用后可删除
        assert client.delete("/api/providers/backup").status_code == 200
        assert client.delete("/api/providers/mock").status_code == 409  # 最后一个


def test_providers_file_overrides_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "providers:\n"
        "  - name: old\n"
        "    api_base: http://old/v1\n"
        "    api_keys: [k1]\n"
        f"providers_file: {tmp_path}/providers.yaml\n"
        f"assistants_dir: {tmp_path}\n",
        encoding="utf-8",
    )
    (tmp_path / "providers.yaml").write_text(
        "providers:\n"
        "  - name: managed\n"
        "    api_base: http://managed/v1\n"
        "    api_keys: [k2]\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert [p.name for p in settings.providers] == ["managed"]
