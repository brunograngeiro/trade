"""Streamlit dashboard — Live / Sinais / Trades / Backtest / Portfolio."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from backtest.engine import BacktestParams, run_backtest, walk_forward  # noqa: E402
from backtest.portfolio_sim import SimParams, run_simulation  # noqa: E402


SETTINGS = get_settings()
API_URL = os.getenv("TRADE2_API_URL", f"http://127.0.0.1:{SETTINGS.app_port}")


st.set_page_config(page_title="trade2", layout="wide", page_icon="📈")
st.title("trade2 — Kalshi BTC 15min Operator")


def _get(path: str, timeout: float = 5.0) -> dict:
    try:
        r = requests.get(f"{API_URL}{path}", timeout=timeout)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc)}


TICKER_RE = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
TTC_BUCKETS = [
    ("0-30s", 0, 30),
    ("30-60s", 30, 60),
    ("60-120s", 60, 120),
    ("2-3m", 120, 180),
    ("3-5m", 180, 300),
    ("5-8m", 300, 480),
    ("8-12m", 480, 720),
    ("12-15m", 720, 900),
]
REGIME_ORDER = [
    "40-60",
    "60-70/30-40",
    "70-90/10-30",
    "90-95/5-10",
    "95-99/1-5",
    ">=99/<=1",
]


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


def _regime(prob: float) -> str:
    confidence = max(prob, 1.0 - prob)
    if 0.40 <= prob <= 0.60:
        return "40-60"
    if confidence < 0.70:
        return "60-70/30-40"
    if confidence < 0.90:
        return "70-90/10-30"
    if confidence < 0.95:
        return "90-95/5-10"
    if confidence < 0.99:
        return "95-99/1-5"
    return ">=99/<=1"


@st.cache_data(ttl=120)
def calibration_stats(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    resolutions = {
        r["ticker"]: r["result"]
        for r in conn.execute(
            "SELECT ticker, result FROM market_resolutions WHERE result IN ('yes', 'no')"
        )
    }

    ttc_regime = {label: Counter() for label, _, _ in TTC_BUCKETS}
    ttc_correct = {label: Counter() for label, _, _ in TTC_BUCKETS}
    ttc_totals = Counter()
    final_tick = Counter()
    final_correct = Counter()
    by_ticker: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for row in conn.execute(
        """SELECT ticker, captured_at, yes_bid, yes_ask, last_price
           FROM ticks ORDER BY ticker, captured_at"""
    ):
        ticker = row["ticker"]
        result = resolutions.get(ticker)
        if result not in ("yes", "no"):
            continue
        close_t = _close_time_from_ticker(ticker)
        prob = _yes_mid(row)
        if close_t is None or prob is None:
            continue
        captured = datetime.fromisoformat(row["captured_at"]).replace(tzinfo=None)
        ttc = (close_t - captured).total_seconds()
        if ttc < 0 or ttc > 900:
            continue
        prob = max(0.0, min(1.0, prob))
        side = "yes" if prob >= 0.5 else "no"
        regime = _regime(prob)
        for label, lo, hi in TTC_BUCKETS:
            if lo <= ttc < hi:
                ttc_regime[label][regime] += 1
                ttc_totals[label] += 1
                if side == result:
                    ttc_correct[label][regime] += 1
                break
        by_ticker[ticker].append((ttc, prob))

    cross_rows = []
    for window in (30, 60, 120, 180, 300, 900):
        markets = with_cross = last_ok = after_cross_ok = after_cross_n = 0
        for ticker, samples in by_ticker.items():
            result = resolutions[ticker]
            sub = [(ttc, prob) for ttc, prob in samples if ttc <= window]
            if len(sub) < 2:
                continue
            markets += 1
            prev_side = None
            crosses = 0
            last_side = None
            for _ttc, prob in sub:
                side = "yes" if prob > 0.5 else "no" if prob < 0.5 else None
                if side is None:
                    continue
                if prev_side and side != prev_side:
                    crosses += 1
                prev_side = side
                last_side = side
            if last_side == result:
                last_ok += 1
            if crosses:
                with_cross += 1
                after_cross_n += 1
                if last_side == result:
                    after_cross_ok += 1
        cross_rows.append({
            "janela": f"{window}s" if window < 900 else "15m",
            "mercados": markets,
            "cruzaram_50": with_cross,
            "pct_cruzaram": with_cross / markets if markets else 0,
            "ultimo_lado_acerto": last_ok / markets if markets else 0,
            "pos_cruzamento_acerto": after_cross_ok / after_cross_n if after_cross_n else None,
        })

    for ticker, samples in by_ticker.items():
        if not samples:
            continue
        _ttc, prob = samples[-1]
        regime = _regime(prob)
        side = "yes" if prob >= 0.5 else "no"
        final_tick[regime] += 1
        if side == resolutions[ticker]:
            final_correct[regime] += 1

    conn.close()
    dist_rows = []
    for label, _, _ in TTC_BUCKETS:
        total = ttc_totals[label]
        for regime in REGIME_ORDER:
            n = ttc_regime[label][regime]
            dist_rows.append({
                "ttc": label,
                "regime": regime,
                "ticks": n,
                "pct_ticks": n / total if total else 0,
                "direction_accuracy": ttc_correct[label][regime] / n if n else None,
            })
    final_rows = [
        {
            "regime": regime,
            "markets": final_tick[regime],
            "accuracy": final_correct[regime] / final_tick[regime]
            if final_tick[regime] else None,
        }
        for regime in REGIME_ORDER
    ]
    return {
        "dist": dist_rows,
        "cross": cross_rows,
        "final": final_rows,
        "markets": len(by_ticker),
    }


tabs = st.tabs(["Live", "Portfolio", "Radar", "Sinais", "Trades", "Backtest"])


# -------------------- Live --------------------

with tabs[0]:
    col_l, col_r = st.columns([2, 1])
    with col_r:
        if st.button("Refresh", use_container_width=True, key="refresh_live"):
            st.rerun()
        auto = st.toggle("Auto-refresh (5s)", value=False)

    live = _get("/market/live")
    if "_error" in live:
        st.error(f"API offline: {live['_error']}")
    elif live.get("ticker"):
        phase = live.get("phase") or "?"
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Ticker", live.get("ticker") or "—")
        m2.metric("Fase", phase.upper())
        m3.metric("YES mid", f"{(live.get('yes_mid') or 0):.3f}")
        m4.metric("Último sinal", (live.get("last_signal_kind") or "—").upper())

        b1, b2, b3, b4 = st.columns(4)
        b1.metric("YES bid", f"{(live.get('yes_bid') or 0):.3f}")
        b2.metric("YES ask", f"{(live.get('yes_ask') or 0):.3f}")
        b3.metric("NO bid", f"{(live.get('no_bid') or 0):.3f}")
        b4.metric("NO ask", f"{(live.get('no_ask') or 0):.3f}")

        st.caption(f"Captured at: {live.get('captured_at')}")
        st.caption(live.get("title") or "")

        stats = calibration_stats(SETTINGS.db_path)
        dist = pd.DataFrame(stats["dist"])
        cross = pd.DataFrame(stats["cross"])
        final = pd.DataFrame(stats["final"])

        st.subheader("Calibração por tempo até o fechamento")
        c1, c2, c3 = st.columns(3)
        c1.metric("Mercados calibrados", stats["markets"])
        c2.metric("Cruzam 50% nos 60s finais",
                  f"{cross.loc[cross['janela'] == '60s', 'pct_cruzaram'].iloc[0] * 100:.1f}%")
        c3.metric("Último lado acerta",
                  f"{cross.loc[cross['janela'] == '60s', 'ultimo_lado_acerto'].iloc[0] * 100:.1f}%")

        fig_dist = go.Figure()
        for regime in REGIME_ORDER:
            chunk = dist[dist["regime"] == regime]
            fig_dist.add_trace(go.Bar(
                x=chunk["ttc"], y=chunk["pct_ticks"] * 100,
                name=regime,
                customdata=(chunk["direction_accuracy"] * 100).round(1),
                hovertemplate="%{x}<br>%{fullData.name}: %{y:.1f}% dos ticks"
                              "<br>acerto direção: %{customdata:.1f}%<extra></extra>",
            ))
        fig_dist.update_layout(
            barmode="stack", height=360, margin=dict(t=10, b=10, l=10, r=10),
            yaxis_title="% dos ticks", legend=dict(orientation="h", y=-0.18),
        )
        st.plotly_chart(fig_dist, use_container_width=True)

        col_a, col_b = st.columns(2)
        with col_a:
            fig_cross = go.Figure()
            fig_cross.add_trace(go.Bar(
                x=cross["janela"], y=cross["pct_cruzaram"] * 100,
                name="cruzaram 50%", marker_color="#f59e0b",
            ))
            fig_cross.add_trace(go.Scatter(
                x=cross["janela"], y=cross["pos_cruzamento_acerto"] * 100,
                name="acerto após cruzar", mode="lines+markers",
                line=dict(color="#19c37d"),
            ))
            fig_cross.update_layout(
                height=320, margin=dict(t=10, b=10, l=10, r=10),
                yaxis_title="%", legend=dict(orientation="h", y=-0.18),
            )
            st.plotly_chart(fig_cross, use_container_width=True)

        with col_b:
            fig_final = go.Figure()
            fig_final.add_trace(go.Bar(
                x=final["regime"], y=final["markets"],
                name="mercados", marker_color="#64748b", yaxis="y1",
            ))
            fig_final.add_trace(go.Scatter(
                x=final["regime"], y=final["accuracy"] * 100,
                name="acerto", mode="lines+markers",
                line=dict(color="#19c37d"), yaxis="y2",
            ))
            fig_final.update_layout(
                height=320, margin=dict(t=10, b=10, l=10, r=10),
                yaxis=dict(title="mercados"),
                yaxis2=dict(title="acerto %", overlaying="y", side="right", range=[0, 105]),
                legend=dict(orientation="h", y=-0.18),
            )
            st.plotly_chart(fig_final, use_container_width=True)

        decisions = _get("/strategy/decisions/recent?limit=20").get("decisions", [])
        if decisions:
            st.subheader("Últimas decisões da estratégia")
            ddf = pd.DataFrame(decisions)
            ddf["captured_at"] = pd.to_datetime(ddf["captured_at"])
            keep = ["captured_at", "ticker", "action", "side", "probability",
                    "confidence", "ttc_seconds", "limit_price_cents", "reason"]
            st.dataframe(ddf[keep], use_container_width=True, hide_index=True)
    else:
        st.info("Aguardando primeiro tick do coletor...")

    if auto:
        st.markdown(
            "<script>setTimeout(()=>window.location.reload(),5000);</script>",
            unsafe_allow_html=True,
        )


# -------------------- Portfolio --------------------

with tabs[1]:
    if st.button("Refresh", key="refresh_pf"):
        st.rerun()

    snap = _get("/portfolio/snapshot", timeout=10)
    if "_error" in snap:
        st.error(snap["_error"])
    else:
        m1, m2, m3, m4 = st.columns(4)
        balance_dollars = (snap.get("balance_cents") or 0) / 100
        m1.metric("Saldo Kalshi", f"${balance_dollars:.2f}")
        pv = snap.get("portfolio_value_cents")
        m2.metric("Portfolio value", f"${(pv or 0) / 100:.2f}")
        m3.metric("Posições abertas", snap.get("summary", {}).get("open_positions_count", 0))
        m4.metric("Ordens descansando", snap.get("summary", {}).get("resting_orders_count", 0))

        # Equity curve
        equity = _get("/portfolio/equity?limit=2000").get("history", [])
        if equity:
            edf = pd.DataFrame(equity)
            edf["captured_at"] = pd.to_datetime(edf["captured_at"])
            edf["balance_dollars"] = edf["balance_cents"] / 100
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=edf["captured_at"], y=edf["balance_dollars"],
                mode="lines", name="Balance",
                line=dict(color="#19c37d"),
            ))
            fig.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10),
                              yaxis_title="USD")
            st.subheader("Equity curve")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Sem snapshots de equity ainda — o resolver service grava a cada 5 min.")

        # Open positions
        st.subheader("Posições abertas")
        if snap.get("open_positions"):
            pdf = pd.DataFrame(snap["open_positions"])
            cols = [c for c in ["ticker", "position_fp", "market_exposure_dollars",
                                "fees_paid_dollars", "realized_pnl_dollars",
                                "last_updated_ts"] if c in pdf.columns]
            st.dataframe(pdf[cols], use_container_width=True, hide_index=True)
        else:
            st.caption("nenhuma")

        # Resting orders
        st.subheader("Ordens descansando")
        if snap.get("resting_orders"):
            rdf = pd.DataFrame(snap["resting_orders"])
            keep = [c for c in ["order_id", "ticker", "side", "action",
                                "yes_price_dollars", "no_price_dollars",
                                "remaining_count_fp", "created_time"] if c in rdf.columns]
            st.dataframe(rdf[keep], use_container_width=True, hide_index=True)
        else:
            st.caption("nenhuma")

        # Real trade outcomes
        st.subheader("Histórico de trades reais (com PnL realizado)")
        outcomes = _get("/portfolio/outcomes?limit=200").get("outcomes", [])
        if outcomes:
            odf = pd.DataFrame(outcomes)
            odf["submitted_at"] = pd.to_datetime(odf["submitted_at"])
            cols = ["submitted_at", "ticker", "side", "count", "limit_price_cents",
                    "resolution", "realized_pnl_dollars", "fees_paid_dollars"]
            cols = [c for c in cols if c in odf.columns]
            st.dataframe(odf[cols], use_container_width=True, hide_index=True)

            settled = odf[odf["resolution"].isin(["yes", "no"])]
            if not settled.empty:
                c1, c2, c3, c4 = st.columns(4)
                wins = (settled["realized_pnl_dollars"] > 0).sum()
                c1.metric("Trades resolvidos", len(settled))
                c2.metric("Wins", int(wins))
                c3.metric("Win rate", f"{wins/len(settled)*100:.1f}%")
                c4.metric("PnL realizado",
                          f"${settled['realized_pnl_dollars'].sum():+.2f}")
        else:
            st.caption("Nenhum trade real registrado ainda.")


# -------------------- Radar --------------------

with tabs[2]:
    st.subheader("Radar semi-automático")
    st.caption("Snapshot diário de mercados com volume/liquidez, spread aceitável e deadline próximo. Observação apenas.")
    if st.button("Refresh", key="refresh_radar"):
        st.rerun()

    radar = _get("/markets/radar/recent?limit=200").get("candidates", [])
    if radar:
        rdf = pd.DataFrame(radar)
        rdf["captured_at"] = pd.to_datetime(rdf["captured_at"])
        rdf["close_time"] = pd.to_datetime(rdf["close_time"], errors="coerce")
        rdf["ttc_h"] = rdf["ttc_seconds"] / 3600
        rdf["yes_mid_pct"] = rdf["yes_mid"] * 100
        latest_scan = rdf["scan_id"].iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Scan", latest_scan)
        c2.metric("Candidatos salvos", len(rdf))
        c3.metric("Fecha <=24h", int((rdf["ttc_h"] <= 24).sum()))
        c4.metric("Spread mediano", f"{rdf['spread_cents'].median():.1f}c")

        category = st.multiselect(
            "Categoria",
            sorted([x for x in rdf["category"].dropna().unique().tolist() if x]),
            default=[],
        )
        view = rdf.copy()
        if category:
            view = view[view["category"].isin(category)]

        cols = [
            "rank", "score", "ticker", "title", "category", "ttc_h",
            "yes_mid_pct", "spread_cents", "volume", "liquidity", "close_time",
        ]
        st.dataframe(
            view[cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "score": st.column_config.NumberColumn("score", format="%.1f"),
                "ttc_h": st.column_config.NumberColumn("TTC h", format="%.1f"),
                "yes_mid_pct": st.column_config.NumberColumn("YES %", format="%.1f"),
                "spread_cents": st.column_config.NumberColumn("spread c", format="%.1f"),
            },
        )

        with st.expander("Histórico salvo"):
            hist = _get("/markets/radar/history?limit=1000").get("candidates", [])
            if hist:
                hdf = pd.DataFrame(hist)
                hdf["captured_at"] = pd.to_datetime(hdf["captured_at"])
                hdf["close_time"] = pd.to_datetime(hdf["close_time"], errors="coerce")
                hdf["ttc_h"] = hdf["ttc_seconds"] / 3600
                hdf["yes_mid_pct"] = hdf["yes_mid"] * 100
                hcols = ["captured_at", "scan_id", "rank", "score", "ticker",
                         "yes_mid_pct", "spread_cents", "volume", "ttc_h", "title"]
                st.dataframe(
                    hdf[hcols],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "score": st.column_config.NumberColumn("score", format="%.1f"),
                        "ttc_h": st.column_config.NumberColumn("TTC h", format="%.1f"),
                        "yes_mid_pct": st.column_config.NumberColumn("YES %", format="%.1f"),
                        "spread_cents": st.column_config.NumberColumn("spread c", format="%.1f"),
                    },
                )
    else:
        st.info("Ainda não há radar salvo. O job diário grava candidatos quando encontra mercados líquidos o bastante.")


# -------------------- Sinais --------------------

with tabs[3]:
    signals = _get("/signals/recent?limit=200").get("signals", [])
    if signals:
        df = pd.DataFrame(signals)
        df["captured_at"] = pd.to_datetime(df["captured_at"])
        st.dataframe(
            df[["captured_at", "ticker", "kind", "side", "phase",
                "probability", "delta", "notes"]],
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("Sem sinais ainda — estratégia atual dispara só em LATE explosion (Δ≥0.20).")


# -------------------- Trades --------------------

with tabs[4]:
    orders = _get("/orders/recent?limit=200").get("orders", [])
    if orders:
        df = pd.DataFrame(orders)
        df["submitted_at"] = pd.to_datetime(df["submitted_at"])
        df["dry_run"] = df["dry_run"].astype(bool)
        df["ok"] = df["ok"].astype(bool)
        st.dataframe(
            df[["submitted_at", "ticker", "side", "action", "count",
                "limit_price_cents", "ok", "dry_run", "error"]],
            use_container_width=True, hide_index=True,
        )
        st.caption(f"Total: {len(df)} ordens / "
                   f"reais: {(~df['dry_run']).sum()} / sucesso: {df['ok'].sum()}")
    else:
        st.info("Nenhuma ordem enviada ainda.")


# -------------------- Backtest --------------------

with tabs[5]:
    st.subheader("Backtest single-run (fee-aware)")
    c1, c2, c3, c4 = st.columns(4)
    delta = c1.slider("Explosion Δ", 0.05, 0.40, SETTINGS.prob_explosion_delta, 0.01)
    win = c2.slider("Explosion window (s)", 10, 300, 60, 10)
    plateau = c3.slider("Plateau threshold", 0.55, 0.99, SETTINGS.prob_plateau_threshold, 0.01)
    plateau_s = c4.slider("Plateau seconds", 30, 600, min(600, SETTINGS.prob_plateau_seconds), 10)
    phase_choice = st.selectbox("Phase filter", ["(any)", "early", "middle", "late"])

    if st.button("Run backtest", type="primary"):
        result = run_backtest(SETTINGS.db_path, BacktestParams(
            explosion_delta=delta, explosion_window_seconds=win,
            plateau_threshold=plateau, plateau_seconds=plateau_s,
            min_phase=None if phase_choice == "(any)" else phase_choice,
        ))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades", result["trades_count"])
        m2.metric("Settled", result["settled_count"])
        wr = result["win_rate"]
        m3.metric("Win rate", f"{wr*100:.1f}%" if wr is not None else "—")
        m4.metric("Total PnL", f"${result['total_pnl']:+.2f}")
        if result["trades"]:
            st.dataframe(pd.DataFrame(result["trades"]),
                         use_container_width=True, hide_index=True)
        else:
            st.warning("Sem trades — afrouxe thresholds ou troque a fase.")

    st.divider()
    st.subheader("Portfolio simulator (capital-aware)")
    st.caption("Trade-by-trade equity curve com cooldown, daily cap e kill-switch.")
    c1, c2, c3, c4 = st.columns(4)
    init_cap = c1.number_input("Capital inicial ($)", 10.0, 10000.0, 100.0, 10.0)
    sim_delta = c2.slider("Explosion Δ (sim)", 0.05, 0.40, 0.20, 0.01)
    sim_phase = c3.selectbox("Phase (sim)", ["late", "early", "middle", "any"], index=0)
    sim_contra = c4.checkbox("Contrarian (fade signal)", value=False)
    c5, c6, c7, c8 = st.columns(4)
    sizing = c5.selectbox("Sizing", ["fixed", "fraction", "kelly_fraction"], index=0)
    fixed_ct = c6.number_input("Fixed contracts", 1, 50, 1, 1)
    frac = c7.slider("Fraction of capital", 0.01, 0.20, 0.05, 0.01)
    cooldown = c8.number_input("Cooldown (s)", 0, 3600, 300, 30)
    c9, c10, c11 = st.columns(3)
    daily_cap = c9.number_input("Max trades/day", 1, 20, 6, 1)
    kill_n = c10.number_input("Kill after N losses", 2, 20, 5, 1)
    plat_th = c11.slider("Plateau threshold (0.99 = disabled)", 0.55, 0.99, 0.99, 0.01)

    if st.button("Run simulation", type="primary", key="run_sim"):
        params = SimParams(
            explosion_delta=sim_delta,
            min_phase=None if sim_phase == "any" else sim_phase,
            contrarian=sim_contra,
            initial_capital_dollars=init_cap,
            sizing_mode=sizing,
            fixed_contracts=int(fixed_ct),
            capital_fraction=frac,
            cooldown_seconds=int(cooldown),
            max_trades_per_day=int(daily_cap),
            kill_after_consecutive_losses=int(kill_n),
            plateau_threshold=plat_th,
            plateau_seconds=120 if plat_th < 0.99 else 99999,
        )
        result = run_simulation(SETTINGS.db_path, params)
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Capital final", f"${result.final_capital:.2f}",
                  f"{result.pct_return:+.2f}%")
        m2.metric("Trades", result.trades_total)
        m3.metric("Win rate",
                  f"{(result.win_rate or 0)*100:.1f}%" if result.win_rate is not None else "—")
        m4.metric("Max drawdown", f"{result.max_drawdown_pct:.2f}%")
        m5.metric("Sharpe (proxy)",
                  f"{result.sharpe_proxy:.2f}" if result.sharpe_proxy is not None else "—")
        if result.killed_at:
            st.error(f"⚠️ Kill-switch acionado em {result.killed_at} "
                     f"({kill_n} perdas consecutivas)")

        if result.equity_curve:
            ec = pd.DataFrame(result.equity_curve, columns=["t", "capital"])
            ec["t"] = pd.to_datetime(ec["t"])
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=ec["t"], y=ec["capital"],
                                     mode="lines+markers",
                                     line=dict(color="#19c37d")))
            fig.add_hline(y=init_cap, line_dash="dash",
                          annotation_text="capital inicial")
            fig.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10),
                              yaxis_title="USD")
            st.plotly_chart(fig, use_container_width=True)
        if result.trades:
            st.dataframe(pd.DataFrame(result.trades),
                         use_container_width=True, hide_index=True, height=300)

    st.divider()
    st.subheader("Walk-forward (grid)")
    folds = st.slider("Folds", 2, 8, 4)
    if st.button("Run walk-forward"):
        grid = [
            BacktestParams(explosion_delta=d, plateau_threshold=p, plateau_seconds=plateau_s)
            for d in (0.10, 0.15, 0.20) for p in (0.60, 0.65, 0.70)
        ]
        wf = walk_forward(SETTINGS.db_path, grid, folds=folds)
        if wf:
            df = pd.DataFrame([
                {**r["params"], "fold": r["fold"], "trades": r["trades_count"],
                 "settled": r["settled_count"], "win_rate": r["win_rate"],
                 "avg_pnl": r["avg_pnl"], "total_pnl": r["total_pnl"]}
                for r in wf
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.warning("Sem dados suficientes para walk-forward.")


st.caption(f"API: {API_URL} · DB: {SETTINGS.db_path} · "
           f"real_orders={SETTINGS.enable_real_orders} · "
           f"strategy=late-explosion(Δ={SETTINGS.prob_explosion_delta}) "
           f"· {datetime.now().isoformat(timespec='seconds')}")
