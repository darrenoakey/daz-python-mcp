# handler_js.py – robust Tree-sitter handler for JavaScript / TypeScript

from pathlib import Path

from tree_sitter import Parser

# ➊ Import helper from the *new* package name
from tree_sitter_language_pack import get_language

from handler_base import BaseHandler, Thing

# --------------------------------------------------------------------------- #
#  Initialise parser once per process
# --------------------------------------------------------------------------- #
_JS_LANG = get_language("javascript")
_PARSER: Parser | None = None


def _parser() -> Parser:
    global _PARSER
    if _PARSER is None:
        _PARSER = Parser()
        _PARSER.set_language(_JS_LANG)
    return _PARSER


# --------------------------------------------------------------------------- #
#  DFS walk
# --------------------------------------------------------------------------- #
def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


# --------------------------------------------------------------------------- #
#  Concrete handler
# --------------------------------------------------------------------------- #
class JSHandler(BaseHandler):
    @classmethod
    def parse(cls, fpath: Path, root: Path):
        h = cls(fpath, root)
        text_bytes = h.text.encode("utf8")
        tree = _parser().parse(text_bytes)
        root_node = tree.root_node

        h.structure.span = (0, len(text_bytes))

        def add_thing(parent: Thing, name: str, n):
            parent.children[name] = Thing(name, (n.start_byte, n.end_byte))

        for node in _walk(root_node):
            if node.type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = text_bytes[name_node.start_byte : name_node.end_byte].decode(
                        "utf8"
                    )
                    add_thing(h.structure, name, node)

            elif node.type == "class_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    cls_name = text_bytes[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf8")
                    cls_thing = Thing(cls_name, (node.start_byte, node.end_byte))
                    body = node.child_by_field_name("body")
                    for m in body.children if body else []:
                        if m.type == "method_definition":
                            id_node = m.child_by_field_name("name")
                            if id_node:
                                m_name = text_bytes[
                                    id_node.start_byte : id_node.end_byte
                                ].decode("utf8")
                                cls_thing.children[m_name] = Thing(
                                    m_name, (m.start_byte, m.end_byte)
                                )
                    h.structure.children[cls_name] = cls_thing

        return h

    # append new top-level construct at end-of-file
    def add(self, parent: str, name: str, content: str):
        if not content.endswith("\n"):
            content += "\n"
        self._write_text(self.text + "\n" + content)
