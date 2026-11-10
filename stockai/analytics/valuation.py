"""估值与风控计算：DCF、反向 DCF、交易计划（盈亏比/仓位）。AI 给假设，代码算数。"""
from __future__ import annotations

from typing import Any

from ..util import big, clean, num, pct


def _dcf_value(fcf0: float, g1: float, tg: float, r: float, years: int = 10, stage1: int = 5) -> float:
    """两阶段 DCF：前 stage1 年增速 g1，之后线性衰减到永续增速 tg。返回企业自由现金流现值总和。"""
    pv, fcf = 0.0, fcf0
    for t in range(1, years + 1):
        if t <= stage1:
            g = g1
        else:
            g = g1 + (tg - g1) * (t - stage1) / (years - stage1)
        fcf *= 1 + g
        pv += fcf / (1 + r) ** t
    tv = fcf * (1 + tg) / (r - tg)
    return pv + tv / (1 + r) ** years


def dcf(fcf_ttm: float, shares: float, net_cash: float, price: float,
        scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    if not fcf_ttm or fcf_ttm <= 0:
        return {"error": "自由现金流为负或缺失，DCF 不适用（可改用 PS/EV-Revenue 等相对估值）"}
    if not shares:
        return {"error": "缺少股本数据"}
    rows = []
    for sc in scenarios:
        g1 = float(sc.get("growth_5y", 0.1))
        tg = float(sc.get("terminal_growth", 0.03))
        r = float(sc.get("discount_rate", 0.09))
        if r <= tg:
            rows.append({"name": sc.get("name"), "error": "折现率必须大于永续增长率"})
            continue
        ev = _dcf_value(fcf_ttm, g1, tg, r)
        per_share = (ev + (net_cash or 0)) / shares
        rows.append({
            "name": sc.get("name", "情景"),
            "assumptions": {"fcf_growth_y1_5_%": pct(g1), "terminal_growth_%": pct(tg), "discount_rate_%": pct(r)},
            "fair_value_per_share": num(per_share),
            "upside_%": pct(per_share / price - 1) if price else None,
        })
    return clean({
        "inputs": {"fcf_ttm": big(fcf_ttm), "shares": big(shares), "net_cash": big(net_cash), "price": num(price)},
        "scenarios": rows,
        "method": "两阶段DCF：1-5年按假设增速，6-10年线性降至永续增速，第10年末取永续价值",
    })


def reverse_dcf(fcf_ttm: float, shares: float, net_cash: float, price: float,
                discount_rate: float = 0.09, terminal_growth: float = 0.03) -> dict[str, Any]:
    """当前股价隐含的未来 5 年 FCF 年增速。"""
    if not fcf_ttm or fcf_ttm <= 0 or not shares or not price:
        return {"error": "FCF 为负或数据缺失，无法做反向 DCF"}
    target_ev = price * shares - (net_cash or 0)
    lo, hi = -0.5, 1.5
    for _ in range(100):
        mid = (lo + hi) / 2
        if _dcf_value(fcf_ttm, mid, terminal_growth, discount_rate) < target_ev:
            lo = mid
        else:
            hi = mid
    return {"implied_fcf_growth_y1_5_%": pct(mid), "discount_rate_%": pct(discount_rate),
            "terminal_growth_%": pct(terminal_growth),
            "note": "含义：要让当前股价合理，未来5年自由现金流需要达到的年均增速"}


def trade_plan(entry: float, stop: float, targets: list[float], atr: float | None = None,
               account_size: float | None = None, risk_pct: float = 1.0) -> dict[str, Any]:
    if entry <= 0 or stop <= 0:
        return {"error": "价格必须为正"}
    side = "long" if stop < entry else "short"
    risk = abs(entry - stop)
    out: dict[str, Any] = {
        "side": side,
        "entry": num(entry),
        "stop": num(stop),
        "stop_distance_%": pct(risk / entry),
        "stop_distance_atr": num(risk / atr, 3) if atr else None,
        "targets": [],
    }
    for t in targets or []:
        reward = (t - entry) if side == "long" else (entry - t)
        out["targets"].append({"price": num(t), "move_%": pct(reward / entry), "r_multiple": num(reward / risk, 3)})
    if account_size:
        risk_amt = account_size * risk_pct / 100
        qty = risk_amt / risk
        out["position"] = {
            "risk_amount": num(risk_amt), "quantity": num(qty, 4), "notional": num(qty * entry),
            "notional_pct_of_account": pct(qty * entry / account_size),
            "rule": f"单笔最多亏损账户的 {risk_pct}%",
        }
    if atr and risk / atr < 0.8:
        out["warning"] = "止损距离不到 1 倍 ATR，容易被正常波动扫掉"
    return clean(out)
