"""Entrypoint:  python -m app

Starts the web dashboard and (if a token is configured) the Telegram poller.
"""
import threading

from . import config, db, pipeline, web


def _requeue_stuck():
    ids = pipeline.requeue_stuck()
    if ids:
        print(f"  [requeue] re-ran {len(ids)} item(s) left PROCESSING by a previous run")


def main():
    print("Chat Data Extractor — local web app")
    db.init()
    print(f"  [db] {config.DB_PATH}")

    # heal anything a previous crash left mid-processing (runs in the background)
    threading.Thread(target=_requeue_stuck, daemon=True).start()

    if config.TELEGRAM_BOT_TOKEN:
        from . import telegram
        telegram.start_poller()
    else:
        print("  [telegram] no TELEGRAM_BOT_TOKEN — Telegram disabled (web + manual add still work)")

    print(f"  [extract] {'Gemini ' + config.GEMINI_MODEL if config.USE_GEMINI else 'offline stub (no GEMINI_API_KEY)'}")
    web.serve()


if __name__ == "__main__":
    main()
