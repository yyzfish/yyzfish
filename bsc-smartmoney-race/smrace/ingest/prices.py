"""报价资产的 USD 喂价。

只需要给 **quote 侧** 资产喂价（WBNB / CAKE 等），base 侧 memecoin 的价格
一律由 swap 的实际执行价推导 —— 这是 docs/02 §1.2 的核心口径，
用外部 K 线给 memecoin 定价会系统性低估滑点。

稳定币（USDT / USDC / BUSD / USD1）恒为 1.0。

gas_usd 也依赖这里：gasUsed × effectiveGasPrice × P_BNB(t)。
BNB 价格在几个月的回溯里波动很大，用固定值会让 gas 成本估错一倍以上。
"""

from __future__ import annotations

import bisect
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..constants import CAKE, STABLE_QUOTES, WBNB


@dataclass
class PriceOracle:
    """按时间戳查报价资产 USD 价。内部是有序时间序列 + 二分查找。

    数据来源随便：CoinGecko / Binance K 线 / Dune 的 prices.usd / 自己的池子快照。
    本类只负责「装进来 + 按时间查」，不绑定任何供应商。
    """
    series: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    fallback: dict[str, float] = field(default_factory=dict)
    strict: bool = False   # True 时缺价直接报错，而不是静默用 fallback

    def load_series(self, token: str, points: Iterable[tuple[int, float]]) -> None:
        pts = sorted((int(t), float(p)) for t, p in points)
        self.series[token.lower()] = pts

    def load_csv(self, token: str, path: str | Path) -> None:
        """CSV 两列：unix_ts,usd_price（允许表头）。"""
        pts: list[tuple[int, float]] = []
        for line in Path(path).read_text().splitlines():
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            try:
                pts.append((int(float(parts[0])), float(parts[1])))
            except ValueError:
                continue  # 表头
        self.load_series(token, pts)

    def price(self, token: str, ts: int) -> float:
        t = token.lower()
        if t in STABLE_QUOTES:
            return 1.0
        pts = self.series.get(t)
        if pts:
            i = bisect.bisect_right([p[0] for p in pts], ts) - 1
            if i >= 0:
                return pts[i][1]
            return pts[0][1]   # 早于序列起点，用第一个点
        if t in self.fallback:
            return self.fallback[t]
        if self.strict:
            raise KeyError(f"{token} 在 ts={ts} 没有喂价，且未配置 fallback")
        return 0.0

    def bnb(self, ts: int) -> float:
        return self.price(WBNB, ts)


def default_oracle() -> PriceOracle:
    """从环境变量装一个最小可用的 oracle。

        SMRACE_BNB_PRICE_CSV   BNB 的 ts,price CSV 路径（强烈建议提供）
        SMRACE_BNB_PRICE_FLAT  没有 CSV 时的固定 BNB 价（仅供冒烟测试）
        SMRACE_CAKE_PRICE_FLAT 同上

    ⚠️ 用固定价跑几个月的历史回溯，gas 成本和 quote 计价都会偏 ——
    只适合打通链路，正式算分务必喂真实时间序列。
    """
    o = PriceOracle()
    csv = os.getenv("SMRACE_BNB_PRICE_CSV")
    if csv and Path(csv).exists():
        o.load_csv(WBNB, csv)
    flat = os.getenv("SMRACE_BNB_PRICE_FLAT")
    if flat:
        o.fallback[WBNB] = float(flat)
    cflat = os.getenv("SMRACE_CAKE_PRICE_FLAT")
    if cflat:
        o.fallback[CAKE] = float(cflat)
    return o
