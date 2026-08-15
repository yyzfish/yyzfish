"""数据源工厂 + RPC 骨架。

Bitquery / Dune 的完整实现分别在 `bitquery.py` / `dune.py`。
选型对比与成本区间见 docs/01。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .bitquery import BitquerySource            # noqa: F401  对外保持同名导入
from .dune import DuneSource                    # noqa: F401
from .synthetic import SyntheticSource          # noqa: F401


@dataclass
class RpcSource:
    """自建 / 第三方 RPC + eth_getLogs。最低延迟、无速率上限，但要自己扛索引。

    ⚠️ Fermi 后 BSC 出块 0.45s ≈ 192,000 块/天。eth_getLogs 的区块跨度要按这个
    量级切片，否则必然超时；实时链路一律用 WSS 订阅，不要轮询。
    ⚠️ 历史 PnL 回溯需要 archive 节点（官方最低 5TB，Erigon 从零同步约 3 天、
    占用约 4.3TB）。Fast Node 的 `debug_trace*` 只保留 3 天，只够做实时监控。
    ⚠️ QuickNode 的 "Archive" 在 Enterprise 以下只保留 1 天日志 —— 不是真 archive。
    """
    name: str = "rpc"
    http_url: str = ""
    wss_url: str = ""
    chunk_blocks: int = 2000

    def __post_init__(self) -> None:
        self.http_url = self.http_url or os.getenv("BSC_RPC_HTTP", "")
        self.wss_url = self.wss_url or os.getenv("BSC_RPC_WSS", "")

    def get_logs(self, address: str | list[str], topics: list[Any],
                 from_block: int, to_block: int) -> list[dict]:
        raise NotImplementedError(
            "待实现：按 chunk_blocks 切片调用 eth_getLogs，配合 smrace.decode.swaps 的解码器。"
            "Infinity 要订阅 PoolManager 地址，并先回填 Initialize 事件建立 "
            "PoolId → (currency0, currency1) 映射表，否则 Swap 事件无法还原交易对。"
        )


SOURCES = {
    "synthetic": SyntheticSource,
    "bitquery": BitquerySource,
    "dune": DuneSource,
    "rpc": RpcSource,
}


def build_source(kind: str, **kw):
    if kind not in SOURCES:
        raise ValueError(f"未知数据源 {kind}，可选：{sorted(SOURCES)}")
    return SOURCES[kind](**kw)
