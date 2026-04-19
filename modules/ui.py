import logging
from flask import Flask, render_template, request, jsonify

logger = logging.getLogger(__name__)


def create_app(execution_engine, signal_adapter, broker, config):
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )

    order_manager = execution_engine.orders

    @app.route("/")
    def index():
        defaults = config["defaults"]
        modes = config.get("modes", {})
        default_mode = config.get("default_mode", "conservative")
        return render_template("index.html", defaults=defaults, modes=modes,
                               default_mode=default_mode)

    @app.route("/api/execute", methods=["POST"])
    def execute_trade():
        try:
            data = request.json
            symbol = data["symbol"].upper().strip()
            side = data["side"].upper().strip()
            entry_price = float(data["entry_price"])
            stop_price = float(data["stop_price"])
            risk_amount = float(data["risk_amount"])
            mode = data.get("mode") or None
            session_mode = data.get("session_mode") or None

            trade = signal_adapter.process_manual_signal(
                symbol, side, entry_price, stop_price, risk_amount,
                mode=mode, session_mode=session_mode
            )

            return jsonify({
                "success": True,
                "trade_id": trade["trade_id"],
                "total_shares": trade["total_shares"],
                "bracket_sizes": trade["bracket_sizes"],
                "tp_prices": trade["tp_prices"],
                "state": trade["state"],
                "mode": trade.get("mode"),
                "session_mode": trade.get("session_mode"),
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
        session_state = order_manager.get_session_state()
        session_mode = order_manager.get_session_mode()
        return jsonify({
            "connected": broker.is_connected,
            "session_state": session_state,
            "session_mode": session_mode,
        })

    @app.route("/api/modes")
    def get_modes():
        modes = config.get("modes", {})
        default_mode = config.get("default_mode", "conservative")
        return jsonify({"modes": modes, "default_mode": default_mode})

    @app.route("/api/session", methods=["GET"])
    def get_session():
        return jsonify({
            "session_state": order_manager.get_session_state(),
            "session_mode": order_manager.get_session_mode(),
        })

    @app.route("/api/session", methods=["POST"])
    def set_session():
        try:
            data = request.json
            mode = data.get("mode", "").strip()
            order_manager.set_session_mode(mode)
            return jsonify({"success": True, "session_mode": mode})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/webhook", methods=["POST"])
    def webhook():
        try:
            payload = request.json
            trade = signal_adapter.process_webhook_signal(payload)
            return jsonify({"success": True, "trade_id": trade["trade_id"]})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    return app
