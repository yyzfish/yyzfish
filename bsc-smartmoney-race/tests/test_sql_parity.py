"""SQL 版 vs Python 版：量化「用纯 SQL 会损失多少」。

作者无法访问 Dune，所以 `dune/leaderboard.sql` 的语法没有在真实 Trino 上跑过。
但它的**语义**可以在这里精确复刻，然后在带 ground truth 的合成数据上，
和完整 Python 管线做对照 —— 这样至少能回答：

    「手机上贴个 SQL 就能用的那个简化版，到底还剩多少判别力？」

复刻的是 SQL 里的这几条简化：
  · 地址级，不做实体聚类（Sybil 分身各占一行）
  · 净现金流 PnL（sell_usd − buy_usd），不算浮盈、不做加权平均成本
  · 不接转账流（空投/老鼠仓拦不住）
  · 噪音基准线用经验值 3.7，而不是置换检验精确标定
  · 没有 Elo-MMR、没有 bootstrap CI
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from smrace.config import Config
from smrace.ingest.synthetic import SyntheticSource
from smrace.models import Side

MIN_USD = 50.0
MIN_TRADERS = 5
MIN_RACES = 10
SNIPER_BLOCKS = 3
MEV_HOLD_BLOCKS = 2
WASH_MIN_TRADES = 8          # 同一代币成交 ≥ 8 笔
WASH_MAX_ABS_RET = 0.02      # 且净收益率 < 2% → 对倒
FDR_TARGET = 0.10
Z_EMPIRICAL_FLOOR = 3.7      # SQL 版给不了置换检验，用经验阈值


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def run_sql_semantics(u) -> dict[str, dict]:
    """逐条复刻 dune/leaderboard.sql 的 CTE。"""
    # ---- legs（合成数据已经是 base/quote 视角）+ min_usd
    legs = [t for t in u.trades if t.success and t.usd >= MIN_USD]

    # ---- tok_stats / keep
    traders = defaultdict(set)
    launch = {}
    for t in legs:
        traders[t.token].add(t.wallet)
        launch[t.token] = min(launch.get(t.token, t.block), t.block)
    keep = {tok for tok, ws in traders.items() if len(ws) >= MIN_TRADERS}

    # ---- pos（净现金流口径）
    pos: dict[tuple[str, str], dict] = {}
    for t in legs:
        if t.token not in keep:
            continue
        k = (t.wallet, t.token)
        p = pos.setdefault(k, {"buy": 0.0, "sell": 0.0, "n": 0,
                               "first": t.block, "last": t.block})
        p["buy" if t.side is Side.BUY else "sell"] += t.usd
        p["n"] += 1
        p["first"] = min(p["first"], t.block)
        p["last"] = max(p["last"], t.block)

    # ---- clean：剔 bundler / MEV，标 sniper 赛道
    clean = []
    for (w, tok), p in pos.items():
        if p["buy"] <= 0:
            continue
        bd = p["first"] - launch[tok]
        hold = p["last"] - p["first"]
        if bd <= 0 or hold < MEV_HOLD_BLOCKS:
            continue
        pnl = p["sell"] - p["buy"]
        ret = pnl / p["buy"]
        # 对倒代理：成交很多但净收益≈0（CFTC 定义：无市场风险、无持仓变化）
        if p["n"] >= WASH_MIN_TRADES and abs(ret) < WASH_MAX_ABS_RET:
            continue
        clean.append({
            "wallet": w, "token": tok, "ret": ret, "pnl": pnl,
            "buy": p["buy"], "n": p["n"],
            "lane": "sniper" if bd <= SNIPER_BLOCKS else "main",
        })

    # ---- races：同一代币内横截面名次
    by_tok = defaultdict(list)
    for r in clean:
        by_tok[r["token"]].append(r)
    pct = []
    for tok, rows in by_tok.items():
        n = len(rows)
        if n < 5:
            continue
        rows.sort(key=lambda x: -x["ret"])
        for i, r in enumerate(rows):
            pct.append({**r, "percentile": 1.0 - i / (n - 1)})

    # ---- agg / z
    agg: dict[str, dict] = defaultdict(
        lambda: {"pcts": [], "pnl": 0.0, "buy": 0.0, "n": 0, "wins": 0,
                 "gain": 0.0, "loss": 0.0, "lane": "main"})
    for r in pct:
        a = agg[r["wallet"]]
        a["pcts"].append(r["percentile"])
        a["pnl"] += r["pnl"]
        a["buy"] += r["buy"]
        a["n"] += r["n"]
        a["wins"] += 1 if r["ret"] > 0 else 0
        a["gain"] += max(r["pnl"], 0.0)
        a["loss"] += max(-r["pnl"], 0.0)
        if r["lane"] == "sniper":
            a["lane"] = "sniper"

    out = {}
    for w, a in agg.items():
        nr = len(a["pcts"])
        if nr < MIN_RACES:
            continue
        z = (float(np.mean(a["pcts"])) - 0.5) / math.sqrt((1.0 / 12.0) / nr)
        out[w] = {
            "race_z": z, "n_races": nr, "net_pnl": a["pnl"], "lane": a["lane"],
            "hit_rate": a["wins"] / nr,
            "profit_factor": (a["gain"] / a["loss"]) if a["loss"] > 0 else float("inf"),
            "p_raw": 2.0 * (1.0 - _norm_cdf(abs(z))),
        }

    # ---- BHY
    order = sorted(out, key=lambda w: out[w]["p_raw"])
    M = len(order)
    cM = math.log(M) + 0.5772156649 if M > 1 else 1.0
    running = 1.0
    for i in range(M - 1, -1, -1):
        w = order[i]
        running = min(running, M * cM / (i + 1) * out[w]["p_raw"])
        out[w]["p_bhy"] = min(running, 1.0)

    for w, r in out.items():
        r["passed"] = (r["p_bhy"] <= FDR_TARGET and r["profit_factor"] > 1.2
                       and r["net_pnl"] > 0 and r["race_z"] > Z_EMPIRICAL_FLOOR)
    return out


def test_sql_version_still_separates_skilled_from_gamblers():
    u = SyntheticSource(seed=42).universe
    res = run_sql_semantics(u)
    truth = u.truth

    passed_main = {w for w, r in res.items() if r["passed"] and r["lane"] == "main"}
    by_kind = defaultdict(lambda: [0, 0])
    for w, r in res.items():
        k = truth.get(w, "?")
        by_kind[k][0] += 1
        if w in passed_main:
            by_kind[k][1] += 1

    n_skilled = by_kind["skilled"][1]
    precision = n_skilled / len(passed_main) if passed_main else 0.0
    recall = n_skilled / sum(1 for k in truth.values() if k == "skilled")

    print("\n  === 纯 SQL 版在合成数据上的表现 ===")
    for k in sorted(by_kind, key=lambda x: -by_kind[x][0]):
        tot, ok = by_kind[k]
        print(f"    {k:9s} 候选 {tot:4d}  通过 main 闸门 {ok:3d}")
    print(f"    → 精确率 {precision:.0%}   召回率 {recall:.0%}   "
          f"（main 名单 {len(passed_main)} 人）")

    # 底线要求：赌徒一个都不能混进来。榜单的价值取决于假阳性率。
    assert by_kind["gambler"][1] == 0, "有赌徒混进 main 名单，SQL 版不可用"
    assert n_skilled >= 3, f"只找到 {n_skilled} 个真高手，判别力太低"


def test_sql_version_is_weaker_than_python_version():
    """明确记录「简化的代价」，别让人以为 SQL 版等价于完整框架。"""
    from smrace.pipeline import run, validate
    u = SyntheticSource(seed=42).universe
    sql_res = run_sql_semantics(u)
    sql_main = {w for w, r in sql_res.items() if r["passed"] and r["lane"] == "main"}
    sql_skilled = sum(1 for w in sql_main if u.truth.get(w) == "skilled")

    cfg = Config()
    cfg.gate.bootstrap_B = 400
    py = run(cfg, verbose=False)
    py_v = validate(py, verbose=False)
    py_skilled = py_v["by_kind"]["skilled"]["passed_main"]

    print(f"\n  Python 完整版：{py_skilled}/8 个真高手  ·  "
          f"纯 SQL 版：{sql_skilled}/8")
    print("  SQL 版缺失：实体聚类 · 转账流 · Elo-MMR · bootstrap CI · 置换基准线 · 可跟单性回测")
    assert sql_skilled <= py_skilled + 1, "SQL 版不该反超完整版，说明复刻有误"


def _run(fn):
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {fn.__name__}: {e}")
        return False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"运行 {len(fns)} 个 SQL 对照测试：")
    ok = sum(_run(f) for f in fns)
    print(f"{ok}/{len(fns)} 通过")
    sys.exit(0 if ok == len(fns) else 1)
