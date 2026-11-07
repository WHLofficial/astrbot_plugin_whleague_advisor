#!/usr/bin/env python3
"""离线身价种子 CLI（设计 2026-11-07-market-value-admin §4.2/§5）。

用法：
    python scripts/generate_value_seed.py <advisor.db路径> [-o 输出路径]

只依赖 stdlib + 本仓 services.market_value（纯逻辑模块，零 astrbot、零网络）。
读本地 players 全表复算曲线价，产出平台 D1 种子 SQL（model_value 全量写、
market_value 只填 NULL）并打印摘要与现成的 wrangler 执行命令。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# 设计 §4.2：只 import services.market_value（其内部复用 services.formula /
# fitting / scores 的纯函数；scores 经仓根插件包身份导入，不引入 astrbot）。
import services.market_value as mv  # noqa: E402

PLATFORM_DIR = Path("C:/Users/bhdjb/whlProgram/WHL-club-operations-platform")
DEFAULT_OUTPUT = "market_values_seed.sql"
LADDER_TOP_N = 8


def _diff_counts(rows: list[dict]) -> tuple[int, int]:
    """(差异数, 其中 NULL 补值数)：与本地 players.market_value 逐行比对。"""
    fill = changed = 0
    for row in rows:
        current = row["market_value"]
        if current is None:
            fill += 1
        elif abs(float(current) - row["value"]) > 1e-9:
            changed += 1
    return fill + changed, fill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="players 曲线价 → 平台 model_value/market_value 种子 SQL（设计 §4.2）",
    )
    parser.add_argument("db", help="advisor.db 路径（插件本地库）")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help=f"输出 SQL 路径（默认 {DEFAULT_OUTPUT}）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        rows, stats = mv.collect_rows(conn)
    except mv.ModelUnavailable as exc:
        print(f"模型不可用：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    sql_text, summary = mv.generate_seed_sql(rows, generated_at, stats["model_version"])

    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(sql_text, encoding="utf-8", newline="\n")

    diffs, fills = _diff_counts(rows)
    ladder_top = summary["ladder_counts"][:LADDER_TOP_N]
    reasons = "、".join(f"{k}={v}" for k, v in sorted(stats["skip_reasons"].items())) or "无"

    print(f"数据库: {db_path}")
    print(f"模型: {stats['model_version']}（trained_at {stats['trained_at'] or '未知'}）")
    print(f"入选: {summary['rows']} 行 / 总数 {stats['total']}（跳过 {stats['skipped']}：{reasons}）")
    print("档位分布 top: " + (" ".join(f"{v}×{n}" for v, n in ladder_top) or "无"))
    print(f"与本地 market_value 差异: {diffs} 行（其中 NULL 待补值 {fills} 行）")
    print(f"种子文件: {out_path}")
    print("执行命令（平台 push 部署并跑完迁移后）:")
    print(f'  cd "{PLATFORM_DIR.as_posix()}" && npx wrangler d1 execute whl-club --remote --file="{out_path.as_posix()}"')
    if not PLATFORM_DIR.is_dir():
        print(f"（提示：平台目录 {PLATFORM_DIR} 不存在，命令按设计 §5 给出，请核对）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
