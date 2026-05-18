# trade2

Operador limpo de trades na **Kalshi** para o mercado **BTC 15min** (`KXBTC15M-*`).
Coleta tick a tick + spot BTC-USD da Coinbase, detecta sinais (explosões / plateaus
de probabilidade), expõe API + dashboard para acompanhar, backtest fee-aware
e simulação capital-aware com gates de segurança duros antes de qualquer ordem real.

> ⚠️ **Status estratégico (2026-05-17)**: sweep estatístico sobre 687 mercados
> resolvidos mostra que **nenhuma variante da estratégia explosion/plateau tem
> CI inferior acima do breakeven após fees**. `ENABLE_REAL_ORDERS=false` por
> default — não ligue sem novo edge validado. Detalhes em [AI_INDEX.md](AI_INDEX.md).

## Quick start

```bash
cd /home/ubuntu/trade2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Verifica auth + lista mercado ativo
python scripts/check_auth.py

# Dry-run de uma ordem
python scripts/place_test_order.py

# Ordem real mínima (cap 90¢)
python scripts/place_test_order.py --confirm-real

# API
bash scripts/run_api.sh

# Dashboard (outro terminal)
bash scripts/run_dashboard.sh
```

## Estrutura

```
trade2/
  app/
    api/             FastAPI + WebSocket
    application/     Casos de uso (collector, signals, order_gateway, market_discovery)
    domain/          Entidades puras
    infrastructure/  Kalshi REST client (RSA-PSS-SHA256) + SQLite
    config.py        Settings via .env
  backtest/          Engine de backtest + walk-forward
  dashboard/         Streamlit (Live / Sinais / Trades / Backtest)
  scripts/           check_auth, place_test_order, run_api, run_dashboard
  secrets/           kalshi_api.keys (RSA private key, gitignored)
  data/              SQLite (gitignored)
  tests/
```

## Endpoints

- `GET  /health` — versão e flag de real_orders
- `GET  /market/live` — último tick + fase + sinal
- `GET  /signals/recent?limit=100`
- `GET  /orders/recent?limit=100`
- `GET  /ticks/{ticker}?limit=500`
- `GET  /portfolio/balance`
- `POST /orders` — `{ticker, side, action, count, limit_price_cents, dry_run}`
- `WS   /ws/live` — push de ticks e sinais

## Safety gates

Três camadas antes de qualquer ordem real ir pra Kalshi:
1. `dry_run` no payload do request
2. `ENABLE_REAL_ORDERS` no `.env` (default `false`)
3. `MAX_ORDER_COST_CENTS` (default 90 = $0.90)

## Estratégia (resumo)

A maioria do tempo, a probabilidade de mercados BTC 15min fica perto de 50%.
Dois sinais são monitorados:

- **Explosion**: a prob mudou ≥ `PROB_EXPLOSION_DELTA` (0.15) em
  `explosion_window_seconds` (60s). Lado = direção da explosão.
- **Plateau**: a prob ficou sustentada ≥ `PROB_PLATEAU_THRESHOLD` (0.60)
  por `PROB_PLATEAU_SECONDS` (120s). Lado = YES (ou NO espelhado).

Ambos os sinais geram um `Signal` que pode disparar ordem (não automático ainda —
ordens hoje são enviadas explicitamente via `/orders` ou script).
