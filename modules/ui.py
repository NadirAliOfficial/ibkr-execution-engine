import logging
from flask import Flask, render_template, request, jsonify

logger = logging.getLogger(__name__)


def create_app(execution_engine, signal_adapter, broker, config):
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )

    @app.route("/")
    def index():
        defaults = config["defaults"]
        return render_template("index.html", defaults=defaults)

    @app.route("/api/execute", methods=["POST"])
    def execute_trade():
        try:
            data = request.json
            symbol = data["symbol"].upper().strip()
            side = data["side"].upper().strip()
            entry_price = float(data["entry_price"])
            stop_price = float(data["stop_price"])
            risk_amount = float(data["risk_amount"])

            trade = signal_adapter.process_manual_signal(
                symbol, side, entry_price, stop_price, risk_amount
            )

            return jsonify({
                "success": True,
                "trade_id": trade["trade_id"],
                "total_shares": trade["total_shares"],
                "bracket_sizes": trade["bracket_sizes"],
                "tp_prices": trade["tp_prices"],
                "state": trade["state"],
            })
        except Exception as e:
            logger.error(f"Execute error: {e}")
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/trades")
    def get_trades():
        trades = execution_engine.get_status()
        return jsonify(trades)

    @app.route("/api/trades/active")
    def get_active_trades():
        trades = execution_engine.get_active_trades()
        return jsonify(trades)

    @app.route("/api/trade/<trade_id>")
    def get_trade(trade_id):
        trade = execution_engine.get_status(trade_id)
        if not trade:
            return jsonify({"error": "Trade not found"}), 404
        return jsonify(trade)

    @app.route("/api/trade/<trade_id>/cancel", methods=["POST"])
    def cancel_trade(trade_id):
        try:
            execution_engine.cancel_trade(trade_id)
            return jsonify({"success": True, "message": f"Trade {trade_id} cancelled"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/status")
    def connection_status():
        return jsonify({"connected": broker.is_connected})

    @app.route("/api/webhook", methods=["POST"])
    def webhook():
        """Phase 2: Webhook endpoint for external signals."""
        try:
            payload = request.json
            trade = signal_adapter.process_webhook_signal(payload)
            return jsonify({"success": True, "trade_id": trade["trade_id"]})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    return app
