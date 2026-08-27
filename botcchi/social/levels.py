"""好感度等级表：阈值、关系称谓、态度描述"""

from dataclasses import dataclass

_TIERS: list[tuple[float, str, str, str]] = [
    # (起始好感度, 关系, 态度短语[卡片展示], 对用户的态度[prompt 注入])
    (0, "路人", "排斥", "保持距离，态度冷淡疏离"),
    (5, "陌生", "陌生", "愿意搭话，但不深入"),
    (10, "初识", "可以交流", "普通同学式的客气交流"),
    (20, "普通", "值得信赖", "不亲不疏，正常相处"),
    (30, "熟悉", "普通朋友", "觉得对方靠谱，相处自然"),
    (40, "信赖", "好朋友", "关系亲近，愿意主动接触"),
    (70, "相知", "亲密朋友", "好朋友，有信任感和默契"),
    (110, "厚谊", "特别喜欢", "亲密朋友，能分享心事和小秘密"),
    (199, "亲密", "恋人", "特别喜欢，最亲密的关系"),
]


@dataclass(frozen=True, slots=True)
class FavorLevel:
    level: int
    relation: str
    mood: str
    """态度短语（签到卡片展示，沿用旧版 level2attitude）"""
    attitude: str
    floor: float
    """本级起始好感度"""
    ceiling: float | None
    """下一级所需好感度；满级为 None"""

    def progress(self, impression: float) -> float:
        """本级内进度 0.0~1.0（满级恒为 1.0）"""
        if self.ceiling is None:
            return 1.0
        span = self.ceiling - self.floor
        return max(0.0, min(1.0, (impression - self.floor) / span))

    def to_next(self, impression: float) -> float:
        return 0.0 if self.ceiling is None else max(0.0, self.ceiling - impression)


def favor_level(impression: float) -> FavorLevel:
    for i in range(len(_TIERS) - 1, -1, -1):
        floor, relation, mood, attitude = _TIERS[i]
        if impression >= floor:
            ceiling = _TIERS[i + 1][0] if i + 1 < len(_TIERS) else None
            return FavorLevel(
                level=i,
                relation=relation,
                mood=mood,
                attitude=attitude,
                floor=floor,
                ceiling=ceiling,
            )
    return favor_level(0.0)
