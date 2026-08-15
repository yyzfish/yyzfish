"""基数指标层。

⚠️ memecoin 收益分布是极端右偏 + 厚尾（大部分 −100%，少数 +10000%），
Sharpe 在这种分布上几乎没有意义（σ 被右尾支配）。
优先看 Profit Factor 和「对数收益的 t 统计量」；Sortino 稍好但 −100% 是硬下界，
下行方差同样失真。所有指标都必须配 bootstrap 置信区间才能用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class Metrics:
    n: int = 0
    mean_ret: float = 0.0
    std_ret: float = 0.0
    sharpe: float = 0.0          # 仅供对标，memecoin 上不可单独采信
    sortino: float = 0.0
    calmar: float = 0.0
    max_dd: float = 0.0
    profit_factor: float = 0.0
    hit_rate: float = 0.0
    kelly: float = 0.0
    t_stat: float = 0.0
    skew: float = 0.0
    kurt: float = 3.0            # 非超额峰度
    ci_low: float = 0.0          # 均值收益的 bootstrap 置信下界
    ci_high: float = 0.0
    min_trl: float = float("inf")  # 还需要多少笔交易才能证明自己


def _moments(x: np.ndarray) -> tuple[float, float]:
    n = len(x)
    if n < 3:
        return 0.0, 3.0
    m, s = float(x.mean()), float(x.std(ddof=0))
    if s < 1e-12:
        return 0.0, 3.0
    z = (x - m) / s
    return float((z ** 3).mean()), float((z ** 4).mean())


def compute_metrics(
    returns: list[float] | np.ndarray,
    tokens: list[str] | None = None,
    equity_curve: list[float] | None = None,
    B: int = 2000,
    alpha: float = 0.05,
    seed: int = 42,
    sr_benchmark: float = 0.0,
) -> Metrics:
    """returns 建议传「每个代币仓位的净收益率」，tokens 传对应代币地址。

    置信区间用 **按代币分块的 cluster bootstrap**，而不是按时间分块 ——
    本场景最大的相关性来源是「同一代币上的所有交易」，对代币重采样才能正确
    暴露「这个地址的全部利润其实只来自 1 个代币」这一情形。
    """
    r = np.asarray(list(returns), dtype=float)
    m = Metrics(n=len(r))
    if m.n == 0:
        return m

    m.mean_ret = float(r.mean())
    m.std_ret = float(r.std(ddof=1)) if m.n > 1 else 0.0
    m.hit_rate = float((r > 0).mean())
    m.skew, m.kurt = _moments(r)

    if m.std_ret > 1e-12:
        m.sharpe = m.mean_ret / m.std_ret
        m.t_stat = m.mean_ret / (m.std_ret / math.sqrt(m.n))

    down = r[r < 0]
    dstd = float(np.sqrt((down ** 2).mean())) if len(down) else 0.0
    m.sortino = m.mean_ret / dstd if dstd > 1e-12 else 0.0

    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    m.profit_factor = float(gains / losses) if losses > 1e-12 else (float("inf") if gains > 0 else 0.0)

    # Kelly：离散两点近似 f* = p − (1−p)/b
    p = m.hit_rate
    avg_win = float(r[r > 0].mean()) if (r > 0).any() else 0.0
    avg_loss = float(-r[r < 0].mean()) if (r < 0).any() else 0.0
    b = avg_win / avg_loss if avg_loss > 1e-12 else 0.0
    m.kelly = float(p - (1 - p) / b) if b > 1e-12 else 0.0

    eq = np.asarray(equity_curve, dtype=float) if equity_curve else np.cumsum(r) + 1.0
    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 1e-12, (peak - eq) / peak, 0.0)
    m.max_dd = float(dd.max()) if len(dd) else 0.0
    m.calmar = float(m.mean_ret * m.n / m.max_dd) if m.max_dd > 1e-9 else 0.0

    m.ci_low, m.ci_high = cluster_bootstrap_ci(r, tokens, B=B, alpha=alpha, seed=seed)
    m.min_trl = min_track_record_length(m.sharpe, m.skew, m.kurt, sr_benchmark)
    return m


def cluster_bootstrap_ci(
    r: np.ndarray,
    tokens: list[str] | None,
    B: int = 2000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float]:
    """按代币分块重采样的均值收益置信区间。CI 下界 ≤ 0 → 不进榜。"""
    n = len(r)
    if n < 3:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    if tokens and len(tokens) == n:
        uniq = sorted(set(tokens))
        idx_by_tok = {t: np.where(np.asarray(tokens) == t)[0] for t in uniq}
        keys = np.array(uniq)
        stats = np.empty(B)
        for i in range(B):
            pick = rng.choice(keys, size=len(keys), replace=True)
            sel = np.concatenate([idx_by_tok[k] for k in pick])
            stats[i] = r[sel].mean()
    else:
        stats = r[rng.integers(0, n, size=(B, n))].mean(axis=1)
    return float(np.quantile(stats, alpha / 2)), float(np.quantile(stats, 1 - alpha / 2))


def min_track_record_length(sr: float, skew: float, kurt: float, sr_star: float = 0.0,
                            z_alpha: float = 1.6449) -> float:
    """Bailey & López de Prado 的 MinTRL：还需要多少笔交易才能在 95% 置信下
    证明 SR > SR*。适合作为榜单上的一个字段：「置信所需剩余交易数」。"""
    denom = sr - sr_star
    if denom <= 1e-9:
        return float("inf")
    adj = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    adj = max(adj, 1e-6)
    return 1.0 + adj * (z_alpha / denom) ** 2
