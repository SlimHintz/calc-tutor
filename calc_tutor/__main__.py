import logging

import uvicorn

from . import config, db, remarkable, scheduler
from .server import build_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not config.auth_token():
        raise SystemExit("CALC_TUTOR_TOKEN is not set -- refusing to start")
    counts = db.init_db()
    logging.getLogger("calc_tutor").info("curriculum synced: %s", counts)
    if config.scheduler_enabled():
        scheduler.start()
        remarkable.start()
    uvicorn.run(build_app(), host=config.host(), port=config.port())


if __name__ == "__main__":
    main()
