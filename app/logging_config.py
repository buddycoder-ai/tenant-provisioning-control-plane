import logging
import logging.config
import os
import sys


def setup_logging(component: str = "api") -> None:
    """Configure structured JSON logging for Docker containers."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
            },
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "json" if os.getenv("DOCKER") else "standard",
                "level": log_level,
            },
        },
        "root": {
            "level": log_level,
            "handlers": ["console"],
        },
        "loggers": {
            "uvicorn": {"level": "INFO", "propagate": True},
            "uvicorn.access": {"level": "INFO", "propagate": True},
            "pika": {"level": "WARNING", "propagate": True},
            "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
        },
    }

    logging.config.dictConfig(logging_config)
    logger = logging.getLogger(component)
    logger.info(f"Starting {component} component", extra={"component": component})
