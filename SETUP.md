# IBKR Execution Engine - Setup Guide

## Prerequisites

- Python 3.9+
- IB Gateway or TWS (Trader Workstation)
- IBKR account (paper or live)

## Installation

```bash
cd ibkr-execution-engine
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## IB Gateway Configuration

1. Download and install [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php)
2. Login with your IBKR credentials
3. Go to **Configure > Settings > API**:
   - Enable **ActiveX and Socket Clients**
   - Set **Socket port**: `4002` (paper) or `4001` (live)
   - Enable **Allow connections from localhost only**
   - Disable **Read-Only API** (if you want to place orders)

## Configuration

Edit `config.json`:

```json
{
    "ibkr": {
        "host": "127.0.0.1",
        "port": 4002,        // 4002 = paper, 4001 = live
        "client_id": 1,
        "account": "",       // leave empty for default
        "timeout": 30
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

## Running

```bash
source venv/bin/activate
python main.py
```

Open browser: `http://127.0.0.1:5000`

## Usage

1. Enter ticker symbol (e.g., AAPL)
2. Select side (BUY/SELL)
3. Enter entry price and stop price
4. Adjust risk per trade ($)
5. Review the order preview
6. Click **Execute Trade**

## Architecture

```
ibkr-execution-engine/
├── main.py                 # Entry point
├── config.json             # Configuration
├── modules/
│   ├── broker.py           # IBKR connection & order placement
│   ├── risk.py             # Position sizing & bracket calculation
│   ├── order_manager.py    # Order lifecycle, fills, state persistence
│   ├── execution.py        # Trade orchestration
│   ├── signal_adapter.py   # Signal input layer (manual/webhook)
│   └── ui.py               # Flask web UI
├── templates/
│   └── index.html          # Control panel UI
└── state/
    └── trades.json         # Persisted trade state
```

## Paper Trading

Always test with paper trading first (port 4002). Verify:
- Position sizing matches expected values
- Bracket orders appear correctly in TWS/Gateway
- TP1 fill triggers breakeven stop move
- Runner trailing stop activates after TP2
- Reconnect recovers state properly
