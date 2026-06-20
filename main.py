import json
import logging
import threading
from modules.broker import IBKRBroker
from modules.risk import RiskManager
from modules.order_manager import OrderManager
from modules.execution import ExecutionEngine
from modules.signal_adapter import SignalAdapter
from modules.ui import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("execution_engine.log"),
    ],
)
logger = logging.getLogger(__name__)


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    config = load_config()

    # Initialize modules
    broker = IBKRBroker(config)
    risk_manager = RiskManager(config)
    order_manager = OrderManager(config, broker, risk_manager)
    execution_engine = ExecutionEngine(broker, risk_manager, order_manager)
    signal_adapter = SignalAdapter(execution_engine)

    # Connect to IBKR
    logger.info("Connecting to IBKR Gateway...")
    if not broker.connect():
        logger.error("Could not connect to IBKR Gateway. Make sure it's running.")
        logger.info(f"Config: host={config['ibkr']['host']} port={config['ibkr']['port']}")
        logger.info("Starting UI without IBKR connection (for testing)...")

    # Recover any existing trade state
    if broker.is_connected:
        order_manager.recover_state()
        broker.set_verify_callback(order_manager.verify_stops)
        broker.set_price_monitor_callback(order_manager.check_1r_protections)
        broker.set_runner_monitor_callback(order_manager.check_runner_trailing)

    # Start Flask UI in background thread
    app = create_app(execution_engine, signal_adapter, broker, config)
    ui_config = config["ui"]
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host=ui_config["host"],
            port=ui_config["port"],
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
    )
    flask_thread.start()
    logger.info(f"UI started at http://{ui_config['host']}:{ui_config['port']}")

    # Run IB event loop in main thread — processes commands from Flask via queue
    try:
        broker.run_loop()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        broker.disconnect()


if __name__ == "__main__":
    main()
