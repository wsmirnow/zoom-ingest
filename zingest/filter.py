import logging
import re


class RegexFilter:

    def __init__(self, config):
        self.logger = logging.getLogger(__name__)
        self.pattern = config["Filter"]["regex"]
        self.regex = re.compile(config["Filter"]["regex"])
        self.logger.debug(f"Filtering with regex {self.pattern} against recording topics")

    def matches(self, obj):
        return self.regex.search(obj['topic']) != None
