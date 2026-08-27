"""帮助菜单：主菜单卡 +「帮助 <编号/名称>」功能详情卡。

条目在代码内注册（TOPICS），按四个分类分组展示，分别使用結束バンド
四色（蓝/黄/红/粉）排版；布局参照旧版帮助卡：顶栏左 logo 右 bot 头像 +
可用功能数量、右下角粉色章鱼、底栏仅「Power by Botcchi」，不含时间戳。
普通与群管指令合并展示（管理指令带徽章、仅超管的条目直接隐藏）。
本群被「关闭功能」关掉的条目仍然列出，但置灰划掉，顶栏数量只算可用的；
内容稳定，同一「可用服务 + 关闭功能」组合命中出图缓存（即按群缓存，
开关相同的群共用一张图）。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from loguru import logger

if TYPE_CHECKING:
    from .perms import PermissionGate

from .render import RenderCache, SvgRenderer, image_data_uri, text_width
from .social.card import (
    F_CR105,
    F_JCYT,
    F_RXK,
    F_SH,
    FONT_DIR,
    _defs,
    _fit,
    _text,
    _wrap,
)

W = 700
"""卡片宽度"""

ASSETS = Path(__file__).parent / "assets" / "help"
QQ_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={}&s=160"

HEADER = "#F67186"
"""顶栏粉"""
SUB = "#A08890"
OFF_TEXT = "#B9AEB3"
"""本群已关闭的条目：文字灰"""
OFF_DOT = "#CFC5CA"
OFF_LINE = "#9C9095"
STOPS = (
    '<stop offset="0" stop-color="#FFD9E8"/>'
    '<stop offset="0.55" stop-color="#FBE4E4"/>'
    '<stop offset="1" stop-color="#FFF3F7"/>'
)


@dataclass(frozen=True, slots=True)
class CategoryTheme:
    name: str
    accent: str
    dark: str
    tint: str


CATEGORIES: tuple[CategoryTheme, ...] = (
    CategoryTheme("聊天互动", "#F08CB0", "#C95F86", "#FDF0F5"),  # 粉
    CategoryTheme("实用工具", "#5B7FC7", "#3D5FA6", "#EEF3FB"),  # 蓝
    CategoryTheme("社交玩法", "#F0C542", "#B98A1D", "#FCF6E5"),  # 黄
    CategoryTheme("管理", "#E8635A", "#C6423A", "#FDEFED"),  # 红
)


def category_theme(name: str) -> CategoryTheme:
    for c in CATEGORIES:
        if c.name == name:
            return c
    return CATEGORIES[0]


@dataclass(frozen=True, slots=True)
class HelpItem:
    usage: str
    desc: str
    manage: bool = False
    """需管理权限（帮助里带徽章标注）"""
    su_only: bool = False
    """仅超管：不出现在帮助里"""


@dataclass(frozen=True, slots=True)
class HelpTopic:
    key: str
    """名称（「帮助 <名称>」引用；与功能开关同名时一致）"""
    category: str
    """所属分类（CATEGORIES.name）"""
    summary: str
    """主菜单里的一句话介绍"""
    intro: str
    """详情卡开头的说明"""
    items: tuple[HelpItem, ...] = ()
    requires: str | None = None
    """依赖的服务：social/weather/imagegen/summary/report/scheduler，
    未配置时整个主题从菜单隐藏"""


TOPICS: tuple[HelpTopic, ...] = (
    # ---- 聊天互动（粉） ----
    HelpTopic(
        "闲聊", "聊天互动", "和波奇聊天与会话管理",
        "@波奇、叫她名字或私聊都会回复；群里她还会看气氛随机插话。",
        (
            HelpItem("reset", "清空当前会话上下文（群里需管理权限）"),
            HelpItem("清除记忆", "清空长期记忆：私聊清自己的，群里仅超管可清"),
            HelpItem("人格列表", "查看可用人格"),
            HelpItem("切换人格 <名字>", "切换当前会话人格", manage=True),
        ),
    ),
    HelpTopic(
        "复读", "聊天互动", "跟着大家一起复读",
        "群里多人复读同一内容时波奇会跟上，还有打断施法的小彩蛋。",
        (HelpItem("（无需指令）", "复读自然发生"),),
    ),
    HelpTopic(
        "提醒", "聊天互动", "自然语言定时提醒",
        "直接对波奇说就行，到点她会来喊你。",
        (
            HelpItem("（自然语言）", "如「波奇 10 分钟后提醒我收快递」"),
            HelpItem("（自然语言）", "「我有哪些提醒」「取消刚才的提醒」"),
        ),
        requires="scheduler",
    ),
    HelpTopic(
        "点歌", "聊天互动", "网易云点歌与戳一戳",
        "让波奇点歌会发可播放的音乐卡片；求戳会真的戳你一下。",
        (
            HelpItem("（自然语言）", "如「波奇放首孤独摇滚的歌」「点一首青春コンプレックス」"),
            HelpItem("（自然语言）", "「戳戳我」「戳戳@某人」"),
        ),
    ),
    # ---- 实用工具（蓝） ----
    # 「搜索」是闲聊时自动触发的基础能力，不作为独立条目占位
    HelpTopic(
        "天气", "实用工具", "查天气出卡片",
        "实况 + 3 日预报 + 空气质量 + 气象预警",
        (HelpItem("天气 <城市> / <城市>天气", "如「天气 南京」「南京天气」"),),
        requires="weather",
    ),
    HelpTopic(
        "绘图", "实用工具", "AI 画图与改图",
        "描述想要的画面即可；消息带图或引用图片可以改图。",
        (
            HelpItem("画图 <描述>", "生成图片（每人有冷却时间）"),
            HelpItem("画图 竖图/横图/高清 <描述>", "选尺寸预设"),
            HelpItem("画图 帮助", "查看绘图详细用法"),
        ),
        requires="imagegen",
    ),
    HelpTopic(
        "解析", "实用工具", "分享链接自动解析",
        "把 B站/抖音/微博/小红书/快手/AcFun/网易云 的分享链接或小程序卡片"
        "发进群里，波奇会自动出摘要卡，并在 一定条件下搬运视频/音频/图集。",
        (HelpItem("（无需指令）", "直接发送分享链接即可"),),
    ),
    HelpTopic(
        "总结", "实用工具", "群聊消息总结",
        "翻最近的群聊记录生成总结，支持主题聚焦和每日定时。",
        (
            HelpItem("消息总结 [数量] [主题]", "如「消息总结 300 游戏」"),
            HelpItem("总结定时 <小时> [最少消息数]", "每天自动总结", manage=True),
            HelpItem("总结定时取消", "取消本群定时总结", manage=True),
        ),
        requires="summary",
    ),
    HelpTopic(
        "日报", "实用工具", "孤独日报新闻聚合图",
        "节日倒数、B站热搜、今日新番、60 秒读世界、IT 资讯、今日一言。",
        (
            HelpItem("孤独日报", "看今天的日报"),
            HelpItem("日报定时 <小时>", "每天定时推送到本群", manage=True),
            HelpItem("日报定时取消", "取消本群定时推送", manage=True),
        ),
        requires="report",
    ),
    # ---- 社交玩法（黄） ----
    HelpTopic(
        "签到", "社交玩法", "每日签到攒好感度和金币",
        "好感度会影响波奇对你的态度，金币可以在商店消费。",
        (
            HelpItem("签到", "每日一次，得好感度和金币（连签有加成、小概率双倍）"),
            HelpItem("我的信息 / 好感度 / 我的金币", "查看个人卡片"),
            HelpItem("好感度排行", "好感度全局 Top 10"),
        ),
        requires="social",
    ),
    HelpTopic(
        "商店", "社交玩法", "金币购买头像挂件与道具",
        "「波奇の商店」：挂件戴在签到卡头像上，道具签到时自动生效。",
        (
            HelpItem("商店", "看货架卡片"),
            HelpItem("购买 <商品名> [数量]", "金币下单，可多份的商品带数量"),
            HelpItem("佩戴 <挂件名> / 取下挂件", "换装头像挂件"),
            HelpItem("我的背包", "看已拥有的物品和金币余额"),
        ),
        requires="social",
    ),
    HelpTopic(
        "决斗", "社交玩法", "轮盘决斗，游戏券入场赌奖池",
        "多人决斗：轮流开枪赌空实弹，空弹打自己可以连续行动。"
        "每人 3 张游戏券入场，奖池 120×人数；开局公告实弹数，流局退券。",
        (
            HelpItem("决斗@某人", "发起 1v1 约战（45 秒内对方「接受」开局）"),
            HelpItem("决斗", "开多人决斗桌 / 加入已开的桌（最多 6 人）"),
            HelpItem("开始", "发起者开局（至少 2 人）"),
            HelpItem("选 <道具名>", "开局前选一件道具（糖/手铐/透镜/延长管），"
                     "直接说道具名也行"),
            HelpItem("打自己 / 打对面 / 打@某人", "对局中开枪"),
            HelpItem("用 <道具名>", "自己回合内使用道具（手铐要 @目标）"),
        ),
        requires="social",
    ),
    HelpTopic(
        "24点", "社交玩法", "24 点抢答，拼手速赢金币",
        "用 4 张牌（1-13）加减乘除凑出 24，直接发算式抢答，最先答对赢下"
        "该轮奖金。每人 1 张游戏券入场，奖池 80×人数按 5 轮均分，"
        "无人解出奖金顺延；路人喊出的正确答案会让该解法作废。",
        (
            HelpItem("24点", "开桌 / 加入已开的桌（最多 8 人）"),
            HelpItem("开始", "发起者开局（至少 2 人）"),
            HelpItem("（直接发算式）", "如「(3+5)×(1+2)」，每人每轮限 3 次"),
        ),
        requires="social",
    ),
    HelpTopic(
        "21点", "社交玩法", "Blackjack，和小波奇对赌",
        "对庄制：每人独立对抗庄家小波奇，全员并行拿牌没有回合，一个人"
        "也能玩。每人 2 张游戏券入场；胜 +200、Blackjack +250、平局退券。"
        "庄家一明一暗补到 17 点停，玩家爆牌即负。",
        (
            HelpItem("blackjack", "开桌 / 加入已开的桌（最多 8 人，单人可玩）"),
            HelpItem("blackjack 暗牌", "暗牌赛制：手牌私聊保密、互比点数"
                     "均分奖池 180×人数（≥2 人，需与 bot 为好友）"),
            HelpItem("开始", "发起者开局发牌"),
            HelpItem("拿牌 / 停牌", "补一张牌 / 停止要牌（21 点或爆牌自动停）"),
        ),
        requires="social",
    ),
    HelpTopic(
        "小猪", "社交玩法", "今日小猪占卜与找猪",
        "每天抽一只属于你的小猪，"
        "还能从 PigHub 找真猪照片（包括你这头猪）",
        (
            HelpItem("今日小猪", "抽取今天属于你的小猪，出占卜卡片"),
            HelpItem("随机小猪 [数量]", "随机猪猪照片（1~20 张，多张合并转发）"),
            HelpItem("找猪 <关键词>", "按关键词搜猪猪照片"),
        ),
    ),
    HelpTopic(
        "点赞", "社交玩法", "让波奇给你的资料卡点赞",
        "需要先和波奇成为好友；你给波奇回赞还有金币和好感度奖励。",
        (HelpItem("赞我", "给你的 QQ 资料卡点赞（直到当日限额）"),),
    ),
    # ---- 管理（红） ----
    HelpTopic(
        "群管理", "管理", "迎新文案与群内管理",
        "进群欢迎、退群提醒的文案都可以自定义。",
        (
            HelpItem("查看欢迎语", "看本群当前迎新文案"),
            HelpItem("设置欢迎语 <文案>", "自定义欢迎语，填「默认」恢复",
                     manage=True),
            HelpItem("设置退群提示 <文案>",
                     "可用 {名字}/{操作者} 占位，填「默认」恢复", manage=True),
            HelpItem("休眠 / 唤醒", "让波奇在本群暂停/恢复响应", manage=True),
            HelpItem("撤回", "回复消息+撤回", su_only=True),
        ),
    ),
    HelpTopic(
        "权限", "管理", "封禁、授权与功能开关",
        "数值等级制：群主/管理员天然有管理权限，也可以给他人授权。",
        (
            HelpItem("我的权限 / 权限 @某人", "查看生效等级"),
            HelpItem("功能列表", "看本群各功能开关状态"),
            HelpItem("封禁 @某人 [时长] / 解封 @某人", "封禁期间完全无视",
                     manage=True),
            HelpItem("授权 @某人 <等级> / 取消授权 @某人", "授予数值等级",
                     manage=True),
            HelpItem("关闭功能 <功能> / 开启功能 <功能>", "按群开关功能",
                     manage=True),
            HelpItem("封禁群 <群号> / 解封群 <群号>", "整群封禁", su_only=True),
        ),
    ),
)


def visible_topics(available: set[str] | None = None) -> list[HelpTopic]:
    """按可用服务过滤后的主题列表（编号 = 下标 + 1，按分类顺序排布）。"""
    return [
        t
        for t in TOPICS
        if t.requires is None or available is None or t.requires in available
    ]


def find_topic(
    query: str, available: set[str] | None = None
) -> tuple[int, HelpTopic] | None:
    topics = visible_topics(available)
    if query.isdigit():
        idx = int(query) - 1
        if 0 <= idx < len(topics):
            return idx + 1, topics[idx]
        return None
    for i, t in enumerate(topics):
        if t.key == query:
            return i + 1, t
    return None


# ---- 卡片 ------------------------------------------------------------------


@lru_cache(maxsize=8)
def _asset_uri(name: str) -> str:
    return image_data_uri((ASSETS / name).read_bytes())


def _band(y: float, h: float, fill: str) -> str:
    """顶部圆角、底部平直的头栏色带（与外层面板同宽）。"""
    return (
        f'<rect x="22" y="{y}" width="{W - 44}" height="{h}" rx="26"'
        f' fill="{fill}"/>'
        f'<rect x="22" y="{y + h - 30}" width="{W - 44}" height="30"'
        f' fill="{fill}"/>'
    )


def _header(usable: int, total: int, avatar: str | None) -> str:
    """顶栏：左 logo ｜ 分隔线 ｜ 右 bot 头像 + 可用功能数量。

    有功能被关闭时右上角写成「可用/总数」，一眼看出本群关了几个。
    """
    parts = [
        _band(22, 100, HEADER),
        f'<image x="36" y="20" width="150" height="104"'
        f' href="{_asset_uri("left_logo.png")}"'
        f' preserveAspectRatio="xMidYMid meet"/>',
        '<line x1="206" y1="47" x2="206" y2="97" stroke="#ffffff"'
        ' stroke-opacity="0.35" stroke-width="2"/>',
        _text(228, 82, 26, F_CR105, "Bocchi The Rock!", "#ffffff"),
    ]
    ax = W - 186
    if avatar:
        parts += [
            f'<clipPath id="help-ava"><circle cx="{ax}" cy="72" r="27"/>'
            f"</clipPath>",
            f'<circle cx="{ax}" cy="72" r="29" fill="#ffffff"/>',
            f'<image x="{ax - 27}" y="45" width="54" height="54"'
            f' href="{avatar}" clip-path="url(#help-ava)"'
            f' preserveAspectRatio="xMidYMid slice"/>',
        ]
    else:
        parts += [
            f'<circle cx="{ax}" cy="72" r="28" fill="#ffffff"/>'
            + _text(ax, 82, 26, F_CR105, "波", HEADER, anchor="middle")
        ]
    parts += [
        f'<rect x="{W - 146}" y="50" width="96" height="44" rx="10"'
        f' fill="#ffffff" fill-opacity="0.22"/>',
        _text(W - 98, 70, 18, F_SH,
              str(usable) if usable == total else f"{usable}/{total}",
              "#ffffff", anchor="middle", weight="700"),
        _text(W - 98, 88, 12, F_SH, "可用功能", "#ffffff", anchor="middle"),
    ]
    return "".join(parts)


def _footer(panel_bottom: float) -> str:
    return (
        f'<line x1="42" y1="{panel_bottom - 46}" x2="{W - 42}"'
        f' y2="{panel_bottom - 46}" stroke="#F3D7DF" stroke-width="1.5"/>'
        + _text(W / 2, panel_bottom - 20, 14, F_RXK, "Power by Botcchi",
                "#C78BA5", anchor="middle")
    )


def _wrap_svg(h: float, parts: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}"'
        f' viewBox="0 0 {W} {h}" font-family="sans-serif">'
        + _defs(STOPS)
        + "".join(parts)
        + "</svg>"
    )


COL_W = (W - 84 - 16) / 2
ROW_H = 56
CAT_PAD = 46


def _strike(x: float, baseline: float, size: float, text: str) -> str:
    """穿过文字中部的横线（resvg 不保证支持 text-decoration，自己画）。"""
    ly = baseline - size * 0.3
    return (
        f'<line x1="{x - 2}" y1="{ly}" x2="{x + text_width(text, size) + 2}"'
        f' y2="{ly}" stroke="{OFF_LINE}" stroke-width="1.8"'
        f' stroke-linecap="round"/>'
    )


def _category_block(
    theme: CategoryTheme,
    entries: list[tuple[int, HelpTopic]],
    x: float,
    y: float,
    disabled: frozenset[str] = frozenset(),
) -> tuple[str, float]:
    """一个分类色块，返回 (svg, 高度)。被关闭的条目置灰划掉。"""
    h = CAT_PAD + len(entries) * ROW_H + 10
    usable = sum(1 for _, t in entries if t.key not in disabled)
    parts = [
        f'<rect x="{x}" y="{y}" width="{COL_W}" height="{h}" rx="16"'
        f' fill="{theme.tint}" stroke="{theme.accent}" stroke-opacity="0.35"'
        f' stroke-width="1.4"/>',
        f'<rect x="{x}" y="{y}" width="6" height="{h}" rx="3"'
        f' fill="{theme.accent}"/>',
        _text(x + 22, y + 31, 19, F_JCYT, theme.name, theme.dark,
              weight="700"),
        f'<rect x="{x + COL_W - 52}" y="{y + 14}" width="32" height="22"'
        f' rx="11" fill="{theme.accent}" fill-opacity="0.9"/>'
        + _text(x + COL_W - 36, y + 30, 13, F_SH, str(usable),
                "#ffffff", anchor="middle", weight="700"),
    ]
    for i, (number, topic) in enumerate(entries):
        ry = y + CAT_PAD + i * ROW_H
        off = topic.key in disabled
        dot = OFF_DOT if off else theme.accent
        parts += [
            f'<circle cx="{x + 32}" cy="{ry + 24}" r="13" fill="{dot}"/>'
            + _text(x + 32, ry + 29, 13, F_SH, str(number), "#ffffff",
                    anchor="middle", weight="700"),
            _text(x + 54, ry + 22, 17, F_JCYT, topic.key,
                  OFF_TEXT if off else theme.dark),
            _text(x + 54, ry + 43, 12.5, F_RXK,
                  _fit(topic.summary, 12.5, COL_W - 76),
                  OFF_TEXT if off else SUB),
        ]
        if off:
            parts.append(_strike(x + 54, ry + 22, 17, topic.key))
    return "".join(parts), h


def build_menu_card(
    topics: list[HelpTopic],
    avatar: str | None = None,
    disabled: frozenset[str] = frozenset(),
) -> str:
    """主菜单卡；disabled 为本群关闭的功能名（仍列出但置灰划掉）。"""
    numbered = list(enumerate(topics, start=1))
    grouped = [
        (theme, [(n, t) for n, t in numbered if t.category == theme.name])
        for theme in CATEGORIES
    ]
    grouped = [(theme, entries) for theme, entries in grouped if entries]

    # 提示条
    tips_y, tips_h = 138.0, 40.0
    grid_y = tips_y + tips_h + 16
    # 两列瀑布流：每个分类进当前较矮的一列
    cols = [grid_y, grid_y]
    blocks: list[tuple[CategoryTheme, list, float, float]] = []
    for theme, entries in grouped:
        col = 0 if cols[0] <= cols[1] else 1
        x = 42 + col * (COL_W + 16)
        blocks.append((theme, entries, x, cols[col]))
        cols[col] += CAT_PAD + len(entries) * ROW_H + 10 + 14

    content_bottom = max(cols) - 14
    panel_bottom = content_bottom + 66
    h = panel_bottom + 22

    tips = "发送「帮助 编号」或「帮助 功能名」查看详情 · 部分指令需管理权限"

    parts = [
        f'<rect width="{W}" height="{h}" fill="url(#bg-grad)"/>',
        f'<rect x="22" y="22" width="{W - 44}" height="{panel_bottom - 22}"'
        f' rx="26" fill="#ffffff" fill-opacity="0.95"'
        f' filter="url(#panel-shadow)"/>',
        _header(
            sum(1 for t in topics if t.key not in disabled), len(topics), avatar
        ),
        f'<rect x="42" y="{tips_y}" width="{W - 84}" height="{tips_h}"'
        f' rx="10" fill="{HEADER}" fill-opacity="0.1"/>',
        f'<circle cx="{62}" cy="{tips_y + 20}" r="8" fill="{HEADER}"/>'
        + _text(62, tips_y + 24.5, 11, F_SH, "?", "#ffffff", anchor="middle",
                weight="700"),
        _text(80, tips_y + 25, 13.5, F_JCYT, _fit(tips, 13.5, W - 138),
              "#B06A80"),
    ]
    for theme, entries, x, y in blocks:
        parts.append(_category_block(theme, entries, x, y, disabled)[0])
    parts.append(_footer(panel_bottom))
    # 右下角粉色章鱼（压在面板边缘上）
    parts.append(
        f'<image x="{W - 196}" y="{h - 188}" width="160" height="152"'
        f' href="{_asset_uri("footer_char.png")}"'
        f' preserveAspectRatio="xMidYMid meet"/>'
    )
    return _wrap_svg(h, parts)


ITEM_H = 64


def build_topic_card(number: int, topic: HelpTopic) -> str:
    theme = category_theme(topic.category)
    items = [i for i in topic.items if not i.su_only]
    # 说明按需换行：决斗/24点/21点的规则三四行才讲得完，截断就没意义了
    intro = _wrap(topic.intro, 15, W - 104, max_lines=5)
    top = 128 + len(intro) * 22
    content_bottom = top + len(items) * (ITEM_H + 10)
    panel_bottom = content_bottom + 60
    h = panel_bottom + 22
    parts = [
        f'<rect width="{W}" height="{h}" fill="url(#bg-grad)"/>',
        f'<rect x="22" y="22" width="{W - 44}" height="{panel_bottom - 22}"'
        f' rx="26" fill="#ffffff" fill-opacity="0.95"'
        f' filter="url(#panel-shadow)"/>',
        _band(22, 96, theme.accent),
        '<circle cx="72" cy="70" r="22" fill="#ffffff"/>'
        + _text(72, 78, 22, F_SH, str(number), theme.accent,
                anchor="middle", weight="700"),
        _text(108, 82, 32, F_CR105, topic.key, "#ffffff"),
        _text(W - 52, 78, 15, F_JCYT, theme.name, "#ffffff", anchor="end"),
        *(
            _text(52, 140 + i * 22, 15, F_JCYT, line, SUB)
            for i, line in enumerate(intro)
        ),
    ]
    for i, item in enumerate(items):
        y = top + 12 + i * (ITEM_H + 10)
        parts.append(
            f'<rect x="42" y="{y}" width="{W - 84}" height="{ITEM_H}" rx="14"'
            f' fill="{theme.tint}" stroke="{theme.accent}"'
            f' stroke-opacity="0.3" stroke-width="1.2"/>'
        )
        parts.append(_text(66, y + 27, 19, F_JCYT, item.usage, theme.dark))
        parts.append(
            _text(66, y + 51, 14, F_RXK, _fit(item.desc, 14, W - 280), SUB)
        )
        if item.manage:
            bw = 92
            parts.append(
                f'<rect x="{W - 68 - bw}" y="{y + ITEM_H / 2 - 13}"'
                f' width="{bw}" height="26" rx="13"'
                f' fill="{theme.accent}" fill-opacity="0.16"'
                f' stroke="{theme.accent}" stroke-opacity="0.55"'
                f' stroke-width="1"/>'
                + _text(W - 68 - bw / 2, y + ITEM_H / 2 + 6, 13, F_SH,
                        "需管理权限", theme.dark, anchor="middle")
            )
    parts.append(_footer(panel_bottom))
    return _wrap_svg(h, parts)


def menu_text(
    topics: list[HelpTopic], disabled: frozenset[str] = frozenset()
) -> str:
    numbers = {t.key: i for i, t in enumerate(topics, start=1)}
    lines = []
    for theme in CATEGORIES:
        group = [t for t in topics if t.category == theme.name]
        if not group:
            continue
        lines.append(f"◆ {theme.name}")
        for t in group:
            off = "（本群已关闭）" if t.key in disabled else ""
            lines.append(f"{numbers[t.key]}. {t.key} — {t.summary}{off}")
    usable = sum(1 for t in topics if t.key not in disabled)
    return (
        f"📖 波奇的帮助菜单（{usable} 个可用功能）\n" + "\n".join(lines)
        + "\n发送「帮助 编号」或「帮助 功能名」看详细用法"
    )


def topic_text(topic: HelpTopic) -> str:
    lines = [
        f"  {i.usage} — {i.desc}" + ("（需管理权限）" if i.manage else "")
        for i in topic.items
        if not i.su_only
    ]
    return f"📖 {topic.key}：{topic.intro}\n" + "\n".join(lines)


def build_help_commands(
    helper: "HelpService | None",
    available: set[str],
    perms: "PermissionGate | None" = None,
) -> list:
    """「帮助/菜单/help」主菜单卡与「帮助 <编号/名称>」详情卡指令。"""
    from .commands import Command, exact, prefix
    from .render import to_onebot_file

    async def blocked_here(incoming) -> frozenset[str]:
        """本群被关闭的功能名（私聊无功能开关）。"""
        if perms is None or incoming.session.chat_type != "group":
            return frozenset()
        state = await perms.group_state(incoming.session.chat_id)
        return state.all_blocked

    async def send_menu(incoming, channel, rest: str) -> bool:
        topics = visible_topics(available)
        disabled = await blocked_here(incoming)
        if helper is not None:
            try:
                png = await helper.menu_png(
                    available, bot_id=incoming.self_id, disabled=disabled
                )
                await channel.send_image(incoming.session, to_onebot_file(png))
                return True
            except Exception:
                logger.exception("帮助菜单卡片失败，退化为文本")
        await channel.send_text(
            incoming.session, menu_text(topics, disabled)
        )
        return True

    async def send_topic(incoming, channel, arg: str) -> bool:
        found = find_topic(arg, available)
        if found is None:
            if arg.isdigit():
                await channel.send_text(
                    incoming.session, "没有这个编号哦，发送「帮助」看菜单"
                )
                return True
            # 「帮助我…」这类不是查功能的说法，交给闲聊
            return False
        number, topic = found
        if helper is not None:
            try:
                png = await helper.topic_png(number, topic)
                await channel.send_image(incoming.session, to_onebot_file(png))
                return True
            except Exception:
                logger.exception("功能帮助卡片失败，退化为文本")
        await channel.send_text(incoming.session, topic_text(topic))
        return True

    return [
        Command(exact("帮助", "菜单", "help", lower=True), send_menu),
        Command(prefix("帮助"), send_topic),
    ]


class HelpService:
    """帮助出图：与签到卡同源字体，内容稳定走出图缓存。"""

    def __init__(self, renderer: SvgRenderer, cache: RenderCache | None = None):
        settings = renderer.settings.model_copy(deep=True)
        settings.font_dirs = [*settings.font_dirs, str(FONT_DIR)]
        self.renderer = SvgRenderer(settings)
        self.cache = cache
        self._avatars: dict[str, str | None] = {}

    async def _render(self, svg: str) -> bytes:
        if self.cache is not None:
            return await self.cache.render(self.renderer, svg)
        return await self.renderer.render(svg)

    async def _bot_avatar(self, bot_id: str | None) -> str | None:
        """顶栏 bot 头像（data URI，进程内缓存；失败画占位）。"""
        if not bot_id or not bot_id.isdigit():
            return None
        if bot_id in self._avatars:
            return self._avatars[bot_id]
        avatar = None
        try:
            # QQ 头像为国内直连资源，绕开 http(s)_proxy 环境变量
            async with httpx.AsyncClient(
                timeout=8, trust_env=False, follow_redirects=True
            ) as client:
                resp = await client.get(QQ_AVATAR_URL.format(bot_id))
                resp.raise_for_status()
            mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            avatar = image_data_uri(resp.content, mime)
        except Exception as e:
            logger.warning(f"bot 头像获取失败 {bot_id}: {e}")
        self._avatars[bot_id] = avatar
        return avatar

    async def menu_png(
        self,
        available: set[str] | None = None,
        bot_id: str | None = None,
        disabled: frozenset[str] = frozenset(),
    ) -> bytes:
        avatar = await self._bot_avatar(bot_id)
        return await self._render(
            build_menu_card(visible_topics(available), avatar, disabled)
        )

    async def topic_png(self, number: int, topic: HelpTopic) -> bytes:
        return await self._render(build_topic_card(number, topic))
