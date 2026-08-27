"""内容寻址的出图缓存：SVG 没变就复用上次的 PNG，免去重复渲染。

适用于商店/帮助菜单这类高频且内容基本不变的卡片：以 SVG 内容（+缩放）的
哈希为键，内存 LRU + 磁盘双层。内容一旦变化哈希随之变化，自动触发重渲染，
旧文件按 LRU 清理，无需手动失效。注意卡片内容里不要放时间戳这类每次都变的
元素，否则缓存永远不命中。

各功能用 scope() 分到自己的子目录（cards/help、cards/pig …），淘汰互不干扰
——小猪一次刷 60 张不会把帮助菜单挤掉，排查时也能一眼看出是谁的缓存。
"""

import hashlib
from collections import OrderedDict
from pathlib import Path

from loguru import logger

from .svg import SvgRenderer


class RenderCache:
    def __init__(self, cache_dir: str | Path, max_files: int = 64):
        self.dir = Path(cache_dir)
        self.max_files = max_files
        self._mem: OrderedDict[str, bytes] = OrderedDict()

    def scope(self, name: str, max_files: int | None = None) -> "RenderCache":
        """按功能分出的子缓存（如 scope("help") → cards/help）。"""
        return RenderCache(self.dir / name, max_files or self.max_files)

    def _key(self, svg: str, zoom: float | None) -> str:
        return hashlib.sha1(f"{zoom}:{svg}".encode()).hexdigest()

    async def render(
        self, renderer: SvgRenderer, svg: str, *, zoom: float | None = None
    ) -> bytes:
        key = self._key(svg, zoom)
        if (png := self._mem.get(key)) is not None:
            self._mem.move_to_end(key)
            return png
        file = self.dir / f"{key}.png"
        if file.exists():
            png = file.read_bytes()
        else:
            png = await renderer.render(svg, zoom=zoom)
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                file.write_bytes(png)
                self._prune_disk()
            except OSError as e:
                logger.warning(f"出图缓存写入失败（不影响发送）: {e}")
        self._mem[key] = png
        self._mem.move_to_end(key)
        while len(self._mem) > self.max_files:
            self._mem.popitem(last=False)
        return png

    def _prune_disk(self) -> None:
        files = sorted(
            self.dir.glob("*.png"), key=lambda f: f.stat().st_mtime
        )
        for old in files[: max(0, len(files) - self.max_files)]:
            old.unlink(missing_ok=True)
