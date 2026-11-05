"""估价：基线装配 + assumptions 场景重算（设计 §5.5/§5.7 已裁决 C 混合）。

* 默认（无假设）：工具层读 player_scores 基线行，assemble_value_data 装配 §5.5 形状；
* 带假设：apply_assumptions 改写球员输入 → 同一 services.scores.evaluate_player 重算
  （LLM 不做算术；模型只报数），scenario=true，假设原样写进 caveats；
* build_value_summary：summary 必须同时给「市场参考区间 + RC 值 + 可成交区间 +
  RC 状态 + 置信度」（§5.5 硬要求），任何情况下不说「官方身价」。

场景重算用的 ModelContext 由 services.scores.fit_model_context 提供（与预计算同一
拟合路径）；role 假设把估价角色组映射回代表位置（GK/CB/RB/CM/ST）走同一函数。
"""

from __future__ import annotations

from ..services.scores import ModelContext, assemble_value_data, evaluate_player

# 设计 §6：assumptions 可选键
ASSUMPTION_KEYS = ("ca_delta", "age", "pa", "budget", "role")

# 估价角色组 → 代表位置（role 假设用；走同一 evaluate_player 路径）
_GROUP_POSITION = {"GK": "GK", "CB": "CB", "FB": "RB", "MF": "CM", "FW": "ST"}

_RC_FLAG_TEXT = {
    "rc_overpriced": "违约金高于市场参考价（按 RC 买不划算）",
    "rc_suppressed": "挂牌上限低于市场参考价（违约金会压价）",
    "rc_capped": "违约金能覆盖市场参考价中段，但封住高端",
    "rc_fair": "违约金不压价",
}
_CONF_TEXT = {"medium": "中", "low": "低", "high": "高"}


def apply_assumptions(player: dict, assumptions: dict) -> tuple[dict, list[str], list[str]]:
    """把假设改写到球员输入副本上。返回 (新 player, 假设描述列表, caveats)。

    * ca_delta：在现 CA 上加减（int）；
    * age / pa：直接覆盖（int）；
    * role：估价角色组 GK/CB/FB/MF/FW → 代表位置覆盖；
    * budget 不改球员输入，由 evaluate_scenario 追加 affordability（设计 §6）。
    """
    changed = dict(player)
    desc: list[str] = []
    caveats: list[str] = []
    if "ca_delta" in assumptions and assumptions["ca_delta"] is not None:
        delta = int(assumptions["ca_delta"])
        base_ca = changed.get("ca")
        if base_ca is None:
            raise ValueError("该球员没有 CA 数据，无法套 ca_delta 假设")
        changed["ca"] = int(base_ca) + delta
        desc.append(f"CA{delta:+d}")
        caveats.append(f"假设场景：CA 按 {base_ca}{delta:+d}={changed['ca']} 重算")
    if assumptions.get("age") is not None:
        age = int(assumptions["age"])
        if age <= 0:
            raise ValueError("age 假设必须是正整数")
        old = changed.get("age")
        changed["age"] = age
        desc.append(f"age={age}")
        caveats.append(f"假设场景：年龄按 {age} 重算（现值 {old}）")
    if assumptions.get("pa") is not None:
        pa = int(assumptions["pa"])
        if pa <= 0:
            raise ValueError("pa 假设必须是正整数")
        changed["pa"] = pa
        desc.append(f"PA={pa}")
        caveats.append(f"假设场景：PA 按 {pa} 重算")
    if assumptions.get("role"):
        role = str(assumptions["role"]).strip().upper()
        if role not in _GROUP_POSITION:
            raise ValueError(f"role 假设只接受 {'/'.join(_GROUP_POSITION)}")
        changed["position"] = _GROUP_POSITION[role]
        desc.append(f"role={role}")
        caveats.append(f"假设场景：按 {role} 角色组估价（现位置 {player.get('position')}）")
    return changed, desc, caveats


def evaluate_scenario(
    player: dict, attrs: dict | None, baseline: dict, model: ModelContext, assumptions: dict
) -> dict:
    """assumptions 场景重算：同一 evaluate_player，输出基线行同形状 + scenario 标记。

    assumptions 里的 budget 不进估价，转成 data.affordability（§6 假设只对
    单球员/两人对比生效；返回 scenario:true 并把假设写进 caveats 由调用方拼）。
    """
    changed, desc, caveats = apply_assumptions(player, assumptions)
    row = evaluate_player(changed, attrs, baseline, model)
    row["scenario"] = True
    row["assumptions_desc"] = desc
    row["assumptions_caveats"] = caveats
    if changed.get("status") == "trainee":
        row["kind"] = "trainee"
    budget = assumptions.get("budget")
    if budget is not None:
        budget = float(budget)
        if budget <= 0:
            raise ValueError("budget 假设必须是正数")
        deal_high = row.get("deal_high")
        desc.append(f"budget={budget:g}m")
        caveats.append(f"假设场景：预算按 {budget:g}m 对照可成交上限")
        row["affordability"] = {
            "budget": budget,
            "deal_high": deal_high,
            "within": None if deal_high is None else bool(deal_high <= budget),
        }
    return row


def row_to_value_data(row: dict, model_meta: dict) -> dict:
    """估价行（基线或场景）→ §5.5 data 形状；场景行带 scenario/assumptions/affordability。

    额外带 role_group（估价角色组）：whl_compare_players 判断是否跨位置用。
    """
    data = assemble_value_data(row, model_meta)
    data["role_group"] = row.get("role_group")
    if row.get("scenario"):
        data["scenario"] = True
        data["assumptions"] = row.get("assumptions_desc") or []
    if row.get("affordability") is not None:
        data["affordability"] = row["affordability"]
    if row.get("kind") == "trainee":
        data["kind"] = "trainee"
    return data


def model_meta_from_params(params: dict[str, dict]) -> dict:
    """dao.read_all_model_params 形状 → assemble_value_data 需要的 {wage:{r2,...}, comp:{...}}。"""
    meta: dict[str, dict] = {}
    for key in ("wage", "comp"):
        item = params.get(key) or {}
        meta[key] = {
            "r2": item.get("r2"),
            "trained_at": item.get("trained_at"),
            "version": item.get("version"),
        }
    return meta


def model_meta_from_rows(model_rows: list[dict]) -> dict:
    """fit_model_context 的 model_rows → assemble_value_data 需要的形状（场景重算用）。"""
    meta: dict[str, dict] = {}
    for row in model_rows or []:
        meta[row["key"]] = {
            "r2": row.get("r2"),
            "trained_at": None,
            "version": None,
        }
    return meta


def build_value_summary(data: dict) -> str:
    """§5.5：市场参考价 + 违约金（RC）+ 可成交区间 + 违约金（RC）状态 + 置信度，一句话；不说「官方身价」。"""
    fair = data.get("fair") or {}
    rc = data.get("rc") or {}
    deal = data.get("deal") or {}
    if data.get("kind") == "trainee":
        cost = fair.get("mid")
        return (
            f"训练营球员（训练营合同）不给市场估价；参考激活成本 {cost:g}m（平台固定，不乘违约金（RC））。"
            if cost is not None else "训练营球员（训练营合同）不给市场估价。"
        )
    parts: list[str] = []
    if fair.get("low") is not None:
        parts.append(
            f"市场参考价 {fair['low']:g}–{fair['high']:g}m（中位 {fair['mid']:g}）"
        )
    if rc.get("value") is not None:
        parts.append(f"违约金（RC）{rc['value']:g}m")
    if deal.get("low") is not None:
        parts.append(f"可成交区间 {deal['low']:g}–{deal['high']:g}m")
    elif fair.get("low") is not None:
        parts.append("自由身/无违约金：只有市场参考，没有可成交区间")
    if deal.get("flag") == "no_listable_range":
        parts.append("挂牌下限高于市场高端，无合法挂牌区间")
    if rc.get("flag"):
        parts.append(f"违约金（RC）状态：{_RC_FLAG_TEXT.get(rc['flag'], rc['flag'])}")
    if fair.get("confidence"):
        parts.append(f"置信度{_CONF_TEXT.get(fair['confidence'], fair['confidence'])}")
    return "；".join(parts) + "。"


def baseline_caveats(fair_source: str | None, role_n: int | None) -> list[str]:
    """市场价口径 caveat（§5.4/§5.6：样本少、marketValue 全 NULL、不是官方身价）。"""
    caveats = ["市场参考价是公开价格曲线估计（挂牌/强制拍卖/转会广告板），不是官方身价，也不是同球员成交价"]
    if fair_source == "gk_outfield_0.6":
        caveats.append("GK 公开价格点不足，参考价 = 外场四组曲线均值 × 0.6（启发式，低置信度）")
    elif fair_source == "rc_fallback":
        caveats.append("该角色公开价格点 <3 个，退回 0.5–1.5 倍违约金（RC）区间作参考（低置信度）")
    if role_n is not None and 0 < role_n < 8:
        caveats.append(f"同角色公开价格样本只有 {role_n} 个，区间偏宽")
    return caveats
