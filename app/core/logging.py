from __future__ import annotations

import logging
from logging.config import dictConfig

from app.core.config import get_settings


LOGGER_PREFIX = "memomate"


def setup_logging() -> None:
    settings = get_settings()
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
            },
            "handlers": {
                "default": {"class": "logging.StreamHandler", "formatter": "default"}
            },
            "loggers": {
                LOGGER_PREFIX: {
                    "handlers": ["default"],
                    "level": settings.log_level,
                    "propagate": False,
                },
                "uvicorn.error": {
                    "handlers": ["default"],
                    "level": settings.log_level,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["default"],
                    "level": settings.log_level,
                    "propagate": False,
                },
                "httpx": {
                    "handlers": ["default"],
                    "level": "WARNING",
                    "propagate": False,
                },
            },
            "root": {"handlers": ["default"], "level": settings.log_level},
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_PREFIX}.{name}")
