#!/usr/bin/env python3
# daz-python-mcp.py
#
# THIS MCP EXPOSES *ONLY* dazbuild_* TOOLS — DO NOT STRIP THE PREFIX.
#
# ─────────────────────────────────────────────────────────────────────────────
#  CRITICAL WORKFLOW SUMMARY  (also returned via dazbuild_guidelines)
# ─────────────────────────────────────────────────────────────────────────────
# 1.  dazbuild_start_change   →   repository must be clean.
# 2.  dazbuild_write / dazbuild_add / … (use the smallest Thing reference).
# 3.  dazbuild_end_change
#     • pylint (score must be 10/10, no errors or warnings)
#     • python -m unittest discover   (all tests must pass)
#     • every .py file must contain at least one unittest.TestCase test.
#     • no file may invoke unittest.main()
#     If ANY rule fails,  dazbuild_end_change returns:
#         {"success": false, "message": "<human summary>", "diagnostics": {...}}
#     The change session remains open; fix issues and call dazbuild_end_change
#     again.  On success it returns {"success": true, "message": "Committed"}.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import subprocess
import textwrap
import traceback
from pathlib import Path
from typing import Any, Dict, List, Set

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from indexer import CodeIndexer
from handler_base import error, get_handler_for, Thing


# --------------------------------------------------------------------------- #
#  MCP server
# --------------------------------------------------------------------------- #
class PyProjectMCPServer:
    def __init__(self):
        self.server = Server("daz-python-code-navigator")
        self.repos: Dict[str, Path] = {}
        self.open_handlers: Dict[str, Dict[str, Any]] = {}
        self._active_changes: Set[str] = set()
        self.indexer = CodeIndexer()
        self._register_handlers()

    # ------------------------------------------------ configuration
    def _load_config(self):
        cfg = Path(__file__).with_name("config.json")
        self.repos = {
            n: Path(p).expanduser().resolve()
            for n, p in json.loads(cfg.read_text()).get("repositories", {}).items()
            if Path(p).expanduser().exists()
        }

    # ------------------------------------------------ git helpers
    def _git(self, root: Path, *args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    def _git_list_files(self, root: Path):
        try:
            return self._git(
                root, "ls-files", "--others", "--cached", "--exclude-standard"
            ).splitlines()
        except Exception:
            return [
                str(p.relative_to(root))
                for p in root.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            ]

    # ------------------------------------------------ change session
    def _start_change(self, repo: str):
        root = self.repos[repo]
        if self._git(root, "status", "--porcelain"):
            raise Exception("Working tree has uncommitted changes")
        self._active_changes.add(repo)
        return {"success": True, "message": "Change session started"}

    # ------------------------------------------------------------ end_change
    def _end_change(self, repo: str, message: str):
        """
        Complete a change session.

        • pylint must report 0 errors/warnings.
        • python -m unittest discover -v -p '*.py' must run ≥1 tests and all pass.
        • Every .py file must contain at least one real TestCase; no mocks allowed.
        • No file may invoke unittest.main().

        Returns
        -------
        dict
            {
              "success": bool,          # True on commit, False on failure
              "message": str,           # Human summary
              "diagnostics": { ... }    # Detailed linter/test output on failure
            }
        """
        if repo not in self._active_changes:
            raise Exception("No change started")

        root = self.repos[repo]
        diag: Dict[str, Any] = {}

        # ---------- pylint --------------------------------------------------
        try:
            pylint_json = subprocess.check_output(
                ["pylint", "-f", "json", "--exit-zero", "."],
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
            )
            diag["pylint"] = json.loads(pylint_json or "[]")
            if any(item["type"] in {"error", "warning"} for item in diag["pylint"]):
                return {
                    "success": False,
                    "message": "pylint reported issues",
                    "diagnostics": diag,
                }
        except FileNotFoundError:
            diag["pylint_error"] = "pylint not installed"
            return {
                "success": False,
                "message": "pylint missing",
                "diagnostics": diag,
            }

        # ---------- unittest ------------------------------------------------
        try:
            test_out = subprocess.check_output(
                ["python", "-m", "unittest", "discover", "-v", "-p", "*.py"],
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
            )
            diag["unittest"] = test_out
            if "Ran 0 tests" in test_out:
                return {
                    "success": False,
                    "message": "No tests discovered (pattern '*.py')",
                    "diagnostics": diag,
                }
        except subprocess.CalledProcessError as exc:
            diag["unittest_failures"] = exc.output
            return {
                "success": False,
                "message": "Unit tests failed",
                "diagnostics": diag,
            }

        # ---------- ensure each .py has tests ------------------------------
        untested = [
            rel
            for rel, h in self.open_handlers[repo].items()
            if rel.endswith(".py")
            and not any(
                c.name.startswith("test") or getattr(c, "is_test", False)
                for c in h.structure.children.values()
            )
        ]
        if untested:
            diag["untested_files"] = untested
            return {
                "success": False,
                "message": "Some Python files lack tests",
                "diagnostics": diag,
            }

        # ---------- forbid unittest.main() ---------------------------------
        illegal = [
            rel
            for rel, h in self.open_handlers[repo].items()
            if rel.endswith(".py") and "unittest.main" in h.text
        ]
        if illegal:
            diag["illegal_unittest_main"] = illegal
            return {
                "success": False,
                "message": "unittest.main() found in code",
                "diagnostics": diag,
            }

        # ---------- all good → commit -------------------------------------
        subprocess.check_call(["git", "add", "--all"], cwd=root)
        subprocess.check_call(["git", "commit", "-m", message], cwd=root)
        self._active_changes.remove(repo)
        return {"success": True, "message": "Committed", "diagnostics": diag}

    # ------------------------------------------------ outline
    def _outline(self, repo: str, ref: str):
        def to_dict(t: Thing):
            d = {"name": t.name, "span": t.span, "children": []}
            for c in t.children.values():
                d["children"].append(to_dict(c))
            return d

        h = self.open_handlers[repo][ref.split("::")[0]]
        return {"outline": to_dict(h.structure)}

    # ------------------------------------------------ thin wrappers
    def _get(self, repo, ref):  # noqa: ANN001
        return {"content": self.open_handlers[repo][ref.split("::")[0]].get(ref)}

    def _write(self, repo, ref, content):  # noqa: ANN001
        if repo not in self._active_changes:
            raise Exception("Must call dazbuild_start_change first")
        h = self.open_handlers[repo][ref.split("::")[0]]
        h.write(ref, content)
        self.indexer.update_file(repo, self.repos[repo], ref.split("::")[0])
        return {"written": True}

    def _add(self, repo, obj_type, parent, name, content):  # noqa: ANN001
        if repo not in self._active_changes:
            raise Exception("Must call dazbuild_start_change first")
        root = self.repos[repo]
        if obj_type == "file":
            rel = str(Path(parent) / name) if parent else name
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            self.open_handlers[repo][rel] = get_handler_for(path)
            self.indexer.update_file(repo, root, rel)
            return {"added_file": rel}
        handler = self.open_handlers[repo][parent.split("::")[0]]
        handler.add(parent, name, content)
        self.indexer.update_file(repo, root, parent.split("::")[0])
        return {"added": name}

    def _search(self, repo, query, limit):  # noqa: ANN001
        return {"matches": self.indexer.search(repo, query, limit)}

    # ------------------------------------------------ repo open/close
    def _open(self, name):
        root = self.repos[name]
        files = self._git_list_files(root)
        self.open_handlers[name] = {rel: get_handler_for(root / rel) for rel in files}
        self.indexer.index_repository(
            name, root, {k: h.structure for k, h in self.open_handlers[name].items()}
        )
        return {"opened": True, "files": files}

    def _close(self, name):
        self.open_handlers.pop(name, None)
        self.indexer.drop_repo(name)
        self._active_changes.discard(name)
        return {"closed": True}

    # ------------------------------------------------ tool registration
    def _register_handlers(self):
        def schema(**props):
            return {"type": "object", "properties": props, "required": list(props)}

        # master documentation returned as a pseudo-tool
        guidelines_text = textwrap.dedent(
            """
            ### dazbuild Guidelines

            • **Always** call `dazbuild_start_change` before any write/add.
            • Edit at the *smallest hierarchy node* you can.
            • Add a `unittest` block (real tests, no mocks) to **every** Python file.
            • Never invoke `unittest.main()`; `dazbuild_end_change` runs tests.

            `dazbuild_end_change` runs pylint + tests. If failures occur it returns
            `{ "success": false, "message": "...", "diagnostics": {...} }`.
            Fix issues and call `dazbuild_end_change` again.
            """
        ).strip()

        tools: Dict[str, tuple] = {
            "guidelines": (schema(), guidelines_text),
            "list_repositories": (schema(), "List configured repositories."),
            "open_repository": (
                schema(name={"type": "string"}),
                "Open repo and index git-tracked files.",
            ),
            "close_repository": (
                schema(name={"type": "string"}),
                "Close repo and free memory.",
            ),
            "start_change": (
                schema(name={"type": "string"}),
                "Begin change session (must precede edits).",
            ),
            "end_change": (
                schema(name={"type": "string"}, message={"type": "string"}),
                "Commit edits (pylint + tests). Returns success boolean.",
            ),
            "outline": (
                schema(name={"type": "string"}, reference={"type": "string"}),
                "Return hierarchy of a file.",
            ),
            "get": (
                schema(name={"type": "string"}, reference={"type": "string"}),
                "Get content at reference.",
            ),
            "write": (
                schema(
                    name={"type": "string"},
                    reference={"type": "string"},
                    content={"type": "string"},
                ),
                "Replace content at reference.",
            ),
            "add": (
                schema(
                    name={"type": "string"},
                    type={
                        "type": "string",
                        "enum": ["file", "class", "function", "method", "test"],
                    },
                    parent_reference={"type": "string"},
                    object_name={"type": "string"},
                    content={"type": "string"},
                ),
                "Add new thing or file.",
            ),
            "search": (
                schema(
                    name={"type": "string"},
                    query={"type": "string"},
                    limit={"type": "integer", "default": 10},
                ),
                "Vector search in repo.",
            ),
        }

        @self.server.list_tools()
        async def list_tools():
            return [
                types.Tool(
                    name=f"dazbuild_{n}",
                    description=desc,
                    inputSchema=sch,
                )
                for n, (sch, desc) in tools.items()
            ]

        @self.server.call_tool()
        async def call_tool(name: str, args: Any):
            try:
                cmd = name.removeprefix("dazbuild_")
                if cmd == "guidelines":
                    res = {"guidelines": guidelines_text}
                elif cmd == "list_repositories":
                    res = self._list_repositories()
                elif cmd == "open_repository":
                    res = self._open(args["name"])
                elif cmd == "close_repository":
                    res = self._close(args["name"])
                elif cmd == "start_change":
                    res = self._start_change(args["name"])
                elif cmd == "end_change":
                    res = self._end_change(args["name"], args["message"])
                elif cmd == "outline":
                    res = self._outline(args["name"], args["reference"])
                elif cmd == "get":
                    res = self._get(args["name"], args["reference"])
                elif cmd == "write":
                    res = self._write(args["name"], args["reference"], args["content"])
                elif cmd == "add":
                    res = self._add(
                        args["name"],
                        args["type"],
                        args["parent_reference"],
                        args["object_name"],
                        args["content"],
                    )
                elif cmd == "search":
                    res = self._search(
                        args["name"], args["query"], args.get("limit", 10)
                    )
                else:
                    res = {"error": f"Unknown tool {name}"}
                return [types.TextContent(type="text", text=json.dumps(res, indent=2))]
            except Exception as exc:
                return [
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": str(exc), "trace": traceback.format_exc()},
                            indent=2,
                        ),
                    )
                ]

    # ------------------------------------------------ run loop
    async def run(self):
        try:
            self._load_config()
        except Exception as exc:
            error(str(exc))
            return
        async with mcp.server.stdio.stdio_server() as (r, w):
            await self.server.run(
                r,
                w,
                InitializationOptions(
                    server_name="daz-python-code-navigator",
                    server_version="3.1.0",
                    capabilities=self.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )


async def main():
    await PyProjectMCPServer().run()


if __name__ == "__main__":
    asyncio.run(main())
