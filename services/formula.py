"""平台公式纯函数复刻（设计 §5.6：复刻平台 core/ 纯函数 + 黄金用例锁定）。

对照源（只读参考，绝不改动平台仓）：
* ratingLevel / abilityLevel（十档 + youngBlendAge）
  → WHL-club-operations-platform src/core/negotiation-rules.ts:17-34
* listingPriceBounds（挂牌合法区间）
  → src/core/market-rules.ts:24-38
* activationFee（保护期倍数；训练营固定 5m）
  → src/core/bypass-rules.ts:47-51 + market-rules.ts:8
* expectedWage（谈判预期工资，只做工资信号、不进价格）
  → src/worker/negotiation-secret.ts:1-3

所有金额单位 m；round2 与平台 JS Math.round 口径一致（正数四舍五入到分位）。
"""

from __future__ import annotations

import math

# ---- 常量（平台同值）----

YOUNG_BLEND_AGE = 25  # negotiation-context.ts:80 默认成长年龄

TRAINEE_ACTIVATION_FEE = 5  # market-rules.ts:8 训练营激活固定 5m
TRAINEE_WAGE = 0.75  # negotiation-rules.ts:5 训练营双固定工资
TRAINEE_RELEASE_FEE = 5  # negotiation-rules.ts:5 训练营双固定违约金

LISTING_ABSOLUTE_FLOOR = 1  # market-rules.ts:5 挂牌绝对下限

# 设计 §5.3 文档化拟合系数（2026-11-04 快照），只作 model_params 缺失时的兜底。
WAGE_DEFAULT_PARAMS = {"a": 0.07749, "b": 0.9383, "c": 0.5711, "n": 539, "r2": 0.9152}
COMP_DEFAULT_PARAMS = {
    "a": -7.1536, "b": 4.8156, "fb": 0.5954, "mf": 0.4042, "fw": -0.1430,
    "n": 29, "r2": 0.7234,
}


def round2(x: float) -> float:
    """JS Math.round((x+ε)*100)/100 同口径（金额两位小数，正数四舍五入）。"""
    return math.floor(x * 100 + 0.5) / 100


# ---- 能力等级（§5.2）----


def rating_level(rating: float) -> int:
    """CA/PA → 十档等级（negotiation-rules.ts:17-28 逐阈值复刻）。"""
    if rating >= 93:
        return 10
    if rating >= 90:
        return 9
    if rating >= 87:
        return 8
    if rating >= 84:
        return 7
    if rating >= 80:
        return 6
    if rating >= 75:
        return 5
    if rating >= 70:
        return 4
    if rating >= 65:
        return 3
    if rating >= 60:
        return 2
    return 1


def ability_level(ca: float, pa: float, age: float, young_blend_age: int = YOUNG_BLEND_AGE) -> float:
    """能力等级：年龄 ≤ 成长年龄取 (CA 等级 + PA 等级)/2（0.5 步进），否则取 CA 等级。"""
    ca_level = rating_level(ca)
    if age <= young_blend_age:
        return (ca_level + rating_level(pa)) / 2
    return ca_level


# ---- 位置角色（§5.2）----

# 画像角色（细粒度，用于属性权重与百分位分组）
POSITION_IMAGE_ROLES: dict[str, str] = {
    "GK": "GK",
    "CB": "CB",
    "RB": "FB",
    "LB": "FB",
    "CDM": "DM",
    "CM": "CM",
    "CAM": "AM",
    "RM": "W",
    "LM": "W",
    "RW": "W",
    "LW": "W",
    "ST": "ST",
}

# FC 位置 ID → 平台位置名。无合同/非活跃球员不在 players 表，只能从
# /api/players/meta 的 PosID1 推断位置；只覆盖估价需要的常用位置。
POS_ID_POSITION: dict[int, str] = {
    0: "GK",
    3: "RB",
    5: "CB",
    7: "LB",
    10: "CDM",
    12: "RM",
    14: "CM",
    16: "LM",
    18: "CAM",
    23: "RW",
    25: "ST",
    27: "LW",
}


def meta_int(attrs: dict, key: str) -> int | None:
    """平台 meta 字段转 int；缺失/布尔/脏值返回 None。"""
    value = attrs.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def position_from_meta(attrs: dict) -> str | None:
    """meta 的 PosID1→位置；PosID1 缺失时依次看 PosID2–4。"""
    for key in ("PosID1", "PosID2", "PosID3", "PosID4"):
        pos_id = meta_int(attrs, key)
        if pos_id is not None and pos_id >= 0:
            position = POS_ID_POSITION.get(pos_id)
            if position:
                return position
    return None


# 估价角色组（§5.2：W 暂与中场同组，等 W 公开点 ≥8 再单列）
IMAGE_ROLE_TO_GROUP: dict[str, str] = {
    "GK": "GK",
    "CB": "CB",
    "FB": "FB",
    "DM": "MF",
    "CM": "MF",
    "AM": "MF",
    "W": "MF",
    "ST": "FW",
}


def image_role(position: str | None) -> str | None:
    """平台位置名 → 画像角色；未知位置返回 None（画像跳过、估价走回退）。"""
    if position is None:
        return None
    return POSITION_IMAGE_ROLES.get(position.strip())


def role_group(position: str | None) -> str | None:
    """平台位置名 → 估价角色组（GK/CB/FB/MF/FW）；未知位置返回 None。"""
    role = image_role(position)
    return IMAGE_ROLE_TO_GROUP.get(role) if role else None


# 画像关键属性（§5.2 逐角色 5–8 项，等权平均）
PROFILE_ATTRS: dict[str, tuple[str, ...]] = {
    "GK": ("gkreflexes", "gkhandling", "gkpositioning", "gkdiving", "gkkicking"),
    "CB": ("defensiveawareness", "standingtackle", "slidingtackle", "headingaccuracy",
           "strength", "jumping", "composure"),
    "FB": ("sprintspeed", "acceleration", "stamina", "crossing", "standingtackle",
           "defensiveawareness", "ballcontrol"),
    "DM": ("interceptions", "standingtackle", "defensiveawareness", "shortpassing",
           "longpassing", "stamina", "composure"),
    "CM": ("shortpassing", "longpassing", "vision", "ballcontrol", "composure",
           "stamina", "interceptions"),
    "AM": ("vision", "shortpassing", "ballcontrol", "dribbling", "agility",
           "finishing", "curve"),
    "W": ("acceleration", "sprintspeed", "dribbling", "ballcontrol", "agility",
          "crossing", "finishing"),
    "ST": ("finishing", "positioning", "shotpower", "headingaccuracy", "composure",
           "acceleration", "strength"),
}


# ---- 市场规则（§5.3/§5.6）----


def listing_price_bounds(
    release_fee: float,
    market_value: float | None = None,
    *,
    floor_rc: float = 0.5,
    floor_value: float = 0.5,
    cap_rc: float = 1.5,
) -> tuple[int, int] | None:
    """挂牌合法区间 [min, max]；market_value 生产全 NULL，传 None 即只用违约金（RC）项。

    复刻 market-rules.ts:24-38：下限 = max(1, ceil(min(0.5×违约金（RC）, 0.5×平台身价（marketValue）)))，
    上限 = floor(round2(1.5×违约金（RC）))；下限 > 上限（违约金（RC）过低）返回 None（挂牌不可行）。
    """
    if not (isinstance(release_fee, (int, float)) and math.isfinite(release_fee)) or release_fee <= 0:
        return None
    floors = [release_fee * floor_rc]
    if market_value is not None and math.isfinite(market_value) and market_value > 0:
        floors.append(market_value * floor_value)
    low = max(LISTING_ABSOLUTE_FLOOR, math.ceil(min(floors)))
    high = math.floor(round2(release_fee * cap_rc))
    if low > high:
        return None
    return low, high


def is_protected(protection_ticks: int | None, current_ticks: int) -> bool:
    """保护期内判定（bypass-rules.ts:40-42：NULL = 无保护期）。"""
    return protection_ticks is not None and current_ticks < protection_ticks


def activation_fee(release_fee: float, protection_ticks: int | None, current_ticks: int) -> float:
    """激活金额（bypass-rules.ts:47-51）：保护期内 RC≤20m → 2 倍，>20m → 1.5 倍；
    保护期外 1 倍；非正数 RC 抛 ValueError（与平台 RangeError 同语义）。
    训练营球员走 TRAINEE_ACTIVATION_FEE 常量（固定 5m），不经本函数。"""
    if not (isinstance(release_fee, (int, float)) and math.isfinite(release_fee)) or release_fee <= 0:
        raise ValueError("违约金须为正数")
    mult = 2 if release_fee <= 20 else 1.5
    if not is_protected(protection_ticks, current_ticks):
        mult = 1
    return math.floor(release_fee * mult + 0.5)  # JS Math.round 同口径（v6.37.0 取整）


def expected_wage(level: float, release_fee: float, a: float, b: float, c: float) -> float:
    """谈判预期工资（negotiation-secret.ts:1-3）：a · level^b · rc^c，两位小数。

    只用于 exp_wage / wage_gap 信号，不进价格、不反推 RC 当价值锚（§5.3）。
    """
    return round2(a * (level**b) * (release_fee**c))


def trainee_activation_cost() -> float:
    """学员参考激活成本（平台口径，2026-11-05 用户拍板）：恒为 TRAINEE_ACTIVATION_FEE。

    平台实现（activations.ts:98-101）是三元分支：askPrice = isTrainee
    ? TRAINEE_ACTIVATION_FEE : activationFee(...)——学员激活不乘 RC 倍数、不走求和，
    恒收 5m。设计 §5.3 原写的「activationFee + 5」求和口径与平台不符，已按用户裁决
    以平台为准（设计副本 285/307 行同步更正），原实现 5 + 5 = 10 作废。
    """
    return float(TRAINEE_ACTIVATION_FEE)


# ---- 年龄/潜力小幅校正（§5.3）----


def age_adjustments(age: float, ca: float, pa: float) -> list[dict]:
    """fair_mid 的年龄/潜力校正项列表 [{reason, delta}]（比例，非金额）。

    ≤21 +8%；22–23 +4%；24–29 0；30–31 −5%；≥32 −10%；
    age≤25 且 pa>ca 每点 +1%，封顶 +10%（成长溢价单列一项）。
    """
    if age <= 21:
        delta = 0.08
    elif age <= 23:
        delta = 0.04
    elif age <= 29:
        delta = 0.0
    elif age <= 31:
        delta = -0.05
    else:
        delta = -0.10
    adjustments = []
    if delta:
        adjustments.append({"reason": f"age {int(age)}", "delta": delta})
    if age <= 25 and pa > ca:
        gap_delta = min(0.10, 0.01 * (pa - ca))
        adjustments.append({"reason": f"potential gap {int(pa - ca)}", "delta": gap_delta})
    return adjustments


def apply_adjustments(mid: float, adjustments: list[dict]) -> float:
    """把校正项按比例乘到 fair_mid 上。"""
    factor = 1.0
    for item in adjustments:
        factor += item["delta"]
    return mid * factor


# ---- 名字折叠（§6 名字解析的地基，roster/player_index 的 search_name）----

# 平台 name-fold.ts FOLD_SPEC 逐项移植（2026-09-21 生产 18301 行实测的 87 个非 ASCII 字）。
# 「4 位十六进制码位=替换文本」，替换文本只允许 ASCII 小写字母或空 —— 与平台 sqlFold
# REPLACE 链同源，保证「插件侧折叠结果 ≡ 平台库侧折叠结果」（阶段 4 名字解析的前提）。
_FOLD_SPEC = """
00ad= 00c0=a 00c1=a 00c2=a 00c5=a 00c7=c 00c9=e 00cd=i 00d3=o 00d6=o 00d8=o
00dc=u 00de=th 00df=ss 00e0=a 00e1=a 00e2=a 00e3=a 00e4=a 00e5=a 00e6=ae
00e7=c 00e8=e 00e9=e 00ea=e 00eb=e 00ed=i 00ee=i 00ef=i 00f0=d 00f1=n 00f2=o
00f3=o 00f4=o 00f5=o 00f6=o 00f8=o 00f9=u 00fa=u 00fc=u 00fd=y 00fe=th 0103=a
0105=a 0106=c 0107=c 010b=c 010c=c 010d=c 010e=d 0110=d 0119=e 011b=e 011f=g
0130=i 0131=i 0137=k 013d=l 013e=l 0141=l 0142=l 0144=n 0146=n 0148=n 0151=o
0159=r 015a=s 015b=s 015e=s 015f=s 0160=s 0161=s 0163=t 0165=t 016f=u 0171=u
017a=z 017b=z 017c=z 017d=z 017e=z 0218=s 0219=s 021a=t 021b=t 0301= 0308=
""".split()

FOLD_MAP: dict[str, str] = {}
for _token in _FOLD_SPEC:
    _eq = _token.index("=")
    _key = chr(int(_token[:_eq], 16))
    if _key in FOLD_MAP:
        raise RuntimeError(f"name-fold 表项重复: {_token}")
    _value = _token[_eq + 1:]
    if not _value.isascii() or not (_value.islower() or _value == ""):
        raise RuntimeError(f"name-fold 替换文本非法: {_token}")
    FOLD_MAP[_key] = _value
del _token, _key, _value, _eq


def fold_name(raw: str) -> str:
    """查表替换 + ASCII 小写，与平台 foldName（name-fold.ts:76-88）逐字同源。

    刻意不做 NFKD / 整段 casefold / 空白压缩：SQLite lower() 只折 ASCII，
    多做一步就多一类「插件折了、平台没折」的静默漏搜（name-fold.ts:72-75 原注）。
    """
    out = []
    for ch in raw or "":
        mapped = FOLD_MAP.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        code = ord(ch)
        out.append(chr(code + 32) if 0x41 <= code <= 0x5A else ch)
    return "".join(out)


def fold_name_query(query: str) -> str:
    """查询词归一：去首尾空白 + 折叠（平台 foldNameQuery 同语义）。"""
    return fold_name((query or "").strip())
