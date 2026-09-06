"""南京邮电大学校园地图查询工具"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .render import to_onebot_file
from .tools.registry import Tool, ToolContext, tool

MAP_ASSETS = Path(__file__).parent / "assets" / "maps"


@dataclass(frozen=True, slots=True)
class MapResult:
    location: str
    version: str | None
    path: Path


_MAPS: dict[str, dict[str, str] | str] = {
    "锁金村": "sjc.jpg",
    "三牌楼": {"新": "splnew.jpg", "旧": "splold.jpg"},
    "仙林": {
        "花": "xlflower.jpg",
        "新": "xlnew.jpg",
        "旧": "xlold.jpg",
        "洲": "xlzhou.jpg",
    },
}
_ALIASES = {"牌牌牌楼": "三牌楼", "小破邮": "仙林", "破邮": "仙林"}
_VERSION_ALIASES = {"-n": "新", "-o": "旧", "-f": "花", "-z": "洲"}
_DEFAULT_VERSION = "新"


class CampusMapService:
    def __init__(self, assets: Path = MAP_ASSETS):
        self.assets = assets

    def resolve(self, location: str, version: str = "") -> MapResult:
        location = _ALIASES.get(location.strip(), location.strip())
        if location not in _MAPS:
            choices = "、".join([*_MAPS, *_ALIASES])
            raise ValueError(f"未知地点「{location}」。可用地点：{choices}")

        config = _MAPS[location]
        version = _VERSION_ALIASES.get(version.strip(), version.strip())
        if isinstance(config, str):
            if version:
                raise ValueError(f"{location}地图无需指定版本")
            filename = config
            selected_version = None
        else:
            selected_version = version or _DEFAULT_VERSION
            if selected_version not in config:
                choices = "、".join(config)
                raise ValueError(
                    f"{location}没有「{selected_version}」版地图。可用版本：{choices}"
                )
            filename = config[selected_version]

        path = self.assets / filename
        if not path.is_file():
            raise FileNotFoundError(f"地图资源不存在：{filename}")
        return MapResult(location, selected_version, path)


def build_campus_map_tool(service: CampusMapService) -> Tool:
    @tool(
        name="get_campus_map",
        description=(
            "查询并发送南京邮电大学校园地图。location 支持「仙林」「三牌楼」"
            "「锁金村」，也支持别名「小破邮」「破邮」「牌牌牌楼」。version 可选："
            "仙林支持「新」「旧」「花」「洲」，三牌楼支持「新」「旧」，不填默认新版；"
            "锁金村不要传版本。用户询问南邮校区地图、校园地图或地点分布时调用。"
        ),
    )
    async def get_campus_map(
        ctx: ToolContext, location: str, version: str = ""
    ) -> str:
        try:
            result = service.resolve(location, version)
        except (ValueError, FileNotFoundError) as e:
            return str(e)

        try:
            image = await asyncio.to_thread(result.path.read_bytes)
            await ctx.channel.send_image(
                ctx.incoming.session, to_onebot_file(image)
            )
        except NotImplementedError:
            return "当前聊天渠道不支持发送图片"
        except Exception:
            logger.exception(f"校园地图发送失败: {result.path.name}")
            return "校园地图发送失败了，请告诉用户稍后再试"

        label = result.location
        if result.version is not None:
            label += f"（{result.version}版）"
        return f"已发送南邮{label}地图"

    return get_campus_map
