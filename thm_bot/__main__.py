import logging

from filelock import FileLock, Timeout

from .config import Config
from .db import Database
from .discord_app import AchievementBot


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Avoid debug transport output and response data. Token is never logged.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        config = Config.load(require_token=True)
        config.database.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(config.database) + ".lock", timeout=0):
            db = Database(config.database)
            try:
                bot = AchievementBot(config, db)
                bot.run(config.token, log_handler=None)
            finally:
                db.close()
    except Timeout:
        raise SystemExit("Another bot instance is using this database. Run only one instance.") from None
    except ValueError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
