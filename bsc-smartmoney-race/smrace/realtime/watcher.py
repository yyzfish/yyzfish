"""实时层：只跟踪离线赛马选出的头部选手，输出可执行告警。

分工（这是两层落地的关键）：
  离线层（天/周级）负责**选人** —— 重、慢、统计严谨，产出选手池。
  实时层（秒级）  负责**跟人** —— 轻、快，只监听选手池里的地址。

不要让实时层做评分。BSC 出块 0.45s ≈ 192,000 块/天，任何按区块轮询的追踪器
轮询间隔必须 < 450ms 才不丢块 —— 一律改用 WSS / gRPC 推送而非轮询。

告警分级：
  P0  main 赛道选手 + copy_factor > 0.5 + 池子够深   → 可执行
  P1  main 赛道选手，但可跟单性未验证               → 观察
  P2  sniper 赛道选手建仓                          → **只作为信号源，不跟单**
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from ..models import Side, Trade


@dataclass
class Roster:
    """选手池。由离线赛马产出，实时层只读。"""
    main: dict[str, float] = field(default_factory=dict)    # entity -> display score
    sniper: dict[str, float] = field(default_factory=dict)
    copy_factor: dict[str, float] = field(default_factory=dict)
    addr_to_entity: dict[str, str] = field(default_factory=dict)

    def lane_of(self, wallet: str) -> tuple[str, str] | None:
        e = self.addr_to_entity.get(wallet, wallet)
        if e in self.main:
            return e, "main"
        if e in self.sniper:
            return e, "sniper"
        return None


@dataclass
class Alert:
    ts: int
    level: str          # P0 / P1 / P2
    entity: str
    lane: str
    token: str
    side: str
    usd: float
    score: float
    copy_factor: float
    n_confirm: int      # 同一代币上有几个选手同时建仓 —— 共识信号远强于单点
    note: str = ""


class Watcher:
    """消费实时 swap 流，对选手池内地址产出告警。

    共识窗口：多个独立选手在短时间内买入同一代币，是比任何单点信号都强的证据。
    """

    def __init__(
        self,
        roster: Roster,
        consensus_window_sec: int = 900,
        min_usd: float = 200.0,
        sink: Callable[[Alert], None] | None = None,
    ) -> None:
        self.roster = roster
        self.window = consensus_window_sec
        self.min_usd = min_usd
        self.sink = sink or (lambda a: None)
        self._recent: dict[str, deque[tuple[int, str]]] = defaultdict(deque)
        self.alerts: list[Alert] = []

    def _consensus(self, token: str, ts: int, entity: str) -> int:
        dq = self._recent[token]
        dq.append((ts, entity))
        while dq and ts - dq[0][0] > self.window:
            dq.popleft()
        return len({e for _t, e in dq})

    def on_trade(self, t: Trade) -> Alert | None:
        if not t.success or t.usd < self.min_usd:
            return None
        hit = self.roster.lane_of(t.wallet)
        if hit is None:
            return None
        entity, lane = hit

        n = self._consensus(t.token, t.ts, entity) if t.side is Side.BUY else 0
        cf = self.roster.copy_factor.get(entity, 0.0)
        score = (self.roster.main if lane == "main" else self.roster.sniper).get(entity, 0.0)

        if lane == "sniper":
            level, note = "P2", "狙击赛道：作为信号源参考，不建议跟单"
        elif cf >= 0.5:
            level, note = "P0", f"可跟单性 {cf:.0%}"
        else:
            level, note = "P1", f"可跟单性未验证或偏低 ({cf:.0%})"

        a = Alert(ts=t.ts, level=level, entity=entity, lane=lane, token=t.token,
                  side=t.side.value, usd=t.usd, score=score, copy_factor=cf,
                  n_confirm=n, note=note)
        self.alerts.append(a)
        self.sink(a)
        return a

    def replay(self, trades: Iterable[Trade]) -> list[Alert]:
        for t in sorted(trades, key=lambda x: (x.block, x.log_index)):
            self.on_trade(t)
        return self.alerts


def roster_from_leaderboard(
    leaderboard: list[dict],
    lanes: Mapping[str, str],
    copy_factor: Mapping[str, float],
    addr_to_entity: Mapping[str, str],
    top_n: int = 50,
) -> Roster:
    r = Roster(addr_to_entity=dict(addr_to_entity), copy_factor=dict(copy_factor))
    for row in leaderboard[:top_n]:
        e = row["entity"]
        lane = lanes.get(e, "main")
        (r.sniper if lane == "sniper" else r.main)[e] = row["display"]
    return r
