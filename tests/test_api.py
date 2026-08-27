"""M3 后端 API：管理接口 + 网页对话 SSE。"""

import json

from conftest import text_chunks
from fastapi.testclient import TestClient
from test_service import make_settings

from botcchi.app import create_app


def make_client(mock_openai, tmp_path, with_db: bool = True) -> TestClient:
    settings = make_settings(tmp_path)
    if with_db:
        settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    return TestClient(create_app(settings, client=mock_openai.client()))


def parse_sse(body: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]


def test_status_and_lists(mock_openai, tmp_path):
    with make_client(mock_openai, tmp_path) as client:
        status = client.get("/api/status").json()
        assert status["default_assistant"] == "test"
        assert status["storage"] == "database"

        assistants = client.get("/api/assistants").json()
        assert assistants[0]["name"] == "test"
        assert assistants[0]["is_default"] is True

        providers = client.get("/api/providers").json()
        assert providers[0]["name"] == "mock"
        assert providers[0]["key_count"] == 1
        assert "api_keys" not in providers[0]  # 密钥不出网

        tools = client.get("/api/tools").json()
        assert any(t["name"] == "current_time" for t in tools)


def test_update_assistant_and_hot_reload(mock_openai, tmp_path):
    with make_client(mock_openai, tmp_path) as client:
        resp = client.put(
            "/api/assistants/test",
            json={"prompt": "你是新的波奇", "temperature": 0.5},
        )
        assert resp.status_code == 200
        assert resp.json()["prompt"] == "你是新的波奇"

        # 落盘生效
        assert "你是新的波奇" in (tmp_path / "test.yaml").read_text(encoding="utf-8")

        # 热更新生效：新对话使用新 prompt
        mock_openai.enqueue(text_chunks("好耶"))
        chat = client.post(
            "/api/chat", json={"conversation_id": "c1", "message": "你好"}
        )
        assert chat.status_code == 200
        assert mock_openai.requests[0]["messages"][0]["content"].startswith("你是新的波奇")

        # 无效模型引用被拒绝
        bad = client.put("/api/assistants/test", json={"model": "nope/x/y"})
        assert bad.status_code in (200, 422)  # 单 provider 时兜底成功，多 provider 则 422
        missing = client.put("/api/assistants/ghost", json={"prompt": "x"})
        assert missing.status_code == 404


def test_web_chat_sse_stream_and_persistence(mock_openai, tmp_path):
    with make_client(mock_openai, tmp_path) as client:
        mock_openai.enqueue(text_chunks("你好呀！", reasoning="用户在打招呼"))
        resp = client.post(
            "/api/chat", json={"conversation_id": "c42", "message": "你好"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = parse_sse(resp.text)
        types = [e["type"] for e in events]
        assert "reasoning" in types and "text" in types
        assert events[-1]["type"] == "done"
        assert events[-1]["reply"] == "你好呀！"

        # 历史持久化，可在管理台浏览
        convs = client.get("/api/conversations").json()
        assert convs[0]["session_key"] == "web:web:c42"
        msgs = client.get("/api/messages", params={"session": "web:web:c42"}).json()
        assert [m["role"] for m in msgs] == ["user", "assistant"]

        # 第二轮带上下文
        mock_openai.enqueue(text_chunks("记得哦"))
        client.post("/api/chat", json={"conversation_id": "c42", "message": "记住我"})
        roles = [m["role"] for m in mock_openai.requests[1]["messages"]]
        assert roles == ["system", "user", "assistant", "user"]

        # reset 后上下文清空
        client.post("/api/chat/reset", json={"conversation_id": "c42"})
        mock_openai.enqueue(text_chunks("我们认识吗"))
        client.post("/api/chat", json={"conversation_id": "c42", "message": "我是谁"})
        roles = [m["role"] for m in mock_openai.requests[2]["messages"]]
        assert roles == ["system", "user"]


def test_memory_endpoints(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    settings.memory.extract_every = 1  # 每轮都提取，便于测试
    app = create_app(settings, client=mock_openai.client())

    with TestClient(app) as client:
        # 一轮对话 + 一次记忆提取（第二个 mock 响应给提取器）
        mock_openai.enqueue(text_chunks("年糕好可爱"))
        mock_openai.enqueue(text_chunks('["用户养了一只叫年糕的猫"]'))
        client.post(
            "/api/chat",
            json={"conversation_id": "m1", "message": "我养了只猫叫年糕"},
        )

        # 等待 fire-and-forget 的提取任务完成
        import time

        for _ in range(50):
            scopes = client.get("/api/memories/scopes").json()
            if scopes:
                break
            time.sleep(0.05)
        assert scopes == [{"scope_key": "user:web-user", "count": 1}]

        entries = client.get(
            "/api/memories", params={"scope": "user:web-user"}
        ).json()
        assert entries[0]["content"] == "用户养了一只叫年糕的猫"

        client.delete(f"/api/memories/{entries[0]['id']}")
        assert client.get("/api/memories/scopes").json() == []


def test_conversations_require_db(mock_openai, tmp_path):
    with make_client(mock_openai, tmp_path, with_db=False) as client:
        assert client.get("/api/conversations").status_code == 409
        assert client.get("/api/memories/scopes").status_code == 409
