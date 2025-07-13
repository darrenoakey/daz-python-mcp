# handler_css.py – char-offset CSS rule finder

import re
from pathlib import Path
from handler_base import BaseHandler, Thing


_rule = re.compile(r"([^{]+)\{")  # naive: selector till first '{'


class CSSHandler(BaseHandler):
    @classmethod
    def parse(cls, fpath: Path, root: Path):
        h = cls(fpath, root)
        text = h.text
        matches = [(m.group(1).strip(), m.start()) for m in _rule.finditer(text)]
        h.structure.span = (0, len(text))
        for (name, start), nxt in zip(matches, matches[1:] + [(None, len(text))]):
            h.structure.children[name] = Thing(name, (start, nxt[1]))
        return h

    def add(self, parent: str, name: str, content: str):
        if not content.endswith("\n"):
            content += "\n"
        self._write_text(self.text + "\n" + content)
