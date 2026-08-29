"""
OneBot v11 反向 WebSocket 适配器
NapCat / Lagrange 作为客户端连入本服务（协议面比较小）
"""

import asyncio
import base64
import json
import mimetypes
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from ...config import OneBotSettings
from ..base import Channel, IncomingMessage, IncomingRequest, SessionKey

API_TIMEOUT = 30.0
FRIEND_CACHE_TTL = 300.0
IMAGE_FETCH_TIMEOUT = 15.0
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 项目临时资源目录；部署时工作目录为项目根目录。
IMAGE_TEMP_DIR = Path("data/temp/quote-images")

MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
RequestHandler = Callable[[IncomingRequest], Awaitable[None]]
"""好友申请/拉群邀请等请求事件的回调（超管审批队列）"""
ProfileLikeHandler = Callable[[str, str, int], Awaitable[None]]
"""(user_id, nickname, times)：用户给 bot 资料卡点赞的通知回调"""
MemberChangeHandler = Callable[[str, str, str, str, str], Awaitable[None]]
"""(group_id, user_id, nickname, change, operator_name)：群成员变动回调，
change 为 join / leave / kick；nickname 仅退群场景尽力获取，
operator_name 仅被踢场景为踢人的管理员名字（拿不到则为空）"""


def _extract_card_url(raw: str) -> str | None:
    """从 json 卡片段提取跳转 URL（B站/公众号等分享多为小程序卡片）

    卡片结构：raw JSON 的 meta 下挂 detail_1.qqdocurl（小程序）或
    news/music.jumpUrl（图文/音乐卡）
    """
    try:
        meta = (json.loads(raw) or {}).get("meta") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    for value in meta.values():
        if isinstance(value, dict):
            if url := value.get("qqdocurl") or value.get("jumpUrl"):
                return str(url)
    return None


class OneBotAdapter(Channel):
    name = "onebot"

    def __init__(
        self,
        settings: OneBotSettings,
        on_message: MessageHandler,
        on_profile_like: ProfileLikeHandler | None = None,
        on_member_change: MemberChangeHandler | None = None,
        on_request: RequestHandler | None = None,
    ):
        self.settings = settings
        self.on_message = on_message
        self.on_profile_like = on_profile_like
        self.on_member_change = on_member_change
        self.on_request = on_request
        self._connections: dict[str, WebSocket] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._friends: set[str] | None = None
        self._friends_at: float = 0.0

    @property
    def is_connected(self) -> bool:
        return bool(self._connections)

    @property
    def router(self) -> APIRouter:
        router = APIRouter()
        router.add_api_websocket_route(self.settings.path, self._handle_ws)
        return router

    # ---- 连接与事件接收 -------------------------------------------------

    async def _handle_ws(self, ws: WebSocket) -> None:
        if not self._authorized(ws):
            await ws.close(code=1008)
            return
        self_id = ws.headers.get("x-self-id", "")
        await ws.accept()
        self._connections[self_id] = ws
        logger.info(f"OneBot 已连接: self_id={self_id}")
        try:
            while True:
                data = json.loads(await ws.receive_text())
                self._dispatch(data, self_id)
        except WebSocketDisconnect:
            logger.warning(f"OneBot 连接断开: self_id={self_id}")
        finally:
            self._connections.pop(self_id, None)

    def _authorized(self, ws: WebSocket) -> bool:
        token = self.settings.access_token
        if not token:
            return True
        auth = ws.headers.get("authorization", "")
        return auth in (f"Bearer {token}", f"Token {token}")

    def _dispatch(self, data: dict[str, Any], self_id: str) -> None:
        if "echo" in data:
            future = self._pending.pop(str(data["echo"]), None)
            if future and not future.done():
                future.set_result(data)
            return
        if data.get("post_type") == "notice":
            self._dispatch_notice(data)
            return
        if data.get("post_type") == "request":
            self._dispatch_request(data)
            return
        if data.get("post_type") not in ("message", "meta_event"):
            # 协议端各家事件不尽相同，丢弃前留个记录，免得"什么都没发生"无从查起
            logger.debug(f"未处理的事件: {data}")
            return
        if data.get("post_type") != "message":
            return
        incoming = self._parse_message_event(data, self_id)
        if incoming is None:
            return
        task = asyncio.create_task(self._safe_handle(incoming))
        task.add_done_callback(lambda t: t.exception())

    def _dispatch_notice(self, data: dict[str, Any]) -> None:
        """通知事件：资料卡点赞、群成员增减。"""
        notice_type = data.get("notice_type")
        if (
            self.on_profile_like is not None
            and notice_type == "notify"
            and data.get("sub_type") == "profile_like"
        ):
            user_id = str(data.get("operator_id") or "")
            if not user_id:
                return
            nickname = str(data.get("operator_nick") or user_id)
            times = int(data.get("times") or 1)
            task = asyncio.create_task(
                self._safe_profile_like(user_id, nickname, times)
            )
            task.add_done_callback(lambda t: t.exception())
            return
        if (
            self.on_member_change is not None
            and notice_type in ("group_increase", "group_decrease")
        ):
            user_id = str(data.get("user_id") or "")
            group_id = str(data.get("group_id") or "")
            # bot 自身进退群不通知（kick_me 亦然）
            if not user_id or not group_id or user_id == str(data.get("self_id", "")):
                return
            if notice_type == "group_increase":
                change = "join"
            elif data.get("sub_type") == "kick":
                change = "kick"
            else:
                change = "leave"
            operator_id = str(data.get("operator_id") or "")
            task = asyncio.create_task(
                self._safe_member_change(group_id, user_id, change, operator_id)
            )
            task.add_done_callback(lambda t: t.exception())
            return
        logger.debug(f"未处理的通知事件: {data}")

    def _dispatch_request(self, data: dict[str, Any]) -> None:
        """请求事件：好友申请 / 拉群邀请（不自动同意，进超管审批队列）

        入队失败等于超管永远收不到申请，所以每一条都记日志——收到记 INFO
        被丢弃记 WARNING 并带上原始事件，方便对照协议端实际字段
        """
        kind = str(data.get("request_type") or "")
        flag = str(data.get("flag") or "")
        user_id = str(data.get("user_id") or "")
        logger.info(
            f"收到请求事件: {kind}/{data.get('sub_type') or '-'} "
            f"user={user_id} flag={'有' if flag else '无'}"
        )
        if self.on_request is None:
            logger.warning("未接入请求处理器，申请被丢弃")
            return
        if kind not in ("friend", "group") or not flag or not user_id:
            logger.warning(f"请求事件字段不完整，已丢弃: {data}")
            return
        req = IncomingRequest(
            kind=kind,
            flag=flag,
            user_id=user_id,
            sub_type=str(data.get("sub_type") or ""),
            group_id=str(data.get("group_id") or ""),
            comment=str(data.get("comment") or "").strip(),
        )
        task = asyncio.create_task(self._safe_request(req))
        task.add_done_callback(lambda t: t.exception())

    async def _safe_request(self, req: IncomingRequest) -> None:
        try:
            # 昵称/群名尽力补全，拿不到不影响入队
            try:
                resp = await self.call_api(
                    "get_stranger_info", {"user_id": int(req.user_id)}
                )
                req.user_name = str(
                    (resp.get("data") or {}).get("nickname") or ""
                )
            except Exception:
                pass
            if req.group_id:
                try:
                    resp = await self.call_api(
                        "get_group_info", {"group_id": int(req.group_id)}
                    )
                    req.group_name = str(
                        (resp.get("data") or {}).get("group_name") or ""
                    )
                except Exception:
                    pass
            assert self.on_request is not None
            await self.on_request(req)
        except Exception:
            logger.exception(f"处理请求事件失败: {req.kind}/{req.user_id}")

    async def _safe_member_change(
        self, group_id: str, user_id: str, change: str, operator_id: str
    ) -> None:
        try:
            nickname = ""
            if change != "join":
                # 人已退群，昵称只能从陌生人资料尽力获取
                try:
                    resp = await self.call_api(
                        "get_stranger_info", {"user_id": int(user_id)}
                    )
                    nickname = str((resp.get("data") or {}).get("nickname") or "")
                except Exception:
                    pass
            operator_name = ""
            if change == "kick" and operator_id not in ("", "0", user_id):
                # 踢人的管理员还在群里，取群名片
                try:
                    resp = await self.call_api(
                        "get_group_member_info",
                        {"group_id": int(group_id), "user_id": int(operator_id)},
                    )
                    info = resp.get("data") or {}
                    operator_name = str(
                        info.get("card") or info.get("nickname") or ""
                    )
                except Exception:
                    pass
            assert self.on_member_change is not None
            await self.on_member_change(
                group_id, user_id, nickname, change, operator_name
            )
        except Exception:
            logger.exception(f"处理群成员变动失败: {group_id}/{user_id}")

    async def _safe_profile_like(
        self, user_id: str, nickname: str, times: int
    ) -> None:
        try:
            assert self.on_profile_like is not None
            await self.on_profile_like(user_id, nickname, times)
        except Exception:
            logger.exception(f"处理资料卡点赞失败: {user_id}")

    async def _safe_handle(self, incoming: IncomingMessage) -> None:
        try:
            if incoming.reply_to_id:
                await self._resolve_quote(incoming)
            await self.on_message(incoming)
        except Exception:
            logger.exception(f"处理消息失败: {incoming.session}")

    def _parse_message_event(
        self, event: dict[str, Any], self_id: str
    ) -> IncomingMessage | None:
        message_type = event.get("message_type")
        if message_type == "group":
            chat_id = str(event.get("group_id", ""))
            session = SessionKey(channel=self.name, chat_type="group", chat_id=chat_id)
        elif message_type == "private":
            chat_id = str(event.get("user_id", ""))
            session = SessionKey(channel=self.name, chat_type="private", chat_id=chat_id)
        else:
            return None

        texts: list[str] = []
        images: list[str] = []
        image_keys: list[str] = []
        mentions: list[str] = []
        at_me = False
        reply_to_id: str | None = None
        segments = event.get("message") or []
        if isinstance(segments, str):
            # message_format=string 时退化处理：仅取原文，不解析 CQ 码
            segments = [{"type": "text", "data": {"text": segments}}]
        for seg in segments:
            seg_type = seg.get("type")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                texts.append(seg_data.get("text", ""))
            elif seg_type == "at":
                if str(seg_data.get("qq", "")) == self_id:
                    at_me = True
                else:
                    name = seg_data.get("name") or seg_data.get("qq", "")
                    texts.append(f"@{name}")
                    if str(seg_data.get("qq", "")).isdigit():
                        mentions.append(str(seg_data["qq"]))
            elif seg_type == "image" and seg_data.get("url"):
                images.append(seg_data["url"])
                image_keys.append(str(seg_data.get("file") or seg_data["url"]))
            elif seg_type == "reply" and seg_data.get("id"):
                reply_to_id = str(seg_data["id"])
            elif seg_type == "json":
                # 小程序/结构化分享卡片：提取跳转链接供链接解析与 AI 感知
                if url := _extract_card_url(seg_data.get("data") or ""):
                    texts.append(f" {url} ")

        sender = event.get("sender") or {}
        user_id = str(event.get("user_id", ""))
        return IncomingMessage(
            session=session,
            user_id=user_id,
            user_name=sender.get("card") or sender.get("nickname") or user_id,
            text="".join(texts).strip(),
            images=images,
            image_keys=image_keys,
            at_me=at_me or message_type == "private",
            sender_role=sender.get("role") if message_type == "group" else None,
            mentions=mentions,
            message_id=str(event.get("message_id", "")) or None,
            self_id=self_id,
            reply_to_id=reply_to_id,
            raw=event,
        )

    # ---- 引用消息解析 ---------------------------------------------------

    MAX_QUOTE_IMAGES = 4
    MAX_FORWARD_RECORDS = 30
    MAX_FORWARD_DEPTH = 3
    """合并转发最多向内展开几层，兜底防深度嵌套的恶意消息把上下文撑爆"""

    async def _resolve_quote(self, incoming: IncomingMessage) -> None:
        """把被引用消息的内容（文本/图片/合并转发记录）并入当前消息。"""
        try:
            resp = await self.call_api(
                "get_msg", {"message_id": incoming.reply_to_id}
            )
        except Exception as e:
            logger.warning(f"获取被引用消息失败: {e}")
            return
        data = resp.get("data") or {}
        sender = data.get("sender") or {}
        nick = sender.get("card") or sender.get("nickname") or "对方"
        text, images = await self._flatten_segments(data.get("message") or [])
        quote_block = f"【被引用的消息|{nick}】{text or '（无文本）'}"
        incoming.text = f"{quote_block}\n{incoming.text}".strip()
        for url in images:
            if len(incoming.images) >= self.MAX_QUOTE_IMAGES:
                break
            incoming.images.append(url)

    async def prepare_incoming(self, incoming: IncomingMessage) -> None:
        """下载入站图片并转成 data URL，避免模型服务访问 QQ 临时链接。"""
        if not incoming.images:
            return
        materialized: list[str] = []
        for url in incoming.images:
            if url.startswith("data:image/"):
                materialized.append(url)
                continue
            try:
                materialized.append(await self._download_image(url))
            except Exception as e:
                logger.warning(f"图片本地化失败，跳过该图片: {e}")
        incoming.images = materialized

    async def _download_image(self, url: str) -> str:
        """图片先落盘到临时目录，再编码为模型可直接读取的 data URL。"""
        await asyncio.to_thread(IMAGE_TEMP_DIR.mkdir, parents=True, exist_ok=True)
        timeout = httpx.Timeout(IMAGE_FETCH_TIMEOUT)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    raise ValueError(f"响应不是图片（{content_type or '未知类型'}）")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > MAX_IMAGE_BYTES:
                    raise ValueError(f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ValueError(f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB")
                    chunks.append(chunk)
        data = b"".join(chunks)
        suffix = mimetypes.guess_extension(content_type) or ".img"
        path = IMAGE_TEMP_DIR / f"{uuid.uuid4().hex}{suffix}"
        try:
            await asyncio.to_thread(path.write_bytes, data)
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
        finally:
            await asyncio.to_thread(path.unlink, missing_ok=True)

    async def _flatten_segments(
        self, segments: list[dict[str, Any]] | str
    ) -> tuple[str, list[str]]:
        """把消息段落展平为文本 + 图片 url 列表（合并转发展开一层）"""
        if isinstance(segments, str):
            return segments, []
        texts: list[str] = []
        images: list[str] = []
        for seg in segments:
            seg_type = seg.get("type")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                texts.append(seg_data.get("text", ""))
            elif seg_type == "image":
                if url := seg_data.get("url"):
                    images.append(url)
                texts.append("[图片]")
            elif seg_type == "at":
                texts.append(f"@{seg_data.get('name') or seg_data.get('qq', '')}")
            elif seg_type == "forward":
                forward_text, forward_images = await self._resolve_forward(
                    seg_data.get("id") or seg_data.get("message_id")
                )
                texts.append(forward_text)
                images.extend(forward_images)
        return "".join(texts).strip(), images

    async def _resolve_forward(
        self,
        forward_id: Any,
        *,
        depth: int = 0,
        inline: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[str]]:
        """展开合并转发的聊天记录，嵌套的转发向内递归（限深）。

        inline 是协议端已随消息带出的嵌套记录（NapCat 的 forward.content），
        有就直接用、省一次 API 调用；没有才按 id 拉取。
        """
        records = inline
        if records is None:
            if not forward_id:
                return "[聊天记录]", []
            try:
                resp = await self.call_api(
                    "get_forward_msg", {"id": str(forward_id)}
                )
            except Exception as e:
                logger.warning(f"获取合并转发失败: {e}")
                return "[聊天记录]", []
            data = resp.get("data") or {}
            records = data.get("messages") or data.get("message") or []
        lines, images = await self._forward_lines(records, depth)
        body = "\n".join(lines)
        return f"[聊天记录开始]\n{body}\n[聊天记录结束]", images

    async def _forward_lines(
        self, records: list[dict[str, Any]], depth: int
    ) -> tuple[list[str], list[str]]:
        """把合并转发的记录列表渲染成「昵称: 内容」行 + 收集图片"""
        lines: list[str] = []
        images: list[str] = []
        for rec in records[: self.MAX_FORWARD_RECORDS]:
            sender = rec.get("sender") or {}
            nick = sender.get("card") or sender.get("nickname") or "?"
            segs = rec.get("message") or rec.get("content") or []
            rec_texts: list[str] = []
            if isinstance(segs, str):
                rec_texts.append(segs)
            else:
                for seg in segs:
                    seg_type = seg.get("type")
                    seg_data = seg.get("data") or {}
                    if seg_type == "text":
                        rec_texts.append(seg_data.get("text", ""))
                    elif seg_type == "image":
                        if url := seg_data.get("url"):
                            images.append(url)
                        rec_texts.append("[图片]")
                    elif seg_type == "at":
                        rec_texts.append(
                            f"@{seg_data.get('name') or seg_data.get('qq', '')}"
                        )
                    elif seg_type == "forward":
                        if depth + 1 >= self.MAX_FORWARD_DEPTH:
                            rec_texts.append("[更深层的聊天记录]")
                            continue
                        nested_text, nested_images = await self._resolve_forward(
                            seg_data.get("id") or seg_data.get("message_id"),
                            depth=depth + 1,
                            inline=seg_data.get("content"),
                        )
                        rec_texts.append(nested_text)
                        images.extend(nested_images)
            lines.append(f"{nick}: {''.join(rec_texts).strip()}")
        if len(records) > self.MAX_FORWARD_RECORDS:
            lines.append(
                f"…（共 {len(records)} 条，仅展示前 {self.MAX_FORWARD_RECORDS} 条）"
            )
        return lines, images

    # ---- API 调用与发送 -------------------------------------------------

    async def call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ws = next(iter(self._connections.values()), None)
        if ws is None:
            raise RuntimeError("没有可用的 OneBot 连接")
        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await ws.send_text(
                json.dumps(
                    {"action": action, "params": params, "echo": echo},
                    ensure_ascii=False,
                )
            )
            return await asyncio.wait_for(future, timeout=API_TIMEOUT)
        finally:
            self._pending.pop(echo, None)

    @staticmethod
    def _sent_message_id(resp: dict[str, Any]) -> str | None:
        mid = (resp.get("data") or {}).get("message_id")
        return str(mid) if mid is not None else None

    async def send_text(
        self,
        session: SessionKey,
        text: str,
        *,
        reply_to: str | None = None,
        at_user: str | None = None,
    ) -> str | None:
        message: list[dict[str, Any]] = []
        if reply_to:
            message.append({"type": "reply", "data": {"id": reply_to}})
        if at_user and session.chat_type == "group":
            message.append({"type": "at", "data": {"qq": at_user}})
            text = " " + text
        message.append({"type": "text", "data": {"text": text}})
        if session.chat_type == "group":
            params: dict[str, Any] = {"group_id": int(session.chat_id), "message": message}
            resp = await self.call_api("send_group_msg", params)
        else:
            params = {"user_id": int(session.chat_id), "message": message}
            resp = await self.call_api("send_private_msg", params)
        return self._sent_message_id(resp)

    async def poke(self, session: SessionKey, user_id: str) -> bool:
        """戳一戳（NapCat 扩展 API：group_poke / friend_poke）。"""
        try:
            if session.chat_type == "group":
                resp = await self.call_api(
                    "group_poke",
                    {"group_id": int(session.chat_id), "user_id": int(user_id)},
                )
            else:
                resp = await self.call_api(
                    "friend_poke", {"user_id": int(user_id)}
                )
        except Exception as e:
            logger.warning(f"戳一戳失败 {user_id}: {e}")
            return False
        return resp.get("status") == "ok" or resp.get("retcode") == 0

    async def send_music(
        self, session: SessionKey, platform: str, music_id: str
    ) -> None:
        message = [
            {"type": "music", "data": {"type": platform, "id": music_id}}
        ]
        if session.chat_type == "group":
            await self.call_api(
                "send_group_msg",
                {"group_id": int(session.chat_id), "message": message},
            )
        else:
            await self.call_api(
                "send_private_msg",
                {"user_id": int(session.chat_id), "message": message},
            )

    async def send_like(self, user_id: str, times: int = 10) -> bool:
        """资料卡点赞。QQ 每日有限额，超限时协议端返回失败。"""
        try:
            resp = await self.call_api(
                "send_like", {"user_id": int(user_id), "times": times}
            )
        except Exception as e:
            logger.warning(f"点赞调用失败 {user_id}: {e}")
            return False
        return resp.get("status") == "ok" or resp.get("retcode") == 0

    async def is_friend(
        self, user_id: str, *, refresh: bool = False
    ) -> bool | None:
        """好友判定（get_friend_list，5 分钟缓存）；API 失败返回 None。

        no_cache=True 是必须的：NapCat 自己也缓存好友表（登录时建立），
        不带这个参数时刚加上的好友要等它下次同步才认，点赞会一直说不是好友。
        """
        now = time.monotonic()
        if (
            refresh
            or self._friends is None
            or now - self._friends_at > FRIEND_CACHE_TTL
        ):
            try:
                resp = await self.call_api(
                    "get_friend_list", {"no_cache": True}
                )
            except Exception as e:
                logger.warning(f"获取好友列表失败: {e}")
                return None
            if resp.get("status") != "ok" and resp.get("retcode") != 0:
                logger.warning(f"获取好友列表失败: {resp}")
                return None
            self._friends = {
                str(f.get("user_id")) for f in resp.get("data") or []
            }
            self._friends_at = now
        return user_id in self._friends

    async def recall(self, message_id: str) -> bool:
        try:
            resp = await self.call_api("delete_msg", {"message_id": int(message_id)})
        except Exception:
            logger.warning(f"撤回消息失败: {message_id}")
            return False
        return resp.get("status") == "ok"

    async def send_image(self, session: SessionKey, file: str) -> str | None:
        return await self._send_media(session, "image", file)

    async def send_sticker(self, session: SessionKey, file: str) -> None:
        """image 段带 sub_type=1：QQ 显示为表情包而非图片"""
        message = [
            {"type": "image", "data": {"file": file, "sub_type": "1"}}
        ]
        if session.chat_type == "group":
            await self.call_api(
                "send_group_msg",
                {"group_id": int(session.chat_id), "message": message},
            )
        else:
            await self.call_api(
                "send_private_msg",
                {"user_id": int(session.chat_id), "message": message},
            )

    async def send_video(self, session: SessionKey, file: str) -> None:
        await self._send_media(session, "video", file)

    async def send_record(self, session: SessionKey, file: str) -> None:
        await self._send_media(session, "record", file)

    async def send_forward(
        self, session: SessionKey, nodes: list[tuple[str, str, str | None]]
    ) -> None:
        uin = next(iter(self._connections), "10000")
        message = []
        for name, text, image in nodes:
            content: list[dict[str, Any]] = []
            if text:
                content.append({"type": "text", "data": {"text": text}})
            if image:
                content.append({"type": "image", "data": {"file": image}})
            message.append(
                {
                    "type": "node",
                    "data": {"name": name, "uin": uin, "content": content},
                }
            )
        if session.chat_type == "group":
            await self.call_api(
                "send_group_forward_msg",
                {"group_id": int(session.chat_id), "message": message},
            )
        else:
            await self.call_api(
                "send_private_forward_msg",
                {"user_id": int(session.chat_id), "message": message},
            )

    async def _send_media(
        self, session: SessionKey, seg_type: str, file: str
    ) -> str | None:
        message = [{"type": seg_type, "data": {"file": file}}]
        if session.chat_type == "group":
            resp = await self.call_api(
                "send_group_msg",
                {"group_id": int(session.chat_id), "message": message},
            )
        else:
            resp = await self.call_api(
                "send_private_msg",
                {"user_id": int(session.chat_id), "message": message},
            )
        return self._sent_message_id(resp)

    # ---- 超管设施：列表 / 退群 / 请求审批 --------------------------------

    async def list_friends(self) -> list[tuple[str, str]]:
        resp = await self.call_api("get_friend_list", {"no_cache": True})
        return [
            (str(f.get("user_id")), str(f.get("remark") or f.get("nickname") or ""))
            for f in resp.get("data") or []
        ]

    async def list_groups(self) -> list[tuple[str, str]]:
        resp = await self.call_api("get_group_list", {})
        return [
            (str(g.get("group_id")), str(g.get("group_name") or ""))
            for g in resp.get("data") or []
        ]

    async def leave_group(self, group_id: str) -> bool:
        try:
            resp = await self.call_api(
                "set_group_leave", {"group_id": int(group_id)}
            )
        except Exception as e:
            logger.warning(f"退群失败 {group_id}: {e}")
            return False
        return resp.get("status") == "ok" or resp.get("retcode") == 0

    async def fetch_pending_requests(self) -> list[IncomingRequest]:
        """补捞「可疑好友申请」。

        QQ 会把陌生人（尤其是只在群里见过的）的好友申请判定为可疑，单独收进
        一个桶：不推 request 事件、不进新朋友列表，只能主动查。协议端对这一桶
        也只提供同意、没有拒绝。
        """
        try:
            resp = await self.call_api(
                "get_doubt_friends_add_request", {"count": 50}
            )
        except Exception as e:
            logger.warning(f"查询可疑好友申请失败: {e}")
            return []
        if resp.get("status") != "ok" and resp.get("retcode") != 0:
            logger.warning(f"查询可疑好友申请失败: {resp}")
            return []
        requests = []
        for item in resp.get("data") or []:
            flag = str(item.get("flag") or "")
            user_id = str(item.get("user_id") or "")
            if not flag or not user_id:
                continue
            requests.append(
                IncomingRequest(
                    kind="friend",
                    sub_type="doubt",
                    flag=flag,
                    user_id=user_id,
                    user_name=str(item.get("nickname") or ""),
                    comment=str(item.get("reason") or "").strip(),
                )
            )
        return requests

    async def set_request_result(self, req, approve: bool) -> bool:
        try:
            if req.kind == "friend" and req.sub_type == "doubt":
                if not approve:
                    # 协议端的 approve 字段「仅做保留，强制为 True」
                    logger.info(f"可疑好友申请不支持拒绝: {req.user_id}")
                    return False
                resp = await self.call_api(
                    "set_doubt_friends_add_request",
                    {"flag": req.flag, "approve": True},
                )
            elif req.kind == "friend":
                resp = await self.call_api(
                    "set_friend_add_request",
                    {"flag": req.flag, "approve": approve},
                )
            else:
                resp = await self.call_api(
                    "set_group_add_request",
                    {
                        "flag": req.flag,
                        "sub_type": req.sub_type or "invite",
                        "approve": approve,
                    },
                )
        except Exception as e:
            logger.warning(f"请求审批调用失败 {req.kind}/{req.user_id}: {e}")
            return False
        if req.kind == "friend" and approve:
            self._friends = None  # 好友缓存失效，点赞/私聊判定即时生效
        return resp.get("status") == "ok" or resp.get("retcode") == 0
