from bot.main import main

import asyncio
import logging
import time


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception:
            logging.exception("Бот упал, перезапуск через 5 секунд")
            time.sleep(5)
