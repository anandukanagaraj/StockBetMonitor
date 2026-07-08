from configparser import ConfigParser
from pathlib import Path


class Config:

    def __init__(self, config_file: str = "config.ini"):

        self.config_file = Path(config_file)

        if not self.config_file.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_file}"
            )

        self.parser = ConfigParser()
        self.parser.read(self.config_file)

        self.general = self.parser["GENERAL"]
        self.market = self.parser["MARKET"]
        self.email = self.parser["EMAIL"]
        self.whatsapp = self.parser["WHATSAPP"]
        self.alerts = self.parser["ALERTS"]

    @property
    def excel_file(self):
        return self.general.get("EXCEL_FILE")

    @property
    def market_start(self):
        return self.market.get("START_TIME")

    @property
    def market_end(self):
        return self.market.get("END_TIME")

    @property
    def report_time(self):
        return self.market.get("REPORT_TIME")

    @property
    def interval(self):
        return self.market.getint("CHECK_INTERVAL_MINUTES")