"""命令行入口。

    python -m smrace.cli run                      # 端到端跑一遍
    python -m smrace.cli validate                 # 合成数据 ground truth 验证
    python -m smrace.cli oos                      # 时间外样本验证（真实数据首选）
    python -m smrace.cli probe --source bitquery  # 拉一小段真数据，核对字段映射与单位
    python -m smrace.cli bootstrap --days 7       # Dune 一条龙：校验→建Query→估账单→(--yes)真跑
    python -m smrace.cli sql                      # 打印要贴到 Dune 的 SQL
    python -m smrace.cli estimate --rows 250000   # 本地算账单
    python -m smrace.cli sweep                    # 阈值敏感性扫描
    python -m smrace.cli config                   # 打印当前配置

真实数据源需要的环境变量：
    BITQUERY_TOKEN            Bitquery API token
    DUNE_API_KEY              Dune API key（配合 --query-id）
    SMRACE_BNB_PRICE_CSV      BNB 的 ts,price 时间序列（gas 计价必需）
    SMRACE_BNB_PRICE_FLAT     没有 CSV 时的固定 BNB 价（仅供冒烟测试）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Config
from .pipeline import run, save, validate


def _probe(args) -> int:
    """拉一小段真实数据，打印归一化前后的对照。

    **接线后第一件要做的事。** 它专门用来抓两类错误：
      · 字段名对不上 → 归一化后是空列表
      · 单位对不上   → gas_usd 或 quote_usd 差一个数量级
    """
    from .ingest.adapters import build_source

    end_ts = args.end_ts or int(time.time())
    start_ts = args.start_ts or (end_ts - args.minutes * 60)
    kw = {}
    if args.source == "dune" and args.query_id:
        kw["query_id"] = args.query_id

    print(f"探针：source={args.source}  窗口 {start_ts}~{end_ts}"
          f"（{(end_ts - start_ts) / 60:.0f} 分钟）")
    src = build_source(args.source, **kw)
    trades = src.fetch_trades(None, start_ts, end_ts)
    print(f"\n归一化后得到 {len(trades):,} 笔成交")
    if not trades:
        print("\n❌ 一笔都没有。排查顺序：")
        print("   1. 凭证是否有效（换个更宽的时间窗再试）")
        print("   2. 字段名是否对得上 —— 改 _map_trade() / _map_row()")
        print("   3. 报价资产白名单 constants.QUOTE_TOKENS 是否覆盖了实际的 quote 侧")
        return 1

    for t in trades[:5]:
        print(f"  blk={t.block} {t.side.value:4s} {t.token[:12]}…  "
              f"${t.usd:>12,.2f}  px={t.exec_price:.3e}  gas=${t.gas_usd:.4f}  "
              f"{t.venue}  wallet={t.wallet[:12]}…")

    # ---- 合理性检查：单位错误在这里必然暴露
    gas = sorted(t.gas_usd for t in trades if t.gas_usd > 0)
    print("\n合理性检查")
    if not gas:
        print("  ⚠️ gas_usd 全为 0 —— 没接 gas 字段或没喂 BNB 价。"
              "净 PnL 会高估，高频地址尤其严重。")
    else:
        med = gas[len(gas) // 2]
        ok = 0.005 <= med <= 2.0
        print(f"  {'✅' if ok else '❌'} gas 中位数 ${med:.4f}"
              f"（BSC 典型 $0.05–0.3）{'' if ok else ' ← 多半是 GasPrice 单位错了'}")
    usd = sorted(t.usd for t in trades)
    print(f"  成交额中位 ${usd[len(usd)//2]:,.2f}  最小 ${usd[0]:,.2f}  最大 ${usd[-1]:,.2f}")
    wallets = {t.wallet for t in trades}
    top = max(sum(1 for t in trades if t.wallet == w) for w in wallets)
    share = top / len(trades)
    print(f"  {'✅' if share < 0.3 else '❌'} 最活跃地址占比 {share:.1%}"
          f"{'' if share < 0.3 else ' ← 疑似把 Router 当成了真人钱包'}")
    print(f"  覆盖 {len(wallets):,} 个地址 · {len({t.token for t in trades}):,} 个代币 · "
          f"venue={sorted({t.venue for t in trades})[:6]}")
    return 0


def _bare_dune(args):
    """构造一个只用来渲染 SQL 的 DuneSource，不触发 API key 校验。"""
    from .ingest.dune import DuneSource
    s = DuneSource.__new__(DuneSource)
    s.with_gas = not args.no_gas
    s.scoped = not args.full
    s.min_usd = args.min_usd
    s.min_traders = args.min_traders
    s.max_tokens = args.max_tokens
    s.buffer_days = args.buffer_days
    return s


def _sql(args) -> int:
    from .ingest.dune import POOLS_SQL, DuneSource
    end_ts = args.end_ts or int(time.time())
    start_ts = args.start_ts or (end_ts - args.days * 86400)
    src = _bare_dune(args)
    mode = "全量" if args.full else f"窄范围（新币 · ≥{args.min_traders} 个参与地址 · Top{args.max_tokens}）"
    print(f"-- ① 成交明细【{mode}】存成 Query 后把 id 传给 --query-id\n")
    print(DuneSource.render_sql(src, start_ts, end_ts))
    print("\n\n-- ② 成本估算：先存成 Query，把 id 传给 `estimate --estimate-query-id`")
    print("-- 它只返回 6 个聚合数，几乎不耗额度。**正式查询前一定先跑这个。**\n")
    print(DuneSource.render_estimate_sql(src, start_ts, end_ts))
    print("\n\n-- ③ 建池区块（launch_block）\n")
    print(POOLS_SQL.format(start_ts=start_ts, end_ts=end_ts))
    print("\n-- ④ ⚠️ 上线前先跑这句，确认 Infinity 是否已被收录：")
    print("-- SELECT DISTINCT project, version FROM dex.trades WHERE blockchain='bnb'")
    return 0


def _estimate(args) -> int:
    """跑前先算账单。没有 key 时退化成纯本地计算器（--rows N）。"""
    from .ingest.dune import DUNE_TIERS, estimate_cost

    def render(cost: dict, label: str) -> None:
        print(f"\n{label}：{cost['n_rows']:,} 行 ≈ {cost['mb']:.1f} MB")
        print(f"  {'档位':<10}{'credits':>12}{'超额成本':>12}{'占免费额度':>14}")
        for name in ("free", "analyst", "plus"):
            c = cost[name]
            quota = (f"{c['pct_of_quota']:.0%}" if "pct_of_quota" in c else "—")
            print(f"  {name:<10}{c['credits']:>12,.0f}{'$' + format(c['usd_if_overage'], ',.2f'):>12}{quota:>14}")

    if args.rows:
        render(estimate_cost(args.rows, args.bytes_per_row), "手动估算")
        _advise(estimate_cost(args.rows, args.bytes_per_row))
        return 0

    if not args.estimate_query_id:
        print("用法二选一：")
        print("  1) 已把 estimate SQL 存成 Dune Query："
              "smrace.cli estimate --estimate-query-id 1234567 --days 7")
        print("  2) 只想本地算："
              "smrace.cli estimate --rows 500000")
        print("\nSQL 用 `smrace.cli sql --days 7` 打印（第 ② 段）。")
        return 1

    from .ingest.dune import DuneSource
    end_ts = args.end_ts or int(time.time())
    start_ts = args.start_ts or (end_ts - args.days * 86400)
    src = DuneSource(with_gas=not args.no_gas, scoped=not args.full,
                     min_usd=args.min_usd, min_traders=args.min_traders,
                     max_tokens=args.max_tokens, buffer_days=args.buffer_days)
    r = src.estimate(start_ts, end_ts, args.estimate_query_id)

    print(f"\n窗口 {args.days} 天  ·  单笔 ≥ ${args.min_usd:g}")
    print(f"  全量成交            {r['rows_full']:>12,} 行")
    print(f"  其中新币且够开比赛   {r['rows_eligible']:>12,} 行"
          f"（{r['tokens_eligible']:,} 个代币）")
    print(f"  Top{args.max_tokens} 截断后实际导出 {r['rows_scoped']:>12,} 行")
    if r["tokens_truncated"]:
        # 自己的文档要求：截断必须显式报出，不能静默发生
        print(f"  ⚠️ 被截断丢弃        {r['rows_truncated']:>12,} 行"
              f"（{r['tokens_truncated']:,} 个代币）—— 调大 --max-tokens 可纳入")
    render(r["cost_full"], "不收敛（全量）")
    render(r["cost_scoped"], "收敛后（本次实际导出）")
    _advise(r["cost_scoped"])
    return 0


def _advise(cost: dict) -> None:
    free = cost["free"]
    print()
    if free.get("fits_in_free_quota"):
        print(f"✅ 放得进 Free 档 2,500 credits/月（用掉 {free['pct_of_quota']:.0%}）")
    else:
        need = free["credits"]
        print(f"❌ 超出 Free 档 2,500 credits/月（需要 {need:,.0f}）。三条路：")
        print(f"   · 缩范围：天数减半 / 调高 --min-usd / 调低 --max-tokens")
        print(f"   · 忍痛补 credit：Free 档超额 ${free['usd_if_overage']:,.2f}")
        print(f"   · 直接升档：Analyst 超额单价只有 Free 的 1/2.7，"
              f"同样的量 ${cost['analyst']['usd_if_overage']:,.2f}")
        print("   ⚠️ 卡在「Free 档超额跑」是最贵的选择，不要停在那里。")
    bpr = cost["bytes_per_row"]
    afford = int(2500 / 20 * 1_048_576 / bpr)     # Free: 2500 credits ÷ 20 credits/MB
    print(f"\nFree 档 2,500 credits 大约能导出 {afford:,} 行"
          f"（按 {bpr} 字节/行）。先用这个数倒推你的窗口和过滤条件。")
    print("注：字节/行是经验值。第一次真跑完请用"
          "「实际扣的 credit ÷ 预估」校准 --bytes-per-row。")


def _bootstrap(cfg: Config, args) -> int:
    """一条命令走完：校验 key → 建 Query → 估账单 → （确认后）真跑 → 出榜单。

    设计原则：**默认在花钱之前停下来。** 估算是免费的，正式查询不是。
    只有显式加 --yes 才会继续往下走。
    """
    from .ingest.dune import POOLS_SQL, DuneSource, estimate_cost
    from .ingest.http import ApiError

    end_ts = args.end_ts or int(time.time())
    start_ts = args.start_ts or (end_ts - args.days * 86400)
    src = DuneSource(with_gas=not args.no_gas, scoped=not args.full,
                     min_usd=args.min_usd, min_traders=args.min_traders,
                     max_tokens=args.max_tokens, buffer_days=args.buffer_days)

    print("① 校验凭证（0 credit）…")
    if not src.check_auth():
        print("   ✗ key 无效或已失效。去 dune.com → Settings → API keys 重新生成。")
        return 2
    print("   ✓ 有效")

    # ---- ② 建 Query（付费档才有 CRUD API，失败就退回手工贴）
    # 参数化渲染（{{start_ts}}/{{end_ts}}）：同一个 Query 换窗口可复用，
    # 执行时传 query_parameters。内联时间戳的版本换窗口会静默跑旧窗口。
    est_sql = src.render_estimate_sql(None, None)
    trades_sql = src.render_sql(None, None)
    tag = f"smrace {args.days}d top{args.max_tokens}"

    est_id = args.estimate_query_id
    trades_id = args.query_id
    if est_id is None or trades_id is None:
        print("② 尝试用 CRUD API 自动建 Query…")
        try:
            if est_id is None:
                est_id = src.create_query(f"{tag} · estimate", est_sql)
                print(f"   ✓ 估算 Query id={est_id}")
            if trades_id is None:
                trades_id = src.create_query(f"{tag} · trades", trades_sql)
                print(f"   ✓ 明细 Query id={trades_id}")
        except ApiError as e:
            print(f"   ✗ 自动建 Query 失败（{e.status or '未知'}）——"
                  " CRUD API 一般要付费档，Free 档走手工路径：")
            path = Path(cfg.out_dir) / "dune_queries.sql"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "-- 前两段含 {{start_ts}}/{{end_ts}} 参数：粘贴后 Dune 会自动识别，\n"
                "-- 把参数类型设成 number 再保存，之后换窗口无需改 SQL。\n\n"
                f"-- ① 估算（先存这个）\n{est_sql}\n\n\n"
                f"-- ② 明细\n{trades_sql}\n\n\n"
                f"-- ③ 建池区块\n{POOLS_SQL.format(start_ts=start_ts, end_ts=end_ts)}\n"
            )
            print(f"      SQL 已写到 {path}")
            print("      到 dune.com 新建 Query → 贴第 ① 段 → 保存 → 复制 URL 里的 id")
            print(f"      然后重跑：smrace.cli bootstrap --days {args.days} "
                  f"--estimate-query-id <id>")
            return 3

    # ---- ③ 估算（便宜）
    print("③ 估算本次导出量与账单（只返回 6 个聚合数）…")
    r = src.estimate(start_ts, end_ts, est_id)
    print(f"\n   窗口 {args.days} 天 · 单笔 ≥ ${args.min_usd:g} · Top{args.max_tokens}")
    print(f"   全量成交              {r['rows_full']:>12,} 行")
    print(f"   新币且够开比赛         {r['rows_eligible']:>12,} 行"
          f"（{r['tokens_eligible']:,} 个代币）")
    print(f"   实际导出              {r['rows_scoped']:>12,} 行")
    if r["tokens_truncated"]:
        print(f"   ⚠️ 截断丢弃           {r['rows_truncated']:>12,} 行"
              f"（{r['tokens_truncated']:,} 个代币）")
    c = estimate_cost(r["rows_scoped"], args.bytes_per_row)
    print(f"\n   ≈ {c['mb']:.1f} MB → Free 档 {c['free']['credits']:,.0f} credits"
          f"（免费额度的 {c['free']['pct_of_quota']:.0%}）")

    if not c["free"]["fits_in_free_quota"]:
        print("\n   ❌ 放不进 Free 档 2,500 credits。收敛后再来：")
        print(f"      --days {max(1, args.days // 2)} 或 --max-tokens "
              f"{max(100, args.max_tokens // 3)} 或 --min-usd {args.min_usd * 5:g}")
        if not args.yes:
            return 4
        print("   （已加 --yes，仍继续 —— 请确认你清楚会产生超额费用）")

    if not args.yes:
        print(f"\n   到此为止没有花钱。确认要跑正式查询就加 --yes：")
        print(f"      smrace.cli bootstrap --days {args.days} "
              f"--max-tokens {args.max_tokens} --query-id {trades_id} "
              f"--estimate-query-id {est_id} --yes")
        return 0

    # ---- ④ 正式跑
    print("\n④ 拉取明细并跑管线…")
    cfg.data_source = "dune"
    cfg.start_ts, cfg.end_ts = start_ts, end_ts
    import smrace.ingest.adapters as ad
    ad.SOURCES["dune"] = lambda **kw: DuneSource(
        query_id=trades_id, with_gas=not args.no_gas, scoped=not args.full,
        min_usd=args.min_usd, min_traders=args.min_traders,
        max_tokens=args.max_tokens, buffer_days=args.buffer_days)
    res = run(cfg, verbose=True)
    validate(res, verbose=True)
    files = save(res, cfg.out_dir)
    print("\n输出：" + "  ".join(files))
    print(f"\n下一步做时间外样本验证（用缓存，不再花 credit）：")
    print(f"   smrace.cli oos --source dune --days {args.days}")
    return 0


def _oos(cfg: Config, verbose: bool, split: float) -> int:
    from .backtest.oos import run_oos
    from .ingest.loader import load_dataset
    ds = load_dataset(cfg, verbose=verbose)
    run_oos(ds, cfg, split_ratio=split, verbose=verbose)
    return 0


def _sweep(path: str | None) -> int:
    print(f"{'min_trades':>10} {'FDR':>6} {'通过':>6} {'精确率':>8} {'召回率':>8}")
    for min_trades in (10, 20, 40):
        for fdr in (0.05, 0.10, 0.20):
            c = Config.load(path)
            c.purify.min_trades = min_trades
            c.gate.fdr_target = fdr
            c.gate.bootstrap_B = 400     # 扫描时降采样加速
            r = run(c, verbose=False)
            v = validate(r, verbose=False)
            n = len(r["_internal"]["passed"])
            p = f"{v['precision']:.1%}" if v["precision"] is not None else "n/a"
            rc = f"{v['recall']:.1%}" if v["recall"] is not None else "n/a"
            print(f"{min_trades:>10} {fdr:>6.2f} {n:>6} {p:>8} {rc:>8}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="smrace", description="BSC 聪明钱赛马框架")
    ap.add_argument("cmd", choices=["run", "validate", "oos", "probe", "sql",
                                    "estimate", "bootstrap", "sweep", "config"])
    ap.add_argument("-c", "--config", default=None, help="YAML 配置文件路径")
    ap.add_argument("-o", "--out", default=None, help="输出目录")
    ap.add_argument("-s", "--source", default=None,
                    help="synthetic | bitquery | dune | rpc（覆盖配置文件）")
    ap.add_argument("--days", type=int, default=None, help="回看天数")
    ap.add_argument("--minutes", type=int, default=10, help="probe 的窗口分钟数")
    ap.add_argument("--start-ts", type=int, default=0)
    ap.add_argument("--end-ts", type=int, default=0)
    ap.add_argument("--query-id", type=int, default=None, help="Dune 预存 Query 的 id")
    ap.add_argument("--split", type=float, default=0.67, help="oos 的训练/测试切分比例")
    ap.add_argument("--no-gas", action="store_true", help="不 join gas（省两列）")
    ap.add_argument("--full", action="store_true",
                    help="用全量 SQL 而不是窄范围版（会非常贵）")
    ap.add_argument("--min-usd", type=float, default=10.0, help="单笔成交额下限")
    ap.add_argument("--min-traders", type=int, default=5, help="代币的参与地址数下限")
    ap.add_argument("--max-tokens", type=int, default=2000, help="代币数硬性封顶（有损）")
    ap.add_argument("--buffer-days", type=int, default=7, help="判定「老币」的回看缓冲")
    ap.add_argument("--estimate-query-id", type=int, default=None,
                    help="estimate 命令：预存的估算 Query id")
    ap.add_argument("--yes", action="store_true",
                    help="bootstrap：估完账单后继续跑正式查询（会消耗 credit）")
    ap.add_argument("--rows", type=int, default=0,
                    help="estimate 命令：跳过查询，直接按行数本地估算")
    ap.add_argument("--bytes-per-row", type=int, default=420,
                    help="估算用的经验字节/行，首跑后请校准")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    cfg = Config.load(a.config)
    if a.out:
        cfg.out_dir = a.out
    if a.source:
        cfg.data_source = a.source
    if a.days:
        cfg.lookback_days = a.days
    cfg.start_ts, cfg.end_ts = a.start_ts, a.end_ts
    verbose = not a.quiet

    if a.cmd == "config":
        print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "probe":
        a.source = a.source or "bitquery"
        return _probe(a)
    if a.cmd == "sql":
        a.days = a.days or 90
        return _sql(a)
    if a.cmd == "estimate":
        a.days = a.days or 7
        return _estimate(a)
    if a.cmd == "bootstrap":
        a.days = a.days or 7
        return _bootstrap(cfg, a)
    if a.cmd == "sweep":
        return _sweep(a.config)
    if a.cmd == "oos":
        return _oos(cfg, verbose, a.split)

    res = run(cfg, verbose=verbose)
    if a.cmd == "validate":
        validate(res, verbose=verbose)
    files = save(res, cfg.out_dir)
    if verbose:
        print("\n输出：" + "  ".join(files))
    return 0


HINTS = {
    "BITQUERY_TOKEN": "export BITQUERY_TOKEN=...   # bitquery.io → 控制台 → API token",
    "DUNE_API_KEY": "export DUNE_API_KEY=...      # dune.com → Settings → API keys",
}


def _friendly(e: Exception) -> int:
    """凭证/配置类错误不需要 traceback，直接告诉用户下一步做什么。"""
    msg = str(e)
    print(f"\n✗ {msg}", file=sys.stderr)
    for k, hint in HINTS.items():
        if k in msg:
            print(f"\n  {hint}", file=sys.stderr)
            print("  另外 gas 计价需要 BNB 价："
                  "export SMRACE_BNB_PRICE_FLAT=600（正式请用 SMRACE_BNB_PRICE_CSV）",
                  file=sys.stderr)
            print("  完整接线步骤见 docs/03-接入真实数据源.md", file=sys.stderr)
            return 2
    return 1


if __name__ == "__main__":
    from .ingest.http import ApiError
    try:
        sys.exit(main())
    except (ApiError, RuntimeError, ValueError) as exc:
        sys.exit(_friendly(exc))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
