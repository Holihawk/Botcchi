"""功能开关注册表：把指令前缀与 LLM 工具归组为可按群开关的「功能」。

功能元数据在代码内声明，数据库只存偏离默认值的覆盖（群禁用列表），
避免旧版 PluginInfo 表与代码漂移的问题。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Feature:
    key: str
    """开关名（也是指令里引用的名字），如「签到」"""
    description: str
    commands: tuple[str, ...] = ()
    """归属本功能的指令前缀（startswith 匹配）"""
    tools: tuple[str, ...] = ()
    """归属本功能的 LLM 工具名（禁用时从 schema 中隐藏并拒绝执行）"""


FEATURES: dict[str, Feature] = {
    f.key: f
    for f in (
        Feature(
            "闲聊",
            "AI 对话（@/私聊/唤醒词与伪人插话）",
        ),
        Feature("复读", "复读机与打断施法"),
        Feature(
            "签到",
            "签到、好感度与金币",
            commands=("签到", "我的信息", "好感度", "我的金币"),
            tools=("sign_in", "get_user_social"),
        ),
        Feature(
            "商店",
            "金币商店：头像挂件与消耗品",
            commands=("商店", "购买", "佩戴", "装备", "取下挂件", "我的背包", "背包"),
            tools=("equip_pendant", "list_backpack"),
        ),
        Feature(
            "决斗",
            "轮盘决斗小游戏（游戏券入场）",
            commands=("决斗",),
        ),
        Feature(
            "24点",
            "24 点抢答小游戏（游戏券入场）",
            commands=("24点",),
        ),
        Feature(
            "21点",
            "Blackjack 对庄小游戏（游戏券入场）",
            commands=("blackjack",),
        ),
        Feature(
            "点赞",
            "资料卡点赞",
            commands=("赞我", "点赞", "给我点赞"),
            tools=("send_like",),
        ),
        Feature(
            "提醒",
            "自然语言定时提醒",
            tools=("set_reminder", "list_reminders", "cancel_reminder"),
        ),
        Feature(
            "总结",
            "群聊消息总结（手动与定时）",
            commands=("消息总结", "总结定时"),
            tools=("summarize_group", "schedule_group_summary"),
        ),
        Feature(
            "日报",
            "孤独日报（手动与定时）",
            commands=("孤独日报", "日报定时"),
            tools=("get_daily_report", "schedule_daily_report"),
        ),
        Feature(
            "小猪",
            "今日小猪占卜与 PigHub 找猪",
            commands=(
                "今日小猪", "本日小猪", "当日小猪", "今天是什么小猪",
                "随机小猪", "找猪", "搜猪",
            ),
            tools=("daily_pig_fortune", "find_pig"),
        ),
        Feature("迎新", "进群欢迎与退群提醒"),
        Feature(
            "天气",
            "和风天气查询（实况 + 3 日预报卡片）",
            commands=("天气",),
            tools=("get_weather",),
        ),
        Feature(
            "绘图",
            "GPT 画图/改图",
            commands=("画图", "生图"),
            tools=("draw_image",),
        ),
        Feature(
            "解析",
            "分享链接自动解析（B站/抖音/微博/小红书/快手/AcFun/网易云）",
            tools=("parse_link",),
        ),
        Feature(
            "表情包",
            "聊天时按心情发表情包",
            tools=("send_sticker",),
        ),
        Feature(
            "点歌",
            "网易云点歌卡片",
            tools=("share_music",),
        ),
        Feature(
            "戳一戳",
            "QQ 戳一戳互动",
            tools=("poke_user",),
        ),
        Feature(
            "搜索",
            "联网搜索与网页阅读（Tavily）",
            tools=("web_search", "fetch_page"),
        ),
        Feature(
            "记忆",
            "长期记忆的主动保存与查询",
            commands=("清除记忆", "清空记忆"),
            tools=("save_memory", "search_memory"),
        ),
    )
}


def feature_for_command(text: str) -> str | None:
    """返回指令文本归属的功能名；不属于任何功能开关的指令返回 None"""
    for feature in FEATURES.values():
        if any(text.startswith(prefix) for prefix in feature.commands):
            return feature.key
    return None


def tools_of(blocked: set[str] | frozenset[str]) -> frozenset[str]:
    """被禁用功能所覆盖的全部工具名"""
    return frozenset(
        name
        for key in blocked
        if (f := FEATURES.get(key)) is not None
        for name in f.tools
    )
