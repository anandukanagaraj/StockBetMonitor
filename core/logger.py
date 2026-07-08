import logging
import os
from datetime import datetime

LOG_FOLDER = "logs"

def setup_logger():

    if not os.path.exists(LOG_FOLDER):
        os.makedirs(LOG_FOLDER)

    logfile = os.path.join(
        LOG_FOLDER,
        datetime.now().strftime("%Y-%m-%d") + ".log"
    )

    logger = logging.getLogger("StockMonitor")

    if logger.hasHandlers():
        return logger

    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    # File Logging
    file_handler = logging.FileHandler(logfile, encoding="utf-8")
    file_handler.setFormatter(formatter)

    # Console Logging
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger