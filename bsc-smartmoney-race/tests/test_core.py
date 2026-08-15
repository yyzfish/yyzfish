"""核心单元测试。跑法：PYTHONPATH=. python3 -m pytest tests -q（或直接 python3 tests/test_core.py）"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from smrace.config import Config
from smrace.models import Flow, FlowKind, RaceEntry, Side, TokenMeta, Trade
from smrace.pnl.engine import compute_positions
from smrace.race.engine import rank_entries
from smrace.scoring.elo_mmr import EloMMR
from smrace.scoring.gates import (bhy_adjust, estimate_pi0, expected_max_sharpe,
                                  permutation_noise_floor, race_zscore)


def _t(block, wallet, token, side, base, quote_amt, li=0, gas=0.0, ok=True):
    return Trade(ts=block, block=block, tx_hash=f"0x{block}", log_index=li,
                 wallet=wallet, token=token, quote="0xq", side=side,
                 base_amount=base, quote_amount=quote_amt, quote_usd=1.0,
                 gas_usd=gas, success=ok)


def test_weighted_average_cost_basis():
    """Nansen 官方示例：$10 买 2 个 → 卖 1 个 → $100 买 1 个 ⇒ 新成本基准 $55。"""
    trades = [
        _t(1, "w", "T", Side.BUY, 2.0, 20.0, li=0),    # 2 个 @ $10
        _t(2, "w", "T", Side.SELL, 1.0, 10.0, li=0),   # 卖 1 个 @ $10 → realized 0
        _t(3, "w", "T", Side.BUY, 1.0, 100.0, li=0),   # 1 个 @ $100
    ]
    pos = compute_positions(trades)[("w", "T")]
    assert abs(pos.realized_pnl) < 1e-9, pos.realized_pnl
    assert abs(pos.qty_open - 2.0) < 1e-9
    assert abs(pos.cost_open / pos.qty_open - 55.0) < 1e-9, pos.cost_open


def test_transfer_in_is_marked_not_zero_cost():
    """转入按市价记成本，且不计入「有对价流入」—— 否则老鼠仓显示为无限 ROI。"""
    trades = [_t(5, "w", "T", Side.SELL, 100.0, 500.0)]
    flows = [Flow(ts=1, block=1, tx_hash="0xa", wallet="w", token="T",
                  amount=100.0, kind=FlowKind.AIRDROP, mark_usd=4.0)]
    pos = compute_positions(trades, flows)[("w", "T")]
    assert abs(pos.realized_pnl - 100.0) < 1e-9, pos.realized_pnl   # 500 − 400
    assert pos.cost_coverage < 0.8, pos.cost_coverage               # 成本基准不可信


def test_failed_trade_burns_gas_only():
    trades = [_t(1, "w", "T", Side.BUY, 1.0, 10.0, gas=0.5, ok=False)]
    pos = compute_positions(trades)[("w", "T")]
    assert pos.gas_usd == 0.5 and pos.qty_open == 0.0 and pos.n_trades == 0


def test_lp_events_are_pnl_neutral():
    flows = [Flow(ts=1, block=1, tx_hash="0x", wallet="w", token="T",
                  amount=-1000.0, kind=FlowKind.LP_NEUTRAL, mark_usd=1.0)]
    pos = compute_positions([_t(1, "w", "T", Side.BUY, 1000.0, 1000.0)], flows)[("w", "T")]
    assert abs(pos.realized_pnl) < 1e-9


def test_rank_entries_does_not_collapse_everyone_into_first():
    """连续分布的收益率不能因为相邻间距小就全部并列 —— 这是会毁掉整个评分的坑。"""
    entries = [RaceEntry(entity=f"e{i}", ret=1.0 - 0.02 * i, net_pnl=0, invested_usd=1)
               for i in range(30)]
    ranked = rank_entries(entries, draw_margin=0.05)
    assert len({r for _e, r in ranked}) > 5, "并列组过多，说明用了相邻间距判定"
    assert ranked[0][1] == 0 and ranked[-1][1] > 0


def test_race_zscore_null_is_calibrated():
    """无技能选手的名次分位 ~ U(0,1)，z 应接近标准正态。"""
    rng = np.random.default_rng(0)
    zs = [race_zscore(rng.random(40))[0] for _ in range(4000)]
    assert abs(float(np.mean(zs))) < 0.1
    assert 0.9 < float(np.std(zs)) < 1.1


def test_bhy_is_monotone_and_bounded():
    p = [1e-9, 1e-4, 0.01, 0.3, 0.9]
    adj = bhy_adjust(p)
    assert all(0 <= x <= 1 for x in adj)
    assert list(adj) == sorted(adj), "校正后 p 值必须保持单调"
    assert adj[0] > p[0], "校正必须收紧，不能放松"


def test_pi0_recovers_all_null_population():
    rng = np.random.default_rng(1)
    assert estimate_pi0(rng.random(20000)) > 0.95   # 全零 alpha → π₀ ≈ 1


def test_noise_floor_tolerates_zero_race_entities():
    """真实数据里有实体 0 场有效比赛（全部比赛参与人数不足）。
    2026-08-15 首次真跑时它们让噪音基准线除零崩掉 —— 必须被静默剔除。"""
    with_zeros = permutation_noise_floor([0, 0, 12, 30, 25], n_trials=5, B=50, seed=7)
    without = permutation_noise_floor([12, 30, 25], n_trials=5, B=50, seed=7)
    assert math.isfinite(with_zeros) and with_zeros == without, (with_zeros, without)
    assert permutation_noise_floor([0, 0], n_trials=2, B=50) == 0.0


def test_expected_max_sharpe_grows_with_trials():
    a = expected_max_sharpe(0.04, 100)
    b = expected_max_sharpe(0.04, 10000)
    assert 0 < a < b, (a, b)


def test_elo_mmr_ranks_consistent_winner_first():
    elo = EloMMR()
    for _ in range(25):
        elo.run_race([("strong", 0), ("mid", 1), ("weak", 2)])
    lb = elo.leaderboard()
    assert [r["entity"] for r in lb] == ["strong", "mid", "weak"], lb
    assert all(math.isfinite(r["display"]) for r in lb)


def test_elo_mmr_uncertainty_penalises_small_sample():
    """两个都全胜，但场次少的展示分必须更低 —— 「4 笔交易 100% 胜率不是信号」。"""
    a, b = EloMMR(), EloMMR()
    for _ in range(3):
        a.run_race([("rookie", 0), ("f1", 1), ("f2", 2)])
    for _ in range(40):
        b.run_race([("veteran", 0), ("f1", 1), ("f2", 2)])
    r = a.ratings["rookie"]
    v = b.ratings["veteran"]
    assert v.display > r.display, (v.display, r.display)


def test_elo_mmr_robust_to_one_lucky_blowout():
    """一次极端好成绩不应把评分推到与长期稳定者同级（log-cosh 鲁棒平均）。"""
    elo = EloMMR()
    field = [(f"f{i}", i + 1) for i in range(20)]
    for _ in range(30):
        elo.run_race([("steady", 0)] + field)
    elo.run_race([("lucky", 0)] + [(f"f{i}", i + 1) for i in range(20)])
    assert elo.ratings["steady"].display > elo.ratings["lucky"].display


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
    print(f"运行 {len(fns)} 个测试：")
    ok = sum(_run(f) for f in fns)
    print(f"{ok}/{len(fns)} 通过")
    sys.exit(0 if ok == len(fns) else 1)
