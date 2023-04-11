# IBKR Execution Engine

A modular, reliable execution engine for Interactive Brokers (IBKR) with a local Flask-based control panel. Designed as Phase 1 of a larger automated trading system.

## Features

- **Risk-Based Position Sizing** — calculates shares from adjustable dollar risk, entry, and stop price
- **Bracket Order System** — TP1 (50%), TP2 (40%), Runner (10%) with automatic management
- **Smart Order Management** — breakeven stop after TP1 fill, trailing stop on runner after TP2
- **Partial Fill Handling** — uses IBKR execution callbacks (`orderStatus` / `execDetails`) to track real fills
- **State Persistence** — JSON-based trade state with full recovery after disconnect/restart
- **Auto-Reconnect** — detects IB Gateway drops and reconnects with state rebuild
- **Local Control Panel** — Flask web UI for manual trade execution and monitoring
- **Phase 2 Ready** — signal adapter with webhook endpoint for scanner/TradingView integration

## Control Panel

Dark-themed local web UI running at `http://127.0.0.1:5000`

### Input Fields
| Field | Description |
|-------|-------------|
| Ticker | Stock symbol (e.g., AAPL) |
| Side | BUY (Long) / SELL (Short) |
| Entry Price | Limit entry price |
| Stop Price | Stop loss price |
| Risk ($) | Dollar risk per trade — adjustable at runtime, no restart needed |

### Controls
- **Execute Button** — places bracket orders after input validation
- **Cancel Button** — cancels all orders for a specific trade

### Status Display
| Display | Description |
|---------|-------------|
| Connection Status | Live IBKR Gateway connection indicator (green/red) |
| Live Order Preview | Position size, risk/share, bracket splits shown before execution |
| Trade Table | All trades with state badges, fill progress, breakeven status |
| State Badges | `pending` → `entry_placed` → `entry_filled` → `tp1_filled` → `tp2_filled` → `runner_active` → `closed` |
| Fill Progress | TP1 and TP2 filled qty vs target qty |
| Breakeven | Shows whether stop has been moved to breakeven |

## Architecture

```
ibkr-execution-engine/
├── main.py                  # Entry point — wires all modules
├── config.json              # Configuration (IBKR, defaults, UI)
├── requirements.txt         # Dependencies
├── modules/
│   ├── broker.py            # IBKR connection, order placement, reconnect, callbacks
│   ├── risk.py              # Position sizing, bracket splits, TP price calculation
│   ├── order_manager.py     # Order lifecycle, fill tracking, state persistence
│   ├── execution.py         # Trade orchestration, event handling
│   ├── signal_adapter.py    # Signal input layer (manual now, webhook for Phase 2)
│   └── ui.py                # Flask routes and API endpoints
├── templates/
│   └── index.html           # Control panel frontend
└── state/
    └── trades.json          # Persisted trade state
```

### Module Responsibilities

| Module | Role |
|--------|------|
| `broker.py` | IBKR connection via `ib_insync`, order placement, execution callbacks, auto-reconnect |
| `risk.py` | Position sizing from risk $, bracket quantity splits, TP price calculation |
| `order_manager.py` | Full order lifecycle — create, execute, track fills, move stops, persist state |
| `execution.py` | Orchestrates trade flow, validates inputs, routes fill/status events |
| `signal_adapter.py` | Thin input layer — manual UI input now, webhook/API for Phase 2 |
| `ui.py` | Flask web server with REST API and control panel |

## Order Flow

```
Input (ticker, side, entry, stop, risk $)
    │
    ▼
Calculate position size → risk $ / |entry - stop|
    │
    ▼
Split into brackets → TP1 (50%) + TP2 (40%) + Runner (10%)
    │
    ▼
Place orders → Entry (limit) + TP1 (limit) + TP2 (limit) + Stop (stop)
    │
    ▼
Monitor fills via IBKR callbacks
    │
    ├── TP1 filled → Move stop to breakeven, reduce stop qty
    ├── TP2 filled → Cancel stop, place trailing stop for runner
    └── Stop/trailing filled → Trade closed
```

## Reliability

- **Partial fills**: TP1 breakeven triggers only after actual 50% fill (not partial assumption)
- **Disconnects**: Auto-reconnect with configurable retries, rebuilds trade state from persistence + IBKR open orders
- **No duplicates**: State sync prevents duplicate order placement after reconnect
- **Order consistency**: Parent-child order ID tracking ensures bracket integrity

## Setup

### Prerequisites
- Python 3.9+
- IB Gateway or TWS
- IBKR account (paper or live)

### Install
```bash
cd ibkr-execution-engine
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure IB Gateway
1. **API Settings**: Enable ActiveX and Socket Clients
2. **Port**: `4002` (paper) or `4001` (live)
3. **Localhost only**: Enable for security

### Configure Engine
Edit `config.json`:
```json
{
    "ibkr": {
        "port": 4002,
        "client_id": 1
    },
    "defaults": {
        "risk_per_trade": 100.0,
        "tp1_pct": 0.50,
        "tp2_pct": 0.40,
        "runner_pct": 0.10,
        "trailing_stop_pct": 0.02
    }
}
```

### Run
```bash
python main.py
```
Open `http://127.0.0.1:5000`

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Control panel UI |
| `POST` | `/api/execute` | Execute a new trade |
| `GET` | `/api/trades` | All trades with status |
| `GET` | `/api/trades/active` | Active trades only |
| `GET` | `/api/trade/<id>` | Single trade status |
| `POST` | `/api/trade/<id>/cancel` | Cancel a trade |
| `GET` | `/api/status` | IBKR connection status |
| `POST` | `/api/webhook` | Webhook signal input (Phase 2) |

## Roadmap

- **Phase 1** (current): Manual execution engine with UI
- **Phase 2**: Scanner + signal layer (Finviz, TradingView webhooks)
- **Phase 3**: Automated execution + AI decision layer

## Tech Stack

- **Python** — core language
- **ib_insync** — IBKR API wrapper
- **Flask** — local web UI and API
- **JSON** — state persistence
<!-- updated: 2023-04-11-r01 -->
