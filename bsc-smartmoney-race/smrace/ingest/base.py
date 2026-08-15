"""数据源统一接口。所有适配器把原始数据归一化成 Trade / Flow / TokenMeta。

分层建议（详见 docs/01）：
    L1 实时层  Bitquery CoreCast gRPC (~300ms)  →  swap + Four.meme 事件
    L2 回填层  Envio HyperSync                  →  历史全量事件，喂自建 PnL 引擎
    L3 校验层  Dune SQL (dex.trades on bnb)     →  离线批量算分、交叉验证
    L4 兜底层  NodeReal / Ankr RPC + 自建 archive →  trace、余额快照、L1 断流补数

关键前提：**BSC 上不存在可用的现成钱包 PnL API**（Moralis 的 profitability
端点当前只覆盖 Ethereum / Polygon / Base）。所以 PnL 引擎必须自建，数据源的
选择标准就变成「谁能最便宜地喂给我完整的 swap 明细」。
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from ..models import Flow, TokenMeta, Trade


@runtime_checkable
class DataSource(Protocol):
    name: str

    def fetch_tokens(self, start_block: int, end_block: int) -> dict[str, TokenMeta]:
        """返回窗口内新建池 / TokenCreate 的代币元数据。"""
        ...

    def fetch_trades(self, tokens: Iterable[str], start_block: int, end_block: int) -> list[Trade]:
        """返回归一化后的 swap 明细。

        实现方必须保证：
          1. wallet = 真实交易者（tx.from），不是 Router —— V3/Infinity 的
             sender/recipient 常常是 Router，直接用会把所有人归成一个地址。
          2. base_amount = ERC20 Transfer 的**实际到账量**，不是 router 报价 ——
             BSC 上大量 memecoin 有 3~10% 的 fee-on-transfer 税。
          3. 失败交易也要返回（success=False），只为统计其 gas。
        """
        ...

    def fetch_flows(self, tokens: Iterable[str], start_block: int, end_block: int) -> list[Flow]:
        """返回非 swap 的代币流入流出（空投 / 桥 / CEX / 内部转账 / LP）。"""
        ...
