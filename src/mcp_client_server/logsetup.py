import logging
import re
from typing import override

_KEY_PARAM = re.compile(r"([?&]key=)[^&\s\"']+")


def redact[T](value: T) -> T | str:
    return _KEY_PARAM.sub(r"\1[redacted]", value) if isinstance(value, str) else value


class RedactKeyParam(logging.Filter):
    """Removes the `?key=` credential from a log record, for example in the uvicorn access log."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(arg) for arg in record.args)
        return True


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(level.upper())
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, RedactKeyParam) for item in logger.filters):
            logger.addFilter(RedactKeyParam())
    # httpx2 logs each request URL at INFO, and an APNs URL contains the device token.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
