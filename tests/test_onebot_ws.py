"""OneBot 反向 WS 全链路：模拟 NapCat 连入 → 私聊消息 → 收到回复动作。"""

import json

import pytest
from conftest import text_chunks
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_service import make_settings

from botcchi.app import create_app


def test_ws_message_round_trip(mock_openai, tmp_path):
    mock_openai.enqueue(text_chunks("在的在的！"))
    app = create_app(make_settings(tmp_path), client=mock_openai.client())

    with TestClient(app) as client:
        with client.websocket_connect(
            "/onebot/v11/ws", headers={"x-self-id": "999"}
        ) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "message_id": 1,
                        "sender": {"nickname": "hhk"},
                        "message": [{"type": "text", "data": {"text": "在吗"}}],
                    }
                )
            )
            # 适配器应通过 WS 下发 send_private_msg 动作
            action = json.loads(ws.receive_text())
            assert action["action"] == "send_private_msg"
            assert action["params"]["user_id"] == 10001
            segments = action["params"]["message"]
            assert any(
                seg["type"] == "text" and "在的在的" in seg["data"]["text"]
                for seg in segments
            )
            # 回执 echo，完成 call_api 闭环
            ws.send_text(
                json.dumps({"status": "ok", "retcode": 0, "echo": action["echo"]})
            )


def test_ws_rejects_bad_token(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    settings.onebot.access_token = "secret"
    app = create_app(settings, client=mock_openai.client())

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/onebot/v11/ws",
                headers={"x-self-id": "999", "authorization": "Bearer wrong"},
            ) as ws:
                ws.receive_text()
