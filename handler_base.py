# handler_base.py – common helpers + generic handler (char-accurate)

import sys
from pathlib import Path
from typing import Dict, List, Tuple


# ----- helpers ---------------------------------------------------------------
def error(msg: str):
    print(msg, file=sys.stderr, flush=True)


def chunk_text(text: str, size: int = 1024, overlap: int = 512):
    for i in range(0, len(text), size - overlap):
        yield text[i : i + size], i


# ----- core node -------------------------------------------------------------
class Thing:
    def __init__(self, name: str, span: Tuple[int, int]):
        self.name = name  # arbitrary identifier
        self.span = span  # (start_char, end_char) **inclusive**
        self.children: Dict[str, "Thing"] = {}

    def to_dict(self):
        return {
            "name": self.name,
            "span": self.span,
            "children": {k: v.to_dict() for k, v in self.children.items()},
        }


# ----- abstract handler ------------------------------------------------------
class BaseHandler:
    # constructor
    def __init__(self, fpath: Path, repo_root: Path):
        self.file_path = fpath
        self.repo_root = repo_root
        self.text: str = fpath.read_text(encoding="utf-8", errors="ignore")
        self.structure: Thing = Thing(".", (0, len(self.text)))

    # low-level I/O
    def _write_text(self, new_text: str):
        self.file_path.write_text(new_text, encoding="utf-8")
        self.text = new_text  # keep cache in sync
        self._reparse()  # rebuild hierarchy

    # resolve reference → Thing
    def _resolve(self, ref: str) -> Thing:
        parts = ref.split("::")[1:]  # drop filename
        node = self.structure
        for p in parts:
            if p not in node.children:
                raise Exception(f"Unknown reference piece {p}")
            node = node.children[p]
        return node

    # public API: get/ write/ add
    def get(self, ref: str) -> str:
        t = self._resolve(ref)
        return self.text[t.span[0] : t.span[1]]

    def write(self, ref: str, content: str):
        t = self._resolve(ref)
        new = self.text[: t.span[0]] + content + self.text[t.span[1] :]
        self._write_text(new)

    def add(self, parent: str, name: str, content: str):
        raise NotImplementedError

    # subclasses must implement parse -----------------------------------------
    @classmethod
    def parse(cls, fpath: Path, root: Path) -> "BaseHandler":
        raise NotImplementedError

    # helper: rebuild self in-place after edits
    def _reparse(self):
        fresh = get_handler_for(self.file_path)  # factory gives new concrete type
        self.__dict__.update(fresh.__dict__)  # shallow copy state


# ----- generic fallback ------------------------------------------------------
class GenericHandler(BaseHandler):
    @classmethod
    def parse(cls, fpath: Path, root: Path):
        h = cls(fpath, root)
        h.structure.span = (0, len(h.text))
        return h

    def add(self, parent: str, name: str, content: str):
        raise Exception("Cannot add items to generic file.")


# ----- factory ---------------------------------------------------------------
# handler_base.py – factory excerpt only (rest unchanged)


def get_handler_for(fpath: Path) -> BaseHandler:
    from handler_python import PythonHandler
    from handler_js import JSHandler
    from handler_html import HTMLHandler
    from handler_css import CSSHandler

    ext = fpath.suffix.lower()
    try:
        if ext == ".py":
            return PythonHandler.parse(fpath, fpath.parent)
        if ext in {".js", ".mjs", ".cjs"}:
            return JSHandler.parse(fpath, fpath.parent)
        if ext in {".html", ".htm"}:
            return HTMLHandler.parse(fpath, fpath.parent)
        if ext == ".css":
            return CSSHandler.parse(fpath, fpath.parent)
    except Exception as exc:  # ← swallow parser failures
        error(f"Parser failed for {fpath}: {exc}")

    return GenericHandler.parse(fpath, fpath.parent)
