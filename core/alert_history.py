import json
import os


class AlertHistory:

    def __init__(self, file="data/alerts.json"):
        self.file = file

        if os.path.exists(file):
            with open(file, "r") as f:
                self.history = json.load(f)
        else:
            self.history = {}

    def get(self, key):
        return self.history.get(key)

    def set(self, key, status):
        self.history[key] = status

        with open(self.file, "w") as f:
            json.dump(self.history, f, indent=4)