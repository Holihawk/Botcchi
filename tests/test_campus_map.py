from pathlib import Path

import pytest
from test_service import FakeChannel

from botcchi.campus_map import CampusMapService, build_campus_map_tool
from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.tools.registry import ToolContext


class ImageChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[str] = []

    async def send_image(self, session, file):
        self.images.append(file)


def make_context(channel: FakeChannel) -> ToolContext:
    incoming = IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id="10001",
        user_name="hhk",
        text="南邮仙林地图",
    )
    return ToolContext(incoming=incoming, channel=channel)


def test_resolve_defaults_aliases_and_short_versions(tmp_path: Path):
    for name in ("sjc.jpg", "splnew.jpg", "xlflower.jpg"):
        (tmp_path / name).write_bytes(b"map")
    service = CampusMapService(tmp_path)

    result = service.resolve("锁金村")
    assert result.location == "锁金村" and result.version is None
    assert result.path.name == "sjc.jpg"

    result = service.resolve("牌牌牌楼")
    assert result.location == "三牌楼" and result.version == "新"
    assert result.path.name == "splnew.jpg"

    result = service.resolve("小破邮", "-f")
    assert result.location == "仙林" and result.version == "花"
    assert result.path.name == "xlflower.jpg"


def test_resolve_rejects_bad_location_and_version(tmp_path: Path):
    service = CampusMapService(tmp_path)
    with pytest.raises(ValueError, match="未知地点"):
        service.resolve("鼓楼")
    with pytest.raises(ValueError, match="无需指定版本"):
        service.resolve("锁金村", "新")
    with pytest.raises(ValueError, match="可用版本：新、旧"):
        service.resolve("三牌楼", "花")


async def test_campus_map_tool_sends_selected_image(tmp_path: Path):
    (tmp_path / "xlold.jpg").write_bytes(b"jpeg-data")
    tool = build_campus_map_tool(CampusMapService(tmp_path))
    channel = ImageChannel()

    out = await tool.execute(
        '{"location": "破邮", "version": "旧"}', make_context(channel)
    )

    assert out == "已发送南邮仙林（旧版）地图"
    assert channel.images == ["base64://anBlZy1kYXRh"]


async def test_campus_map_tool_returns_actionable_error(tmp_path: Path):
    tool = build_campus_map_tool(CampusMapService(tmp_path))
    out = await tool.execute(
        '{"location": "仙林", "version": "不存在"}',
        make_context(FakeChannel()),
    )
    assert "可用版本：花、新、旧、洲" in out
