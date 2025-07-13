# handler_html.py – char-offset HTML id mapper

from html.parser import HTMLParser
from pathlib import Path

from handler_base import BaseHandler, Thing


class _PosParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = {}  # id -> absolute char offset

    def handle_starttag(self, _tag, attrs):
        for k, v in attrs:
            if k.lower() == "id":
                # position of '<' is current offset
                self.ids[v] = self.getpos()[1] + self.get_offset()

    # patch: expose private offset
    def get_offset(self):
        return self.rawdata[: self.getpos()[1]].rfind("<")


class HTMLHandler(BaseHandler):
    @classmethod
    def parse(cls, fpath: Path, root: Path):
        h = cls(fpath, root)
        text = h.text
        parser = _PosParser()
        parser.feed(text)
        h.structure.span = (0, len(text))
        for id_, start in parser.ids.items():
            # naive end = next '<' or EOF
            nxt = text.find("<", start + 1)
            end = nxt if nxt != -1 else len(text)
            h.structure.children[id_] = Thing(id_, (start, end))
        return h

    def add(self, parent: str, name: str, content: str):
        raise Exception("Adding new ids not supported yet.")
