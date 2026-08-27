"""签到/好感度的内置指令：签到、我的信息、好感度排行（卡片出图含文字回退）"""

from loguru import logger

from ..channels.base import Channel, IncomingMessage
from ..commands import Command, exact
from ..render import to_onebot_file
from .card import SocialCards
from .service import (
    SignResult,
    SocialProfile,
    SocialService,
    already_signed_reply,
)

_NO_DB = "未配置数据库，签到/好感度不可用"


async def send_social_card(
    incoming: IncomingMessage,
    channel: Channel,
    cards: SocialCards | None,
    bot_name: str,
    *,
    sign_result: SignResult | None = None,
    profile: SocialProfile | None = None,
) -> None:
    """出卡片图；渲染或 channel 不支持图片时退化为文本"""
    if cards is not None:
        try:
            if sign_result is not None:
                png = await cards.sign_card(sign_result, bot_name)
            else:
                assert profile is not None
                png = await cards.profile_card(profile, bot_name)
            await channel.send_image(incoming.session, to_onebot_file(png))
            return
        except NotImplementedError:
            pass
        except Exception:
            logger.exception("签到卡片渲染/发送失败，退化为文本")
    if sign_result is not None:
        p = sign_result.profile
        lv = p.level
        if sign_result.signed:
            text = (
                f"签到成功！好感度 +{sign_result.impression_added:g}"
                f"{'（双倍！）' if sign_result.double else ''}，"
                f"金币 +{sign_result.coins_added}，"
                f"当前好感度 {p.impression:g}（Lv.{lv.level} {lv.relation}），"
                f"连续签到 {p.streak} 天"
            )
        else:
            text = (
                f"今天已经签到过啦，明天再来～ "
                f"当前好感度 {p.impression:g}（Lv.{lv.level} {lv.relation}）"
            )
    else:
        assert profile is not None
        lv = profile.level
        text = (
            f"{profile.user_name or profile.user_id}：好感度 "
            f"{profile.impression:g}（Lv.{lv.level} {lv.relation}，"
            f"排名第 {profile.rank}），金币 {profile.coins}，"
            f"累计签到 {profile.sign_count} 天（连续 {profile.streak} 天）"
        )
    await channel.send_text(incoming.session, text)


def build_social_commands(
    social: SocialService | None,
    cards: SocialCards | None,
    resolve_assistant,
) -> list[Command]:
    async def sign_cmd(incoming, channel, rest: str) -> bool:
        if social is None:
            await channel.send_text(incoming.session, "未配置数据库，签到不可用")
            return True
        result = await social.sign(incoming.user_id, incoming.user_name)
        if not result.signed:
            # 卡片每人每天只发一次，重复签到只给文字提示
            await channel.send_text(incoming.session, already_signed_reply())
            return True
        assistant = await resolve_assistant(incoming.session)
        await send_social_card(
            incoming, channel, cards, assistant.nickname, sign_result=result
        )
        return True

    async def profile_cmd(incoming, channel, rest: str) -> bool:
        if social is None:
            await channel.send_text(incoming.session, _NO_DB)
            return True
        profile = await social.profile(incoming.user_id, incoming.user_name)
        assistant = await resolve_assistant(incoming.session)
        await send_social_card(
            incoming, channel, cards, assistant.nickname, profile=profile
        )
        return True

    async def rank_cmd(incoming, channel, rest: str) -> bool:
        if social is None:
            await channel.send_text(incoming.session, _NO_DB)
            return True
        top = await social.top(10)
        if not top:
            await channel.send_text(
                incoming.session, "还没有人签到过，发送「签到」抢个头名吧！"
            )
            return True
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[p.rank - 1] if p.rank <= 3 else f'{p.rank}.'} "
            f"{p.user_name or p.user_id}  好感 {p.impression:g}"
            f"（{p.level.relation}）"
            for p in top
        ]
        await channel.send_text(
            incoming.session, "💗 好感度排行\n" + "\n".join(lines)
        )
        return True

    return [
        Command(exact("签到"), sign_cmd),
        Command(exact("我的信息", "好感度", "我的金币"), profile_cmd),
        Command(exact("好感度排行"), rank_cmd),
    ]
