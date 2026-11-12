import os
import threading
import asyncio
from flask import Flask

app = Flask(__name__)


@app.route("/")
@app.route("/health")
def health():
    return "Bot is running", 200


def run_bot():
    from bot import main
    asyncio.run(main())


if __name__ == "__main__":
    # Бот в фоновом потоке
    threading.Thread(target=run_bot, daemon=True).start()

    # Flask слушает порт Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
