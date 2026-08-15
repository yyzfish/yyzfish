"""赛马引擎：把链上行为映射成「比赛」。

映射设计（这是整个框架的核心）：
    一场比赛 = 一个代币 × 一个时间窗（如「代币 T 的前 24 小时」）
    参赛选手 = 窗口内对 T 建立过仓位的所有实体
    名次     = 该实体在 T 上的净收益率降序

为什么这样映射：名次是**同一代币内的横截面比较**，因此自动剔除了代币本身的
beta。memecoin 牛市里所有人都赚、熊市里所有人都亏 —— 横截面排名天然中性化了
这个周期因子。这比绝对 PnL 榜单强得多，也是「赛马」相比「排行榜」的本质优势。

平局处理：收益率差 < draw_margin_ret 视为平局（对应 TrueSkill 的 draw margin），
避免把 +12% 和 +11.5% 判成有意义的胜负。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from ..config import RaceConfig
from ..constants import BLOCK_TIME_SEC
from ..models import Race, RaceEntry, Side, TokenMeta, Trade
from ..scoring.elo_mmr import EloMMR


def build_races(
    trades: Iterable[Trade],
    tokens: Mapping[str, TokenMeta],
    cfg: RaceConfig,
    addr_to_entity: Mapping[str, str] | None = None,
    marks_by_window: Mapping[tuple[str, int], float] | None = None,
) -> list[Race]:
    """按 (代币, 窗口) 切分比赛，窗口内独立结算每个实体的收益率。

    注意：窗口内的未平仓部分按窗口末市价估值（marks_by_window），缺失则按
    窗口内最后一笔成交价估值 —— 保守起见不会凭空造浮盈。
    """
    a2e = addr_to_entity or {}
    marks_by_window = marks_by_window or {}
    win_blocks = int(cfg.window_hours * 3600 / BLOCK_TIME_SEC)

    by_token: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.success:
            by_token[t.token].append(t)

    races: list[Race] = []
    for tok, ts in by_token.items():
        ts.sort(key=lambda x: (x.block, x.log_index))
        meta = tokens.get(tok)
        t0 = meta.launch_block if (meta and meta.launch_block) else ts[0].block

        for w in range(cfg.max_windows_per_token):
            lo = t0 + w * win_blocks
            hi = lo + win_blocks
            chunk = [t for t in ts if lo <= t.block < hi]
            if not chunk:
                continue

            last_price = chunk[-1].exec_price
            mark = marks_by_window.get((tok, w), last_price)

            books: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])  # qty, cost, realized
            for t in chunk:
                e = a2e.get(t.wallet, t.wallet)
                b = books[e]
                if t.side is Side.BUY:
                    b[0] += t.base_amount
                    b[1] += t.usd
                else:
                    if b[0] > 1e-18:
                        q = min(t.base_amount, b[0])
                        avg = b[1] / b[0]
                        b[2] += t.usd * (q / t.base_amount if t.base_amount else 1.0) - avg * q
                        b[1] -= avg * q
                        b[0] -= q
                    else:
                        b[2] += t.usd  # 窗口外建仓的卖出，成本不在本窗口内

            entries: list[RaceEntry] = []
            for e, (qty, cost, realized) in books.items():
                invested = cost + realized  # 近似：本窗口内的资金投入规模
                gross_in = sum(t.usd for t in chunk
                               if a2e.get(t.wallet, t.wallet) == e and t.side is Side.BUY)
                if gross_in < cfg.min_invested_usd:
                    continue
                unreal = qty * mark - cost if mark > 0 else 0.0
                net = realized + unreal
                entries.append(RaceEntry(entity=e, ret=net / gross_in,
                                         net_pnl=net, invested_usd=gross_in))

            if len(entries) < cfg.min_participants:
                continue
            races.append(Race(race_id=f"{tok}@{w}", token=tok,
                              start_block=lo, end_block=hi, entries=entries))
    return races


def rank_entries(entries: Sequence[RaceEntry], draw_margin: float) -> list[tuple[str, int]]:
    """按收益率降序排名，与所在并列组**组首**差距 < draw_margin 的判为并列。

    ⚠️ 这里有一个极易踩的坑：如果用「与前一名的间距」判并列，那么当一场比赛里
    收益率连续分布时（memecoin 里非常常见 —— 大家都亏 55%~65%），相邻间距
    永远小于阈值，会导致**全场并列第一**，名次信息完全丢失，Elo 退化成噪音。
    必须与组首比较，让并列组的宽度有上界。
    """
    s = sorted(entries, key=lambda e: -e.ret)
    ranked: list[tuple[str, int]] = []
    rank = 0
    anchor = s[0].ret if s else 0.0
    for i, e in enumerate(s):
        if i > 0 and (anchor - e.ret) >= draw_margin:
            rank, anchor = i, e.ret
        ranked.append((e.entity, rank))
    return ranked


def rank_percentiles(races: Sequence[Race], draw_margin: float,
                     min_participants: int = 5) -> dict[str, list[float]]:
    """每个实体在各场比赛中的名次分位（1.0 = 第一名，0.0 = 末名）。

    这是序数闸门的输入。它的最大优点是**零假设已知**：若一个实体毫无技能，
    其名次分位应服从 Uniform(0,1)，均值 0.5、方差 1/12。有了已知零假设方差，
    多重检验校正和噪音基准线才有可靠依据 —— 而 Sharpe 的横截面方差会被
    「系统性亏钱的赌徒」污染，不能直接拿来当零假设方差用。
    """
    out: dict[str, list[float]] = defaultdict(list)
    for r in races:
        ranked = rank_entries(r.entries, draw_margin)
        n = len(ranked)
        if n < min_participants:
            continue
        for e, rk in ranked:
            out[e].append(1.0 - rk / (n - 1))
    return dict(out)


def run_season(races: Sequence[Race], elo: EloMMR, cfg: RaceConfig) -> dict:
    """按时间顺序跑完整个赛季。比赛顺序很重要 —— Elo-MMR 的时间扩散 γ² 依赖它，
    这也是系统对抗 alpha 衰减的机制：长期不参赛的选手不确定性会自动上升。"""
    ordered = sorted(races, key=lambda r: r.start_block)
    n_ok = 0
    for race in ordered:
        ranked = rank_entries(race.entries, cfg.draw_margin_ret)
        if len(ranked) >= cfg.min_participants:
            elo.run_race(ranked)
            n_ok += 1
    return {
        "n_races_total": len(ordered),
        "n_races_scored": n_ok,
        "n_entities": len(elo.ratings),
        "avg_races_per_entity": (
            sum(r.n_races for r in elo.ratings.values()) / len(elo.ratings)
            if elo.ratings else 0.0
        ),
    }
