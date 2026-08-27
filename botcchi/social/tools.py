"""Social 的 agent 工具：让「波奇帮我签个到」这类自然语言直达功能。

sign_in 是首个上下文工具：用户身份取自 ToolContext（LLM 无法伪造、
不能替别人签到），卡片图由工具直接经 channel 发出。
"""

from loguru import logger

from ..render import to_onebot_file
from ..tools.registry import Tool, ToolContext, ToolRegistry, tool
from .card import SocialCards
from .likes import LikeService
from .service import SocialService


def build_social_tools(
    social: SocialService, cards: SocialCards
) -> list[Tool]:
    @tool(
        name="sign_in",
        description=(
            "为当前对话的用户执行今日签到，获得好感度和金币，并把签到卡片图"
            "发到聊天中。用户表达想签到时调用（每人每天一次；当天重复调用"
            "无副作用，也不会再发卡片）。调用后用你的语气简短转述结果即可，"
            "不要重复卡片里的所有数字。"
        ),
    )
    async def sign_in(ctx: ToolContext) -> str:
        result = await social.sign(ctx.incoming.user_id, ctx.incoming.user_name)
        p = result.profile
        lv = p.level
        if not result.signed:
            # 卡片每人每天只发一次，重复签到只回信息让模型转述
            return (
                f"该用户今天已经签到过了（卡片当天只发一次，本次未发送）。"
                f"当前好感度 {p.impression:g}（Lv.{lv.level} {lv.relation}），"
                f"金币 {p.coins}，连续签到 {p.streak} 天。"
                "请用你的语气温柔地告诉用户已经签过了、明天再来。"
            )
        card_note = "签到卡片已发送"
        try:
            png = await cards.sign_card(result, ctx.bot_name)
            await ctx.channel.send_image(ctx.incoming.session, to_onebot_file(png))
        except Exception:
            logger.exception("签到卡片发送失败")
            card_note = "卡片图发送失败了，请把结果用文字告诉用户。"
        return (
            f"签到成功：好感度 +{result.impression_added:g}"
            f"{'（触发双倍）' if result.double else ''}，"
            f"金币 +{result.coins_added}（含连签加成 {result.streak_bonus}），"
            f"当前好感度 {p.impression:g}（Lv.{lv.level} {lv.relation}），"
            f"连续签到 {p.streak} 天。{card_note}"
        )

    @tool(
        name="get_user_social",
        description=(
            "查询用户的好感度等级、金币余额和签到情况。"
            "user_id 留空查询当前对话的用户，也可传其他用户的 QQ 号。"
        ),
    )
    async def get_user_social(ctx: ToolContext, user_id: str = "") -> str:
        uid = user_id.strip() or ctx.incoming.user_id
        p = await social.peek(uid)
        if p is None:
            return f"用户 {uid} 还没有签到或互动记录"
        lv = p.level
        return (
            f"用户 {p.user_name or p.user_id}：好感度 {p.impression:g}"
            f"（Lv.{lv.level} {lv.relation}，好感排名第 {p.rank}），"
            f"金币 {p.coins}，累计签到 {p.sign_count} 天（连续 {p.streak} 天）"
        )

    return [sign_in, get_user_social]


def build_like_tool(likes: LikeService) -> Tool:
    @tool(
        name="send_like",
        description=(
            "给当前对话用户的 QQ 资料卡点赞（名片赞）。用户求赞/要赞时调用。"
            "times 为想点的赞数（1~50），用户没说数量就不传（尽量多点）。"
            "只能给好友点赞；QQ 每日有限额，实际数量可能少于请求。"
            "把结果如实转告用户即可。"
        ),
    )
    async def send_like(ctx: ToolContext, times: int = 0) -> str:
        result = await likes.give(
            ctx.channel, ctx.incoming.user_id, times if times > 0 else None
        )
        if not result.supported:
            return "当前聊天渠道不支持点赞"
        if result.friend is False:
            return "点赞失败：对方还不是好友，请告诉用户先加 bot 为好友"
        if result.count <= 0:
            if result.friend:
                return "点赞失败：今天给这位用户的赞已经点满了（每日限额）"
            return "点赞失败：可能今天已点过赞，或对方还不是好友"
        if times > 0 and result.count < times:
            return (
                f"已给用户点了 {result.count} 个赞"
                f"（想点 {times} 个，达到每日限额了）"
            )
        return f"已成功给用户点了 {result.count} 个赞"

    return send_like


def register_social_tools(
    registry: ToolRegistry, social: SocialService, cards: SocialCards
) -> None:
    for t in build_social_tools(social, cards):
        registry.register(t)
