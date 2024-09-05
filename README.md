# IBKR Execution Engine

A modular Flask-based execution engine for Interactive Brokers. Receives trade signals via HTTP, manages order lifecycle, and exposes a local control panel for monitoring and manual overrides.

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-000000?style=flat&logo=flask&logoColor=white)
![IBKR](https://img.shields.io/badge/Interactive%20Brokers-TWS%20API-red?style=flat)

## Architecture

```
Signal Source (webhook / strategy)
        │
        ▼
  Flask HTTP Server  ──►  Order Manager  ──►  ib_insync / TWS API
        │                                            │
        ▼                                            ▼
  Control Panel UI                         Interactive Brokers
```

## Features

- REST API endpoint for incoming trade signals
- Bracket order support (entry + stop-loss + take-profit)
- Order status tracking and position monitoring
- Local control panel for live order view and manual cancel
- Configurable risk parameters per symbol

## Setup

1. Start IB Gateway or TWS (paper or live account, port 7497)
2. Install dependencies:

```bash
pip install flask ib_insync pandas
```

3. Run the engine:

```bash
python main.py
```

## API Endpoints

| Method | Path       | Body                                    | Description        |
|--------|------------|-----------------------------------------|--------------------|
| POST   | `/signal`  | `{"symbol":"AAPL","action":"buy","qty":10}` | Place order    |
| GET    | `/status`  | —                                       | Open positions     |
| POST   | `/cancel`  | `{"orderId": 123}`                      | Cancel order       |

## License

MIT

