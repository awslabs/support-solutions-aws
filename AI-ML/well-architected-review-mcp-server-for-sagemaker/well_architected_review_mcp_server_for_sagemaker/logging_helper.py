# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Logging helper for the Well-Architected Review MCP Server for Amazon SageMaker."""

from enum import Enum
from loguru import logger
from mcp.server.fastmcp import Context
from typing import Any


class LogLevel(Enum):
    """Enum for log levels."""

    DEBUG = 'debug'
    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'
    CRITICAL = 'critical'


def log_with_request_id(ctx: Context, level: LogLevel, message: str, **kwargs: Any) -> None:
    """Log a message with the request ID from the context.

    Args:
        ctx: The MCP context containing the request ID
        level: The log level (from LogLevel enum)
        message: The message to log
        **kwargs: Additional fields to include in the log message
    """
    log_message = f'[request_id={ctx.request_id}] {message}'

    if level == LogLevel.DEBUG:
        logger.debug(log_message, **kwargs)
    elif level == LogLevel.INFO:
        logger.info(log_message, **kwargs)
    elif level == LogLevel.WARNING:
        logger.warning(log_message, **kwargs)
    elif level == LogLevel.ERROR:
        logger.error(log_message, **kwargs)
    elif level == LogLevel.CRITICAL:
        logger.critical(log_message, **kwargs)
