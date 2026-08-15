"""本地缓存：拉一次就别再拉第二次。

Dune 按「数据点 = 行 × 列」计费、Bitquery 按 points + stream-minutes + GB 三条腿
计费 —— 调参阶段反复重拉同一段历史，成本会很难看。所有原始拉取结果落成
JSONL，二次运行直接读盘。
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Type, TypeVar

T = TypeVar("T")


def _enum_safe(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def dump_jsonl(path: str | Path, records: Iterable[Any]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    op = gzip.open if p.suffix == ".gz" else open
    n = 0
    with op(p, "wt", encoding="utf-8") as f:
        for r in records:
            d = asdict(r) if is_dataclass(r) and not isinstance(r, type) else dict(r)
            f.write(json.dumps({k: _enum_safe(v) for k, v in d.items()},
                               ensure_ascii=False) + "\n")
            n += 1
    return n


def load_jsonl(path: str | Path, cls: Type[T] | None = None) -> Iterator[T | dict]:
    p = Path(path)
    if not p.exists():
        return
    op = gzip.open if p.suffix == ".gz" else open
    names = {f.name for f in fields(cls)} if (cls and is_dataclass(cls)) else None
    with op(p, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if cls is None or names is None:
                yield d
            else:
                yield cls(**{k: v for k, v in d.items() if k in names})  # type: ignore[misc]


def cache_path(root: str | Path, source: str, kind: str,
               start_ts: int, end_ts: int) -> Path:
    return Path(root) / "cache" / f"{source}_{kind}_{start_ts}_{end_ts}.jsonl.gz"


def exists(path: str | Path) -> bool:
    return Path(path).exists() and Path(path).stat().st_size > 0
