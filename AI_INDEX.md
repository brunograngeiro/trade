# AI_INDEX — trade2

Índice vivo do projeto para próximas sessões de IA. Sempre que mudar algo
relevante, atualizar este documento (preserva contexto, economiza tokens).

## Estado atual (2026-05-17)

- **Stack**: FastAPI + httpx + websockets + cryptography + SQLite + Streamlit + Plotly
- **Foco operacional**: KXBTC15M-* na Kalshi (uma série, 15 min cada janela)
- **Auth Kalshi**: RSA-PSS-SHA256 em [app/infrastructure/kalshi/signer.py](app/infrastructure/kalshi/signer.py)
  - chave pública: `KALSHI_API_KEY_ID` (UUID em `.env`)
  - chave privada: `secrets/kalshi_api.keys` (PEM RSA)
  - base URL: `https://api.elections.kalshi.com/trade-api/v2`
- **Auth verificada**: `python scripts/check_auth.py` retorna balance + posições reais
- **Primeira ordem real**: 2026-05-16 21:40 UTC, YES 1ct @ 43¢, KXBTC15M-26MAY161745-45,
  fill imediato; saldo $11.20 → $10.77
- **Real orders default**: OFF (`ENABLE_REAL_ORDERS=false`). Scripts forçam ON temporariamente
  com `--confirm-real`.
- **Produção**: 3 services systemd ativos (`trade2-api`, `trade2-resolver`, `trade2-dashboard`).
  Projeto antigo `trade.*` parado e disabled em 2026-05-17.
- **Dados migrados**: 102.860 ticks + 54 resoluções vindos de `trade/data/trade.sqlite3`
  via [scripts/migrate_from_trade.py](scripts/migrate_from_trade.py).

## Estratégia atual — ⚠️ SEM EDGE ESTATÍSTICA COMPROVADA

**Atualização 2026-05-17**: o resolver service sincronizou 687 mercados
(não 54 do dataset inicial). Re-rodando o sweep amplo:

| Variante | n | Win % (CI lo) | Avg entry | Avg PnL | Total |
|---|---|---|---|---|---|
| late Δ=0.20 follow (default antigo) | 338 | 67.2% (62.0%) | $0.71 | -$0.04 | -$14.49 |
| Δ=0.20 early CONTRA (melhor) | 359 | 34.3% (29.5%) | $0.31 | **+$0.01** | +$3.69 |
| plateau ≥0.80 120s late follow | 559 | 95.7% (93.7%) | $0.96 | -$0.01 | -$5.30 |

**Nenhuma variante tem CI lower bound acima do breakeven**. A tese
"explosion/plateau como edge" está empiricamente refutada com n>300 por
variante. O único positivo marginal (~+1¢/trade) é **contrarian/fade
em early phase** — mas CI ainda fica abaixo do breakeven.

**Recomendação operacional**: `ENABLE_REAL_ORDERS=false` permanente até
encontrarmos um edge real. Próxima fronteira: latency arb Coinbase BTC-USD
vs Kalshi mid (infra já está pronta, ver `spot_ticks` + `/spot/recent`).

Comando para re-auditar: `python scripts/data_audit.py` + `python scripts/strategy_sweep.py`.

Fee model: `ceil(0.07 × C × P × (1-P))` em dólares (Kalshi BTC tabela
padrão). Função `kalshi_fee_dollars` em [backtest/engine.py](backtest/engine.py).

## Mapa de arquivos

| Camada | Arquivo | Função |
|---|---|---|
| config | [app/config.py](app/config.py) | pydantic-settings, lê `.env` |
| domain | [app/domain/entities.py](app/domain/entities.py) | Market, Tick, Phase, Signal, Order, Side |
| kalshi | [app/infrastructure/kalshi/signer.py](app/infrastructure/kalshi/signer.py) | sign_request(method, signed_path) → (ts, sig) |
| kalshi | [app/infrastructure/kalshi/client.py](app/infrastructure/kalshi/client.py) | KalshiClient async (`get_markets`, `get_market`, `get_orderbook`, `get_balance`, `place_order`, etc.) |
| kalshi | [app/infrastructure/kalshi/mapper.py](app/infrastructure/kalshi/mapper.py) | v2 payload (`*_dollars` strings) → Market |
| coinbase | [app/infrastructure/coinbase/client.py](app/infrastructure/coinbase/client.py) | BTC-USD ticker (público, sem auth) |
| db | [app/infrastructure/db/sqlite.py](app/infrastructure/db/sqlite.py) | ticks, signals, orders, market_resolutions, spot_ticks, balance_snapshots, trade_outcomes |
| app | [app/application/market_discovery.py](app/application/market_discovery.py) | current_market(now) |
| app | [app/application/signals.py](app/application/signals.py) | SignalEngine (explosion + plateau) |
| app | [app/application/order_gateway.py](app/application/order_gateway.py) | 3 gates de segurança antes do `place_order` |
| app | [app/application/collector.py](app/application/collector.py) | loop async, polling p `COLLECTOR_POLL_SECONDS`, salva tick Kalshi + Coinbase spot |
| app | [app/application/portfolio_snapshot.py](app/application/portfolio_snapshot.py) | snapshot periódico balance + posições para equity curve |
| app | [app/application/outcome_tracker.py](app/application/outcome_tracker.py) | reconcilia ordens reais com `market_resolutions` |
| app | [app/application/resolution_sync.py](app/application/resolution_sync.py) | loop 5min: sync resoluções + reconcile outcomes + snapshot equity |
| api | [app/api/main.py](app/api/main.py) | FastAPI lifespan, rotas REST + WS `/ws/live` |
| api | [app/api/schemas.py](app/api/schemas.py) | Pydantic request/response |
| backtest | [backtest/engine.py](backtest/engine.py) | `run_backtest`, `walk_forward`, `BacktestParams`, `kalshi_fee_dollars` |
| backtest | [backtest/portfolio_sim.py](backtest/portfolio_sim.py) | `run_simulation`, `SimParams` — equity curve, cooldown, daily cap, kill-switch, sizing |
| dashboard | [dashboard/app.py](dashboard/app.py) | Streamlit 4 abas |
| scripts | [scripts/check_auth.py](scripts/check_auth.py) | valida auth + lista mercado ativo |
| scripts | [scripts/place_test_order.py](scripts/place_test_order.py) | `--confirm-real` para real, sem flag = dry-run |
| scripts | [scripts/migrate_from_trade.py](scripts/migrate_from_trade.py) | importa ticks/resoluções do `trade.sqlite3` antigo |
| scripts | [scripts/data_audit.py](scripts/data_audit.py) | resumo + CI Wilson da estratégia default |
| scripts | [scripts/strategy_sweep.py](scripts/strategy_sweep.py) | sweep amplo (explosão + plateau + CONTRA) |
| scripts | [scripts/run_backtest_report.py](scripts/run_backtest_report.py) | grid × fase × fees, texto |
| scripts | [scripts/run_portfolio_sim.py](scripts/run_portfolio_sim.py) | comparação de variantes com capital tracking |
| scripts | [scripts/run_resolution_sync.py](scripts/run_resolution_sync.py) | entrypoint do service `trade2-resolver` |

## Convenções

- **Preços** internamente em **dólares (float)**. API Kalshi v2 retorna strings `*_dollars`.
- **Cents** apenas em `limit_price_cents` (1-99) ao montar ordem.
- **Timestamps** sempre UTC com tz-aware (`datetime.now(timezone.utc)`).
- **Async-first**: REST e WS via `httpx.AsyncClient` / `websockets`.
- **Persistência**: SQLite com schema auto-criado em `Database.__init__`.

## Sinais

- **Explosion**: |prob_now - prob_window_start| ≥ `PROB_EXPLOSION_DELTA` (0.15)
  dentro de `explosion_window_seconds` (60). Lado = direção do delta.
- **Plateau**: prob ≥ `PROB_PLATEAU_THRESHOLD` (0.60) por ≥ `PROB_PLATEAU_SECONDS` (120s).
  Reinicia o timer se o lado mudar.

## Fases do mercado

`Market.phase_at(now)` → divide `[open_time, close_time]` em três:
- **early**: 0–34%
- **middle**: 34–67%
- **late**: 67–100%
- **expired**: ratio ≥ 1.0

## Tabelas SQLite

```sql
ticks(id, ticker, captured_at, yes_bid, yes_ask, no_bid, no_ask, last_price, volume)
signals(id, ticker, captured_at, kind, side, phase, probability, delta, notes)
orders(id, submitted_at, ticker, side, action, count, limit_price_cents,
       client_order_id, dry_run, ok, error, raw)
market_resolutions(ticker PRIMARY KEY, resolved_at, result, raw)
```

## Simulador capital-aware (2026-05-17)

[backtest/portfolio_sim.py](backtest/portfolio_sim.py) faz o que o backtest plano
não faz: trackeia capital trade-a-trade com cooldown, daily cap, kill-switch e
sizing (fixed/fraction/half-Kelly). Resultados sobre os 687 mercados resolvidos:

| Variante | Final ($100→) | Trades | Win% | MaxDD | Killed |
|---|---|---|---|---|---|
| late Δ=0.20 follow (default) | $93.88 | 27 | 48.1% | 6.12% | sim, 2026-05-14 |
| late Δ=0.10 follow | $97.61 | 48 | 66.7% | 4.07% | — |
| early Δ=0.20 CONTRA | $99.41 | 9 | 22.2% | 1.15% | sim |
| plateau ≥0.80 120s late | $99.29 | 48 | 66.7% | 1.53% | — |
| fraction 5% sizing (default) | **$58.96** | 27 | 48.1% | **41%** | sim |

**Lição**: com cooldown 5min e daily cap 6, a estratégia DEFAULT perde 6% em 7
dias e é morta pelo kill-switch. Fraction sizing AMPLIFICA perdas em edge
negativa — Kelly é brutal nos dois sentidos. **Não ligar `ENABLE_REAL_ORDERS=true`**.

## YES/NO mechanics (Kalshi)

- Cada mercado é binário; resolve em YES ou NO no `close_time`
- BUY YES a `yes_ask` cents → recebe $1 se YES vencer, $0 se NO
- BUY NO a `no_ask` cents → recebe $1 se NO vencer, $0 se YES
- `yes_ask + no_bid ≈ 1.00` (sem arb intra-livro)
- Para SAIR antes do settle, SELL no bid do lado oposto (vender YES = adicionar oferta de NO)
- Fees na entrada: `ceil(0.07 × count × P × (1-P))` em $; sem fee no payout
- **Contrarian** (fade) = sinal diz YES → compramos NO (mean reversion)

## Timeframes Kalshi BTC disponíveis (auditado 2026-05-17)

| Série | Tipo | Window | Estrutura |
|---|---|---|---|
| `KXBTC15M` | up/down direcional | 15 min | binary: BTC subiu nos últimos 15min? |
| ~~`KXBTC1H`~~ | — | — | **NÃO existe** |
| ~~`KXBTC4H`~~ | — | — | **NÃO existe** |
| `KXBTCD` | strike (digital option) | 1 dia | binary: BTC > $X às 5pm EDT? Múltiplos strikes/dia |
| `KXBTCMAXY` | strike (digital option) | 1 ano | binary: BTC > $X até dec/31? |

Conclusão: única direcional "up/down" é `KXBTC15M`. Daily e yearly são strike-based
(cash-or-nothing digitals), estrutura diferente, exigem modelo de volatilidade para
precificar — melhor combinação com Coinbase spot já integrado.

## Próximos passos sugeridos

1. **NÃO ligar auto-trading da estratégia atual** — sweep + simulador mostram edge negativa.
2. **Latency arb Coinbase ↔ Kalshi**: usar `spot_ticks` (já coletados) para derivar
   prob teórica via volatility model e comparar com Kalshi mid. Possível edge real
   segundo research público.
3. **Estudar KXBTCD (daily strikes)**: estrutura digital cash-or-nothing favorece
   sinais baseados em distância (BTC-spot vs strike) com tempo p/ desenvolvimento.
   Bastaria adicionar `KALSHI_SERIES_TICKER=KXBTCD` num collector paralelo.
4. **WebSocket Kalshi**: trocar REST polling por `orderbook_delta` + `fill` +
   `market_lifecycle_v2`. 5 conn max/user. Reduz lag p/ <500ms.
5. **Exit-to-close orders**: hoje `OrderGateway` só faz BUY. Adicionar SELL no bid
   do lado oposto para encerrar posição antes do settle (útil em latency arb).

## Operação na VPS

```bash
# status
systemctl status trade2-api trade2-resolver trade2-dashboard

# logs em tempo real
journalctl -u trade2-api -f
tail -f /home/ubuntu/trade2/data/api.log

# health
curl http://127.0.0.1:8020/health
curl http://127.0.0.1:8020/health/collector

# dashboard
http://<VPS_IP>:8502

# restart
sudo systemctl restart trade2-api trade2-dashboard

# reinstalar units (após editar deployment/systemd/*.service)
bash deployment/systemd/install.sh
```

## Gotchas

- Kalshi v2 retorna preços como **string** com sufixo `_dollars` (`"0.4300"`).
  Mapper agora usa isso, não cente integer.
- O endpoint `/markets` (list) **omite** alguns campos de book que `/markets/{ticker}` retorna —
  sempre re-fetch via `get_market(ticker)` antes de decidir preço de ordem.
- `client_order_id` deve ser idempotente: gerado em `OrderGateway.build` com `uuid.uuid4().hex[:16]`.
