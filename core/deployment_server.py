"""Production entrypoint: one process owns SQLite; Uvicorn handles TERM/INT drain."""
import os
import uvicorn
from .deployment import Settings, configure_logging


def main():
    os.umask(0o077)
    settings = Settings.from_env()
    configure_logging()
    uvicorn.run('main:app', host=settings.host, port=settings.port, workers=1,
                reload=False, log_config=None, access_log=False,
                proxy_headers=False, timeout_graceful_shutdown=360,
                timeout_keep_alive=5, limit_concurrency=64)


if __name__ == '__main__':
    main()
