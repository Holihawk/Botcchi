"""超管设施：请求审批、广播、作弊器、好友/群组列表、退群。

全部指令仅超管可用，不进帮助卡片，也不受群功能开关约束。
好友申请与拉群邀请一律不自动同意——落库为待办并私聊通知超管，
经「同意请求 <id>」等指令审批（这就是"不被拉进奇怪群聊"的防线）。

待办来自两条路：协议端推送的 request 事件，以及定期向协议端补捞
（QQ 判定为可疑的好友申请压根不推事件，bot 离线期间到达的也补不回来）。
两条路按 flag 去重，同一条申请只会通知一次。
"""

import asyncio
import re
from contextlib import suppress

from loguru import logger
from sqlalchemy import select

from .channels.base import Channel, IncomingMessage, IncomingRequest, SessionKey
from .db.engine import Database
from .db.models import PendingRequestRow
from .perms import PermissionGate
from .social.service import SocialService

BROADCAST_DELAY = 0.4
"""相邻两个群之间的发送间隔（秒），避免风控；测试可置 0"""
REQUEST_POLL_INTERVAL = 300.0
"""补捞协议端待办申请的间隔（秒）"""
CONNECT_RETRY = 5.0
"""协议端还没连上时的重试间隔（秒）——比轮询间隔短，连上后尽快查第一次"""
LIST_MAX = 60
_QUOTED_BROADCAST = re.compile(
    r"^【被引用的消息\|[^】]*】(?P<body>.*)\n广播$", re.S
)

_NO_DB = "未配置数据库，此功能不可用"


def _request_line(row: PendingRequestRow) -> str:
    if row.kind == "friend":
        tag = "可疑好友" if row.sub_type == "doubt" else "好友"
        head = f"[{tag}] {row.user_name or '?'}({row.user_id})"
    elif row.sub_type == "add":
        head = (
            f"[进群] {row.user_name or '?'}({row.user_id}) 申请加入 "
            f"{row.group_name or '?'}({row.group_id})"
        )
    else:
        head = (
            f"[拉群] {row.user_name or '?'}({row.user_id}) 邀请加入 "
            f"{row.group_name or '?'}({row.group_id})"
        )
    comment = f"：{row.comment}" if row.comment else ""
    return f"#{row.id} {head}{comment}"


class SuperuserService:
    def __init__(
        self,
        perms: PermissionGate,
        db: Database | None = None,
        social: SocialService | None = None,
        superusers: list[str] | None = None,
    ):
        self.perms = perms
        self.db = db
        self.social = social
        self.superusers = superusers or sorted(perms.superusers)
        self.broadcast_delay = BROADCAST_DELAY
        self.poll_interval = REQUEST_POLL_INTERVAL
        self.connect_retry = CONNECT_RETRY
        self._last_broadcast: list[str] = []
        """最近一次广播发出的 message_id（撤回用）"""

    # ---- 请求事件入队与通知 ---------------------------------------------

    async def on_request(self, channel: Channel, req: IncomingRequest) -> None:
        """channel 收到好友申请/拉群邀请：落库 + 私聊通知全部超管"""
        line = None
        if self.db is not None:
            async with self.db.session() as s:
                row = PendingRequestRow(
                    kind=req.kind,
                    sub_type=req.sub_type,
                    flag=req.flag,
                    user_id=req.user_id,
                    user_name=req.user_name,
                    group_id=req.group_id,
                    group_name=req.group_name,
                    comment=req.comment,
                )
                s.add(row)
                await s.commit()
                line = _request_line(row)
        if line is None:
            # 无数据库时无法审批，只能通知超管去协议端手动处理
            line = _request_line(
                PendingRequestRow(
                    kind=req.kind, sub_type=req.sub_type, flag="",
                    user_id=req.user_id, user_name=req.user_name,
                    group_id=req.group_id, group_name=req.group_name,
                    comment=req.comment,
                )
            ).replace("#None ", "")
            text = f"收到请求（未配置数据库，需在协议端手动处理）\n{line}"
        else:
            text = (
                f"收到新请求：\n{line}\n"
                "发送「同意请求 <编号>」「拒绝请求 <编号>」「忽略请求 <编号>」处理，"
                "「查看请求」看全部待办"
            )
        await self._notify_superusers(channel, text)

    async def _notify_superusers(self, channel: Channel, text: str) -> None:
        for su in self.superusers:
            session = SessionKey(
                channel=channel.name, chat_type="private", chat_id=su
            )
            try:
                await channel.send_text(session, text)
            except Exception:
                logger.warning(f"请求通知发送失败: 超管 {su}")

    async def watch_requests(
        self, channel: Channel, stop: asyncio.Event
    ) -> None:
        """定期补捞协议端待办申请并通知超管。

        光靠 on_request 推送是不够的：QQ 判定为可疑的好友申请不推事件，
        bot 离线期间到达的申请也补不回来，只能定期主动查。
        """
        while not stop.is_set():
            # 反向 WS 是对端来连的，启动后有段空窗期；这时查询只会白报错
            if not channel.is_connected:
                delay = self.connect_retry
            else:
                delay = self.poll_interval
                try:
                    if added := await self._sync_requests(channel):
                        await self._notify_superusers(
                            channel,
                            f"收到 {len(added)} 条新请求：\n" + "\n".join(added)
                            + "\n发送「同意请求 <编号>」处理，"
                            "「查看请求」看全部待办",
                        )
                except Exception:
                    logger.exception("轮询待办请求失败")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)

    # ---- 指令入口 --------------------------------------------------------

    async def handle(
        self, incoming: IncomingMessage, channel: Channel
    ) -> bool:
        """尝试作为超管指令处理，返回 True 表示已消费。非超管直接放行"""
        if not self.perms.is_superuser(incoming.user_id):
            return False
        text = incoming.text.strip()

        async def reply(msg: str) -> None:
            await channel.send_text(incoming.session, msg)

        if text == "查看请求":
            await self._sync_requests(channel)
            await self._list_requests(reply)
            return True
        if text == "清空请求":
            await self._clear_requests(reply)
            return True
        for prefix, decision in (
            ("同意请求", "approved"),
            ("拒绝请求", "rejected"),
            ("忽略请求", "ignored"),
        ):
            if text.startswith(prefix):
                await self._decide_request(
                    channel, reply, text.removeprefix(prefix).strip(), decision
                )
                return True

        if text == "广播撤回":
            await self._recall_broadcast(channel, reply)
            return True
        if body := self._broadcast_body(text):
            await self._broadcast(incoming, channel, reply, body)
            return True

        if text.startswith("金币设置"):
            await self._set_coins(
                incoming, reply, text.removeprefix("金币设置").strip()
            )
            return True
        if text.startswith("好感度设置"):
            await self._set_impression(
                incoming, reply, text.removeprefix("好感度设置").strip()
            )
            return True

        if text == "好友列表":
            await self._list_contacts(channel, reply, friends=True)
            return True
        if text == "群组列表":
            await self._list_contacts(channel, reply, friends=False)
            return True
        if text.startswith("退群"):
            rest = text.removeprefix("退群").strip()
            if not rest.isdigit():
                # 「退群提示」等日常聊天不拦截，只有带群号才算指令
                return False
            await self._leave_group(channel, reply, rest)
            return True
        return False

    # ---- 请求审批 --------------------------------------------------------

    async def _sync_requests(self, channel: Channel) -> list[str]:
        """把协议端侧的待办申请补捞进队列，返回新增条目的描述行。

        推送事件收不到的那些（可疑好友申请、bot 离线期间到达的）靠这里兜底，
        按 flag 去重，已经处理过的不会重新冒出来。
        """
        if self.db is None:
            return []
        try:
            found = await channel.fetch_pending_requests()
        except NotImplementedError:
            return []
        except Exception:
            logger.exception("补捞待办请求失败")
            return []
        if not found:
            return []
        added: list[str] = []
        async with self.db.session() as s:
            known = set(
                (
                    await s.scalars(
                        select(PendingRequestRow.flag).where(
                            PendingRequestRow.flag.in_([r.flag for r in found])
                        )
                    )
                ).all()
            )
            rows = []
            for req in found:
                if req.flag in known:
                    continue
                row = PendingRequestRow(
                    kind=req.kind, sub_type=req.sub_type, flag=req.flag,
                    user_id=req.user_id, user_name=req.user_name,
                    group_id=req.group_id, group_name=req.group_name,
                    comment=req.comment,
                )
                s.add(row)
                rows.append(row)
            await s.commit()
            added = [_request_line(r) for r in rows]  # id 要 commit 后才有
        if added:
            logger.info(f"补捞到 {len(added)} 条协议端待办请求")
        return added

    async def _list_requests(self, reply) -> None:
        if self.db is None:
            await reply(_NO_DB)
            return
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(PendingRequestRow)
                    .where(PendingRequestRow.status == "pending")
                    .order_by(PendingRequestRow.id)
                )
            ).all()
        if not rows:
            await reply("没有待处理的请求")
            return
        lines = [_request_line(r) for r in rows[:LIST_MAX]]
        if len(rows) > LIST_MAX:
            lines.append(f"…共 {len(rows)} 条，仅展示前 {LIST_MAX} 条")
        await reply(
            f"📋 待处理请求（{len(rows)}）\n" + "\n".join(lines)
            + "\n发送「同意请求 <编号>」「拒绝请求 <编号>」「忽略请求 <编号>」处理"
        )

    async def _decide_request(
        self, channel: Channel, reply, arg: str, decision: str
    ) -> None:
        if self.db is None:
            await reply(_NO_DB)
            return
        if not arg.isdigit():
            await reply("用法：同意/拒绝/忽略请求 <编号>，「查看请求」看编号")
            return
        async with self.db.session() as s:
            row = await s.get(PendingRequestRow, int(arg))
            if row is None or row.status != "pending":
                await reply(f"没有待处理的请求 #{arg}")
                return
            if decision == "ignored":
                row.status = decision
                await s.commit()
                await reply(f"已忽略 {_request_line(row)}")
                return
            if row.kind == "friend" and row.sub_type == "doubt" and (
                decision == "rejected"
            ):
                # 协议端对可疑好友申请只提供同意，拒绝得去 QQ 里操作
                await reply(
                    f"可疑好友申请只能同意或忽略，{_request_line(row)} 保持待办；"
                    "要真正拒绝请在 QQ 客户端处理，然后「忽略请求」清掉"
                )
                return
            req = IncomingRequest(
                kind=row.kind, flag=row.flag, user_id=row.user_id,
                sub_type=row.sub_type, group_id=row.group_id,
            )
            try:
                ok = await channel.set_request_result(
                    req, decision == "approved"
                )
            except NotImplementedError:
                await reply("当前渠道不支持请求审批")
                return
            if not ok:
                await reply(
                    f"审批调用失败（请求可能已过期），{_request_line(row)} 仍保留待办；"
                    "确认已失效可「忽略请求」清掉"
                )
                return
            row.status = decision
            await s.commit()
            verb = "已同意" if decision == "approved" else "已拒绝"
            await reply(f"{verb} {_request_line(row)}")

    async def _clear_requests(self, reply) -> None:
        if self.db is None:
            await reply(_NO_DB)
            return
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(PendingRequestRow).where(
                        PendingRequestRow.status == "pending"
                    )
                )
            ).all()
            for r in rows:
                r.status = "ignored"
            await s.commit()
        await reply(f"已清空 {len(rows)} 条待处理请求" if rows else "没有待处理的请求")

    # ---- 广播 ------------------------------------------------------------

    @staticmethod
    def _broadcast_body(text: str) -> str | None:
        """解析广播正文；返回 None 表示不是广播指令。

        支持「广播 <内容>」与引用消息后发「广播」两种形式。
        """
        if text.startswith("广播"):
            body = text.removeprefix("广播").strip()
            return body or " "  # 纯图广播：正文为空但仍进入广播流程
        if m := _QUOTED_BROADCAST.match(text):
            body = m.group("body").strip()
            return body if body and body != "（无文本）" else " "
        return None

    async def _broadcast(
        self, incoming: IncomingMessage, channel: Channel, reply, body: str
    ) -> None:
        body = body.strip()
        images = list(incoming.images)
        if not body and not images:
            await reply("要广播什么？用法：广播 <内容>（可带图，或引用消息后发「广播」）")
            return
        try:
            groups = await channel.list_groups()
        except NotImplementedError:
            await reply("当前渠道不支持广播")
            return
        exclude = (
            incoming.session.chat_id
            if incoming.session.chat_type == "group"
            else None
        )
        sent_ids: list[str] = []
        ok = failed = skipped = 0
        for gid, _name in groups:
            if gid == exclude:
                continue
            # 被封禁/休眠中的群不打扰
            if await self.perms.is_banned("", gid):
                skipped += 1
                continue
            if not (await self.perms.group_state(gid)).active:
                skipped += 1
                continue
            session = SessionKey(
                channel=channel.name, chat_type="group", chat_id=gid
            )
            try:
                if body:
                    if mid := await channel.send_text(session, body):
                        sent_ids.append(mid)
                for img in images:
                    if mid := await channel.send_image(session, img):
                        sent_ids.append(mid)
                ok += 1
            except Exception:
                logger.exception(f"广播发送失败: 群 {gid}")
                failed += 1
            if self.broadcast_delay:
                await asyncio.sleep(self.broadcast_delay)
        self._last_broadcast = sent_ids
        parts = [f"广播完成：已发送到 {ok} 个群"]
        if skipped:
            parts.append(f"跳过 {skipped} 个（封禁/休眠）")
        if failed:
            parts.append(f"失败 {failed} 个")
        await reply("，".join(parts) + ("，「广播撤回」可收回" if sent_ids else ""))

    async def _recall_broadcast(self, channel: Channel, reply) -> None:
        if not self._last_broadcast:
            await reply("没有可撤回的广播（只能撤回最近一次，且重启后失效）")
            return
        ok = 0
        for mid in self._last_broadcast:
            try:
                if await channel.recall(mid):
                    ok += 1
            except Exception:
                logger.warning(f"广播撤回失败: {mid}")
        total = len(self._last_broadcast)
        self._last_broadcast = []
        await reply(f"已撤回 {ok}/{total} 条广播消息")

    # ---- 作弊器 ----------------------------------------------------------

    @staticmethod
    def _parse_target_value(
        incoming: IncomingMessage, rest: str
    ) -> tuple[str | None, str | None]:
        """从「<数值> @用户」或「<数值> <QQ号>」解析 (目标, 数值串)"""
        tokens = rest.replace("@", " @").split()
        value = next(
            (t for t in tokens if not t.startswith("@")
             and re.fullmatch(r"-?\d+(\.\d+)?", t)),
            None,
        )
        target = incoming.mentions[0] if incoming.mentions else None
        if target is None:
            # 无 @ 时允许纯数字 QQ 号：数值之外的另一个数字 token
            others = [
                t for t in tokens if t != value and t.lstrip("@").isdigit()
            ]
            if others:
                target = others[0].lstrip("@")
        return target, value

    async def _set_coins(
        self, incoming: IncomingMessage, reply, rest: str
    ) -> None:
        if self.social is None:
            await reply(_NO_DB)
            return
        target, value = self._parse_target_value(incoming, rest)
        if target is None or value is None or "." in value or int(value) < 0:
            await reply("用法：金币设置 <数量> @用户（或 QQ 号）")
            return
        delta, balance = await self.social.set_coins(target, int(value))
        await reply(
            f"已把 {target} 的金币设置为 {balance}"
            f"（{'+' if delta >= 0 else ''}{delta}，已记流水）"
        )

    async def _set_impression(
        self, incoming: IncomingMessage, reply, rest: str
    ) -> None:
        if self.social is None:
            await reply(_NO_DB)
            return
        target, value = self._parse_target_value(incoming, rest)
        if target is None or value is None or float(value) < 0:
            await reply("用法：好感度设置 <数值> @用户（或 QQ 号）")
            return
        new = await self.social.set_impression(target, float(value))
        await reply(f"已把 {target} 的好感度设置为 {new:g}")

    # ---- 列表与退群 ------------------------------------------------------

    async def _list_contacts(
        self, channel: Channel, reply, *, friends: bool
    ) -> None:
        try:
            pairs = (
                await channel.list_friends()
                if friends
                else await channel.list_groups()
            )
        except NotImplementedError:
            await reply("当前渠道不支持该列表")
            return
        except Exception:
            logger.exception("获取列表失败")
            await reply("列表获取失败，稍后再试")
            return
        title = "👥 好友列表" if friends else "📋 群组列表"
        if not pairs:
            await reply(f"{title}：空")
            return
        lines = [f"{name or '?'}（{cid}）" for cid, name in pairs[:LIST_MAX]]
        if len(pairs) > LIST_MAX:
            lines.append(f"…共 {len(pairs)} 个，仅展示前 {LIST_MAX} 个")
        await reply(f"{title}（{len(pairs)}）\n" + "\n".join(lines))

    async def _leave_group(self, channel: Channel, reply, gid: str) -> None:
        try:
            ok = await channel.leave_group(gid)
        except NotImplementedError:
            await reply("当前渠道不支持退群")
            return
        await reply(f"已退出群 {gid}" if ok else f"退群失败（可能不在群 {gid} 中）")
