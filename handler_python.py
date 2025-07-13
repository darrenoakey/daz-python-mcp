# handler_python.py – AST handler that tags tests, functions, classes

import ast
from pathlib import Path
from typing import List

from handler_base import BaseHandler, GenericHandler, Thing


# line→absolute-char helpers
def _line_offsets(text: str) -> List[int]:
    offs = [0]
    for ln in text.splitlines(keepends=True):
        offs.append(offs[-1] + len(ln))
    return offs


def _abs(line: int, col: int, offs: List[int]) -> int:
    return offs[line - 1] + col


class PythonHandler(BaseHandler):
    @classmethod
    def parse(cls, fpath: Path, root: Path):
        h = cls(fpath, root)
        text = h.text
        offs = _line_offsets(text)

        h.structure.span = (0, len(text))
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # invalid Python → fallback
            return GenericHandler.parse(fpath, root)

        def add_thing(parent: Thing, name: str, node: ast.AST):
            parent.children[name] = Thing(
                name,
                (
                    _abs(node.lineno, node.col_offset, offs),
                    _abs(node.end_lineno, node.end_col_offset, offs),
                ),
            )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_thing(h.structure, node.name, node)
            elif isinstance(node, ast.ClassDef):
                cls_thing = Thing(
                    node.name,
                    (
                        _abs(node.lineno, node.col_offset, offs),
                        _abs(node.end_lineno, node.end_col_offset, offs),
                    ),
                )
                # Test class detection (inherits TestCase or name starts with Test)
                is_test_cls = node.name.startswith("Test") or any(
                    getattr(b, "id", "") == "TestCase" for b in node.bases
                )
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add_thing(cls_thing, sub.name, sub)
                        if is_test_cls and sub.name.startswith("test"):
                            cls_thing.children[sub.name].is_test = True  # mark
                h.structure.children[node.name] = cls_thing
        # mark top-level test_ functions
        for child in h.structure.children.values():
            if child.name.startswith("test_"):
                child.is_test = True
        return h

    # append new code at EOF
    def add(self, parent: str, name: str, content: str):
        if not content.endswith("\n"):
            content += "\n"
        self._write_text(self.text + "\n" + content)
