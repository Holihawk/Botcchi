"""媒体下载管线：限额流式下载 + ffmpeg（m3u8 落盘 / 音视频合流）。

所有下载带硬性字节上限，超限立即中止，避免大文件拖垮内存与群发送。
"""

import asyncio
import tempfile
from pathlib import Path

import httpx
from loguru import logger

from .base import ParseError

DOWNLOAD_TIMEOUT = httpx.Timeout(15.0, read=120.0)
CHUNK = 64 * 1024


async def download_capped(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    max_bytes: int,
) -> bytes | None:
    """流式下载，超过 max_bytes 返回 None（不抛错，让上层退回摘要卡）"""
    buf = bytearray()
    async with client.stream(
        "GET", url, headers=headers, timeout=DOWNLOAD_TIMEOUT,
        follow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        declared = int(resp.headers.get("Content-Length") or 0)
        if declared > max_bytes:
            return None
        async for chunk in resp.aiter_bytes(CHUNK):
            buf += chunk
            if len(buf) > max_bytes:
                return None
    return bytes(buf)


async def _run_ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except TimeoutError:
        proc.kill()
        raise ParseError("ffmpeg 处理超时") from None
    except FileNotFoundError:
        raise ParseError("未安装 ffmpeg，无法处理该媒体") from None
    if proc.returncode != 0:
        raise ParseError(f"ffmpeg 失败: {stderr.decode(errors='replace')[:200]}")


async def fetch_m3u8(
    url: str, headers: dict[str, str], max_bytes: int
) -> bytes | None:
    """用 ffmpeg 拉取 m3u8 流并封装为 mp4；产物超限返回 None"""
    header_args: list[str] = []
    if headers:
        blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        header_args = ["-headers", blob]
    with tempfile.TemporaryDirectory(prefix="botcchi-m3u8-") as tmp:
        out = Path(tmp) / "out.mp4"
        await _run_ffmpeg(
            *header_args, "-i", url,
            "-c", "copy", "-bsf:a", "aac_adtstoasc", str(out),
        )
        data = out.read_bytes()
    if len(data) > max_bytes:
        logger.info(f"m3u8 产物超限（{len(data) // 1024 // 1024}MB），放弃发送")
        return None
    return data


async def merge_av(video: bytes, audio: bytes) -> bytes:
    """DASH 视频流 + 音频流合并为 mp4（B站 dash 场景备用）"""
    with tempfile.TemporaryDirectory(prefix="botcchi-merge-") as tmp:
        v, a, out = Path(tmp) / "v.m4s", Path(tmp) / "a.m4s", Path(tmp) / "out.mp4"
        v.write_bytes(video)
        a.write_bytes(audio)
        await _run_ffmpeg("-i", str(v), "-i", str(a), "-c", "copy", str(out))
        return out.read_bytes()
