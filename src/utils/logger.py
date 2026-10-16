"""Logging setup for Web Crawler Pro."""

import logging
import os
import sys

from src.config import get_config

_CONFIGURED_LOGGERS = set()


def setup_logger(name: str = "web_crawler_pro") -> logging.Logger:
    """
    Create (or retrieve) a configured logger.

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        logging.Logger: Configured logger instance.
    """
    config = get_config()
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called multiple times for same name
    if name in _CONFIGURED_LOGGERS:
        return logger

    logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # An empty LOG_FILE means "console only": no file handler, and no logs/
    # directory created on hosts that log to stdout, e.g. containers. When it
    # *is* set, the directory has to exist first — FileHandler does not create
    # it, and a missing one breaks the import of every module that calls
    # setup_logger() at module scope.
    if config.LOG_FILE:
        log_dir = os.path.dirname(config.LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(config.LOG_FILE)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    _CONFIGURED_LOGGERS.add(name)
    return logger


# --------------------------------------------------------------------------- #
# A second, older copy of this module used to be concatenated here. It
# shadowed the implementation above (so editing the top half had no effect),
# re-added a handler on every call, and crashed at import time whenever
# LOG_FILE was empty. Removed rather than kept, since nothing references it.
# --------------------------------------------------------------------------- #
