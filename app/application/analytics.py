"""Read-only analytics helpers for the dashboard analyst tab."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


TICKER_RE = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def readonly_query(db_path: str, sql: str, limit: int = 300) -> dict:
    cleaned = _strip_leading_comments(sql.strip())
    lowered = cleaned.lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("only_select_queries_are_allowed")
    if ";" in cleaned.rstrip(";"):
        raise ValueError("multiple_statements_are_not_allowed")

    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM ({cleaned.rstrip(';')}) LIMIT ?", (limit,)).fetchall()
        return {"columns": rows[0].keys() if rows else [], "rows": [dict(r) for r in rows]}
    finally:
        conn.close()


def schema_summary(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0] for r in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        ]
        chunks = []
        for table in tables:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            col_text = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            chunks.append(f"{table}: {col_text}")
        hints = """
Domain hints:
- Real trades are buy rows in orders where dry_run = 0 and ok = 1.
- Use orders.submitted_at for trade date/time filters.
- Join trade_outcomes t ON t.order_id = orders.id for PnL/resolution.
- Do not use trade_outcomes.updated_at as the trade date; it is reconciliation time.
- For today's UTC trades, use substr(orders.submitted_at, 1, 10) = date('now').
- Prefer aggregate summaries unless the user asks for each row.
"""
        return "\n".join(chunks) + "\n" + hints
    finally:
        conn.close()


def compact_rows(rows: list[dict[str, Any]], max_rows: int = 30) -> str:
    shown = rows[:max_rows]
    return str(shown)


def project_context() -> str:
    return """
Projeto trade2:
- Opera mercados Kalshi BTC 15m com probabilidade Kalshi + spot Coinbase BTC-USD.
- Estratégia atual entra principalmente nos 3 minutos finais, evita 40-60%, usa persistência >=60/<=40 e cruzamento tardio de 50%.
- Usa spot_guard para evitar entrada quando o spot contradiz o lado.
- RiskManager limita entradas reais, risco do portfólio, trade diário, loss diário e loss streak.
- Exits/flip existem, mas ainda estão em avaliação porque exits sensíveis causaram shakeout.
- Para trades reais, diferencie orders enviadas de fills reais: trade_outcomes.entry_price_cents não nulo indica preenchido/reconciliado.

Relatórios prontos que o usuário costuma pedir:
- "trades hoje": resumo de trades reais por orders.submitted_at, PnL e fees.
- "trades recentes": últimos fills/trade_outcomes, wins/losses e pendentes sem fill.
- "minuto final": comportamento das probabilidades nos 60s finais.
- "spot correlação" ou "walk-forward spot": correlação entre variação da probabilidade Kalshi e variação do spot.
- "radar": candidatos de outros mercados com volume/liquidez/spread/deadline.
- "risco": saldo, risco aberto, cap diário e últimos bloqueios.
"""


def analyst_context(db_path: str, question: str, root: Path) -> str:
    stats = final_minute_probability_stats(db_path)
    schema = schema_summary(db_path)
    snippets = _code_snippets(root, question)
    return (
        f"{project_context()}\n"
        f"{schema}\n"
        f"Final-minute BTC15M stats: {stats}\n\n"
        f"Relevant code snippets:\n{snippets}"
    )


def _strip_leading_comments(sql: str) -> str:
    lines = sql.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines).strip()


def _code_snippets(root: Path, question: str) -> str:
    words = {
        w.lower()
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", question)
        if w.lower() not in {"como", "para", "essa", "esse", "dados", "codigo", "funcoes"}
    }
    if not words:
        words = {"decision", "risk", "analytics", "outcome", "order"}
    allowed = [root / "app", root / "backtest", root / "dashboard", root / "tests"]
    matches: list[tuple[int, Path, str]] = []
    for base in allowed:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            score = sum(text.lower().count(w) for w in words)
            if score:
                matches.append((score, path, text))
    out = []
    for _score, path, text in sorted(matches, reverse=True)[:5]:
        picked = _best_snippet(text, words)
        out.append(f"\n# {path.relative_to(root)}\n" + "\n".join(picked[:80]))
    return "\n".join(out)[:6000]


def _best_snippet(text: str, words: set[str]) -> list[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        if (
            (stripped.startswith("class ") or stripped.startswith("def ") or stripped.startswith("async def "))
            and any(w in stripped for w in words)
        ):
            end = min(len(lines), i + 90)
            return [f"{n+1}: {lines[n]}" for n in range(i, end)]
    for i, line in enumerate(lines):
        if any(w in line.lower() for w in words):
            start = max(0, i - 8)
            end = min(len(lines), i + 28)
            return [f"{n+1}: {lines[n]}" for n in range(start, end)]
    return []


def final_minute_probability_stats(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    resolutions = {
        r["ticker"]: r["result"]
        for r in conn.execute(
            "SELECT ticker, result FROM market_resolutions WHERE result IN ('yes', 'no')"
        )
    }
    buckets = {
        "45-60s": [],
        "30-45s": [],
        "15-30s": [],
        "0-15s": [],
    }
    by_ticker: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in conn.execute(
        """SELECT ticker, captured_at, yes_bid, yes_ask, last_price
           FROM ticks
           WHERE ticker LIKE 'KXBTC15M-%'
           ORDER BY ticker, captured_at"""
    ):
        ticker = row["ticker"]
        if ticker not in resolutions:
            continue
        close_time = _close_time_from_ticker(ticker)
        prob = _yes_mid(row)
        if close_time is None or prob is None:
            continue
        captured = datetime.fromisoformat(row["captured_at"]).replace(tzinfo=None)
        ttc = (close_time - captured).total_seconds()
        if 0 <= ttc <= 60:
            by_ticker[ticker].append((ttc, prob))

    for ticker, samples in by_ticker.items():
        result = resolutions[ticker]
        for ttc, prob in samples:
            label = _bucket(ttc)
            if label is None:
                continue
            side = "yes" if prob >= 0.5 else "no"
            buckets[label].append({
                "prob": prob,
                "confidence": max(prob, 1 - prob),
                "correct": 1 if side == result else 0,
                "cross_zone": 1 if 0.45 <= prob <= 0.55 else 0,
            })

    rows = []
    for label, samples in buckets.items():
        n = len(samples)
        rows.append({
            "bucket": label,
            "ticks": n,
            "avg_yes_prob": _avg([s["prob"] for s in samples]),
            "avg_confidence": _avg([s["confidence"] for s in samples]),
            "direction_accuracy": _avg([s["correct"] for s in samples]),
            "pct_near_50": _avg([s["cross_zone"] for s in samples]),
        })
    return {"markets": len(by_ticker), "rows": rows}


def spot_probability_walk_forward(db_path: str, folds: int = 4,
                                  max_ttc_seconds: float = 180.0) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT ticker, captured_at, yes_bid, yes_ask, last_price
           FROM ticks
           WHERE ticker LIKE 'KXBTC15M-%'
           ORDER BY ticker, captured_at"""
    ).fetchall()
    spots = conn.execute(
        """SELECT captured_at, price FROM spot_ticks
           WHERE product = 'BTC-USD'
           ORDER BY captured_at"""
    ).fetchall()
    conn.close()

    spot_series = [
        (datetime.fromisoformat(r["captured_at"]).replace(tzinfo=None), float(r["price"]))
        for r in spots
    ]
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    spot_idx = 0
    for row in rows:
        captured = datetime.fromisoformat(row["captured_at"]).replace(tzinfo=None)
        close_time = _close_time_from_ticker(row["ticker"])
        prob = _yes_mid(row)
        if close_time is None or prob is None:
            continue
        ttc = (close_time - captured).total_seconds()
        if ttc < 0 or ttc > max_ttc_seconds:
            continue
        while spot_idx + 1 < len(spot_series) and spot_series[spot_idx + 1][0] <= captured:
            spot_idx += 1
        if not spot_series:
            continue
        spot_time, spot_price = spot_series[spot_idx]
        if abs((captured - spot_time).total_seconds()) > 10:
            continue
        by_ticker[row["ticker"]].append({
            "captured": captured,
            "prob": float(prob),
            "spot": spot_price,
            "ttc": ttc,
        })

    market_rows = []
    for ticker, samples in by_ticker.items():
        samples.sort(key=lambda x: x["captured"])
        pairs = []
        for prev, cur in zip(samples, samples[1:]):
            d_prob = cur["prob"] - prev["prob"]
            d_spot = cur["spot"] - prev["spot"]
            if abs(d_prob) < 1e-9 and abs(d_spot) < 1e-9:
                continue
            pairs.append((d_prob, d_spot))
        if len(pairs) >= 3:
            market_rows.append({
                "ticker": ticker,
                "samples": len(pairs),
                "corr": _corr([p[0] for p in pairs], [p[1] for p in pairs]),
                "same_direction": _avg([
                    1.0 if (p[0] > 0 and p[1] > 0) or (p[0] < 0 and p[1] < 0) else 0.0
                    for p in pairs if abs(p[0]) > 1e-9 and abs(p[1]) > 1e-9
                ]),
            })

    market_rows.sort(key=lambda r: r["ticker"])
    if not market_rows:
        return {"markets": 0, "folds": []}
    fold_size = max(1, len(market_rows) // max(1, folds))
    fold_rows = []
    for i in range(max(1, folds)):
        start = i * fold_size
        end = len(market_rows) if i == folds - 1 else min(len(market_rows), (i + 1) * fold_size)
        chunk = market_rows[start:end]
        if not chunk:
            continue
        fold_rows.append({
            "fold": i + 1,
            "markets": len(chunk),
            "avg_corr": _avg([r["corr"] for r in chunk if r["corr"] is not None]),
            "avg_same_direction": _avg([
                r["same_direction"] for r in chunk if r["same_direction"] is not None
            ]),
            "samples": sum(r["samples"] for r in chunk),
            "first_ticker": chunk[0]["ticker"],
            "last_ticker": chunk[-1]["ticker"],
        })
    return {
        "markets": len(market_rows),
        "max_ttc_seconds": max_ttc_seconds,
        "folds": fold_rows,
        "overall": {
            "avg_corr": _avg([r["corr"] for r in market_rows if r["corr"] is not None]),
            "avg_same_direction": _avg([
                r["same_direction"] for r in market_rows if r["same_direction"] is not None
            ]),
            "samples": sum(r["samples"] for r in market_rows),
        },
    }


def _close_time_from_ticker(ticker: str) -> datetime | None:
    match = TICKER_RE.match(ticker)
    if not match:
        return None
    yy, mon, dd, hh, mm = match.groups()
    et = datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), int(mm))
    return et + timedelta(hours=4)


def _yes_mid(row: sqlite3.Row) -> float | None:
    if row["yes_bid"] is not None and row["yes_ask"] is not None:
        return (row["yes_bid"] + row["yes_ask"]) / 2
    return row["last_price"]


def _bucket(ttc: float) -> str | None:
    if 45 <= ttc <= 60:
        return "45-60s"
    if 30 <= ttc < 45:
        return "30-45s"
    if 15 <= ttc < 30:
        return "15-30s"
    if 0 <= ttc < 15:
        return "0-15s"
    return None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = sum((x - mx) ** 2 for x in xs)
    deny = sum((y - my) ** 2 for y in ys)
    if denx <= 0 or deny <= 0:
        return None
    return round(num / ((denx * deny) ** 0.5), 4)
