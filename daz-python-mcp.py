#!/usr/bin/env python3
# daz-python-mcp.py
#
# THIS MCP EXPOSES *ONLY* dazbuild_* TOOLS — DO NOT STRIP THE PREFIX.
#
# ─────────────────────────────────────────────────────────────────────────────
#  CRITICAL WORKFLOW SUMMARY  (also returned via dazbuild_guidelines)
# ─────────────────────────────────────────────────────────────────────────────
# 1.  dazbuild_start_change   →   repository must be clean.           (relaxed)
# 2.  dazbuild_write / dazbuild_add / … (use the smallest Thing ref).
# 3.  dazbuild_end_change
#     • pylint (score must be 10/10, no errors or warnings)
#     • python -m unittest discover   (all tests must pass)
#     • every .py file must contain at least one unittest.TestCase test.
#     • no file may invoke unittest.main()
#     • all code files must be smaller than 8192 bytes
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
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

    # ------------------------------------------------ instructions handling
    def _get_instructions_path(self, repo: str) -> Path:
        """Get the path to the instructions file for a repository."""
        repo_root = self.repos[repo]
        return repo_root / ".dazbuild" / "instructions.txt"

    def _get_default_instructions(self) -> str:
        """Return default instructions that explain dazbuild rules and best practices."""
        return textwrap.dedent(
            """
            # Dazbuild Instructions

            ## End Change Validation Rules
            When you call `dazbuild_end_change`, the following checks must pass:

            1. **Pylint Score**: Must be 10/10 with no errors or warnings
            2. **Unit Tests**: All tests must pass via `python -m unittest discover`
            3. **Test Coverage**: Every .py file must contain at least one unittest.TestCase test
            4. **No unittest.main()**: Files cannot invoke unittest.main() directly
            5. **File Size Limit**: All code files must be smaller than 8192 bytes

            ## Best Practices for Using Dazbuild

            ### Work with Small References
            - Always edit at the **smallest hierarchy node** possible
            - Instead of rewriting entire files, target specific functions, methods, or classes
            - Use `dazbuild_outline` to see the structure and find the right reference
            - Example: Edit `myfile.py::MyClass::my_method` instead of `myfile.py`

            ### Workflow Pattern
            1. `dazbuild_start_change` - Begin your change session
            2. Use `dazbuild_get` to examine current code
            3. Use `dazbuild_write` or `dazbuild_add` for targeted changes
            4. `dazbuild_end_change` - Validate and commit

            ### File Size Management
            - Keep code files under 8192 bytes
            - If a file grows too large, refactor into multiple smaller files
            - Split large classes into smaller, focused classes
            - Extract utility functions into separate modules

            ### Testing Strategy
            - Add real unit tests to every Python file (no mocks unless necessary)
            - Test both happy path and edge cases
            - Keep test methods focused and well-named
            - Place tests in the same file or dedicated test files

            ### Code Quality
            - Write clean, readable code that passes pylint
            - Use meaningful variable and function names
            - Add docstrings for public methods and classes
            - Keep functions focused on a single responsibility
        """
        ).strip()

    def _get_repo_instructions(self, repo: str) -> str:
        """Get instructions for a repository, using defaults if none exist."""
        instructions_path = self._get_instructions_path(repo)
        if instructions_path.exists():
            return instructions_path.read_text().strip()
        return self._get_default_instructions()

    def _update_repo_instructions(self, repo: str, instructions: str):
        """Update instructions for a repository."""
        instructions_path = self._get_instructions_path(repo)
        instructions_path.parent.mkdir(parents=True, exist_ok=True)
        instructions_path.write_text(instructions)

    # ------------------------------------------------ list_repositories
    # Return configured repositories (lazy‑loads on first use).
    def _list_repositories(self):
        if not self.repos:
            self._load_config()
        return {"repositories": {name: str(path) for name, path in self.repos.items()}}

    # ------------------------------------------------ git helpers
    def _git(self, root: Path, *args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    def _git_list_files(self, root: Path):
        try:
            # Get all tracked and untracked files from git
            all_files = self._git(
                root, "ls-files", "--others", "--cached", "--exclude-standard"
            ).splitlines()

            # Filter out files that don't actually exist on disk
            existing_files = [rel for rel in all_files if (root / rel).exists()]

            return existing_files
        except Exception:
            # Fallback: scan filesystem directly
            return [
                str(p.relative_to(root))
                for p in root.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            ]

    def _git_check_status(self, root: Path):
        """Check if there are any modified, staged, or untracked files."""
        try:
            status_output = self._git(root, "status", "--porcelain")
            if not status_output:
                return {"clean": True, "files": []}

            changed_files = []
            for line in status_output.splitlines():
                if len(line) >= 3:
                    status = line[:2]
                    filename = line[3:]

                    # Decode status codes
                    index_status = status[0]
                    worktree_status = status[1]

                    file_info = {"file": filename, "status": []}

                    if index_status == "M":
                        file_info["status"].append("staged modified")
                    elif index_status == "A":
                        file_info["status"].append("staged added")
                    elif index_status == "D":
                        file_info["status"].append("staged deleted")
                    elif index_status == "R":
                        file_info["status"].append("staged renamed")
                    elif index_status == "C":
                        file_info["status"].append("staged copied")

                    if worktree_status == "M":
                        file_info["status"].append("modified")
                    elif worktree_status == "D":
                        file_info["status"].append("deleted")
                    elif worktree_status == "?":
                        file_info["status"].append("untracked")

                    changed_files.append(file_info)

            return {"clean": False, "files": changed_files}
        except Exception:
            # If git status fails, assume clean
            return {"clean": True, "files": []}

    # ------------------------------------------------ change session
    # Begin—or re‑enter—an active change session for the given repository.
    # • If already active, we simply join it.
    # • We do NOT block on a dirty working tree anymore.
    def _start_change(self, repo: str):
        if repo in self._active_changes:
            return {"success": True, "message": "Already in change session"}
        self._active_changes.add(repo)
        return {"success": True, "message": "Change session started"}

    # -----------------------------------------------------------------------
    #  Helpers to validate unit‑test presence
    # -----------------------------------------------------------------------
    def _check_tests_in_file(self, path: Path) -> List[str]:
        text = path.read_text()
        has_testcase_ref = bool(re.search(r"\bunittest\.TestCase\b", text, re.I))
        has_test_class = bool(
            re.search(r"class\s+\w*Test\w*\s*\([^)]*unittest\.TestCase", text, re.I)
        )
        has_test_func = bool(re.search(r"def\s+test_\w+\s*\(", text))
        if has_testcase_ref or has_test_class or has_test_func:
            return []
        return [
            "didn't find the word 'unittest.TestCase'",
            "didn't find any class inheriting from TestCase",
            "didn't find any function starting with 'test_'",
        ]

    # -----------------------------------------------------------------------
    #  Helpers used by _end_change
    # -----------------------------------------------------------------------
    def _check_pylint(self, root: Path):
        pylint_cmd = (
            ["pylint"] if shutil.which("pylint") else [sys.executable, "-m", "pylint"]
        )
        pylint_cmd += ["-f", "json", "--exit-zero", "."]

        diag: Dict[str, Any] = {
            "pylint_cmd": " ".join(pylint_cmd),
            "env_PATH": os.environ.get("PATH", ""),
        }

        try:
            pylint_json = subprocess.check_output(
                pylint_cmd, cwd=root, text=True, stderr=subprocess.STDOUT
            )
            diag["pylint"] = json.loads(pylint_json or "[]")
            issues = [i for i in diag["pylint"] if i["type"] in {"error", "warning"}]
            if issues:
                return False, diag, f"pylint reported {len(issues)} issue(s)"
            return True, diag, ""
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            diag["pylint_error"] = str(exc)
            return False, diag, "pylint invocation failed"

    def _check_unittest(self, root: Path):
        try:
            out = subprocess.check_output(
                [sys.executable, "-m", "unittest", "discover", "-v", "-p", "*.py"],
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
            )
            if "Ran 0 tests" in out:
                return False, {"unittest_output": out}, "No tests discovered"
            return True, {"unittest_output": out}, ""
        except subprocess.CalledProcessError as exc:
            return False, {"unittest_failures": exc.output}, "Unit tests failed"

    def _check_file_sizes(self, root: Path, rel_files: List[str]) -> Dict[str, int]:
        """Check that all code files are under the size limit (8192 bytes)."""
        MAX_FILE_SIZE = 8192
        oversized: Dict[str, int] = {}

        # Define code file extensions
        code_extensions = {
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".java",
            ".cpp",
            ".c",
            ".h",
            ".hpp",
            ".cs",
            ".go",
            ".rs",
            ".rb",
            ".php",
            ".swift",
            ".kt",
            ".scala",
            ".clj",
            ".hs",
            ".ml",
            ".fs",
            ".vb",
            ".sql",
        }

        for rel in rel_files:
            if any(rel.endswith(ext) for ext in code_extensions):
                file_size = (root / rel).stat().st_size
                if file_size > MAX_FILE_SIZE:
                    oversized[rel] = file_size

        return oversized

    def _files_with_missing_tests(
        self, root: Path, rel_files: List[str]
    ) -> Dict[str, List[str]]:
        untested: Dict[str, List[str]] = {}
        pattern_tc = re.compile(r"\bunittest\.TestCase\b", re.I)
        pattern_cls = re.compile(
            r"class\s+\w*Test\w*\s*\([^)]*unittest\.TestCase", re.I
        )
        pattern_fn = re.compile(r"def\s+test_\w+\s*\(")

        for rel in rel_files:
            if not rel.endswith(".py"):
                continue
            text = (root / rel).read_text()
            if not (
                pattern_tc.search(text)
                or pattern_cls.search(text)
                or pattern_fn.search(text)
            ):
                untested[rel] = [
                    "didn't find the word 'unittest.TestCase'",
                    "didn't find any class inheriting from unittest.TestCase",
                    "didn't find any function starting with 'test_'",
                ]
        return untested

    def _files_with_unittest_main(
        self, root: Path, rel_files: List[str]
    ) -> Dict[str, str]:
        illegal: Dict[str, str] = {}
        for rel in rel_files:
            if rel.endswith(".py") and "unittest.main" in (root / rel).read_text():
                illegal[rel] = "contains unittest.main()"
        return illegal

    # -----------------------------------------------------------------------
    #  end_change (orchestrator)
    # -----------------------------------------------------------------------
    def _end_change(self, repo: str, message: str):
        root = self.repos[repo]
        rel_files = list(self.open_handlers[repo].keys())
        diagnostics: Dict[str, Any] = {}

        # 1. pylint
        ok, diag, msg = self._check_pylint(root)
        diagnostics.update(diag)
        if not ok:
            return {"success": False, "message": msg, "diagnostics": diagnostics}

        # 2. unittest
        ok, diag, msg = self._check_unittest(root)
        diagnostics.update(diag)
        if not ok:
            return {"success": False, "message": msg, "diagnostics": diagnostics}

        # 3. every file has tests
        missing = self._files_with_missing_tests(root, rel_files)
        if missing:
            diagnostics["untested_files"] = missing
            return {
                "success": False,
                "message": f"{len(missing)} Python file(s) lack tests",
                "diagnostics": diagnostics,
            }

        # 4. forbid unittest.main()
        illegal = self._files_with_unittest_main(root, rel_files)
        if illegal:
            diagnostics["illegal_unittest_main"] = illegal
            return {
                "success": False,
                "message": "unittest.main() found in code",
                "diagnostics": diagnostics,
            }

        # 5. check file sizes
        oversized = self._check_file_sizes(root, rel_files)
        if oversized:
            diagnostics["oversized_files"] = oversized
            return {
                "success": False,
                "message": f"{len(oversized)} file(s) exceed 8192 byte limit",
                "diagnostics": diagnostics,
            }

        # 6. all good → commit
        subprocess.check_call(["git", "add", "--all"], cwd=root)
        subprocess.check_call(["git", "commit", "-m", message], cwd=root)
        self._active_changes.discard(repo)

        # Get current instructions
        instructions = self._get_repo_instructions(repo)

        # Prepare learning prompt
        learning_prompt = (
            f"Congratulations! Your action was a success and the change has been closed.\n\n"
            f"We have instructions for dealing with this repository:\n"
            f"{instructions}\n\n"
            f"If you have learned anything during this change that could be added to "
            f"the instructions to make further changes go smoother, please call "
            f"dazbuild_update_instructions with a complete replacement instructions list."
        )

        return {
            "success": True,
            "message": "Committed",
            "diagnostics": diagnostics,
            "learning_prompt": learning_prompt,
        }

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

        # Check for any uncommitted changes
        status_info = self._git_check_status(root)

        files = self._git_list_files(root)
        self.open_handlers[name] = {rel: get_handler_for(root / rel) for rel in files}
        self.indexer.index_repository(
            name, root, {k: h.structure for k, h in self.open_handlers[name].items()}
        )

        # Get and return instructions
        instructions = self._get_repo_instructions(name)

        result = {"opened": True, "files": files, "instructions": instructions}

        # If there are uncommitted changes, include them in the response
        if not status_info["clean"]:
            result["change_in_progress"] = True
            result["changed_files"] = status_info["files"]

            # Create a human-readable summary
            file_summaries = []
            for file_info in status_info["files"]:
                status_text = ", ".join(file_info["status"])
                file_summaries.append(f"{file_info['file']} ({status_text})")

            result["change_summary"] = (
                f"Repository has uncommitted changes in {len(status_info['files'])} file(s): "
                + "; ".join(file_summaries)
            )

        return result

    def _close(self, name):
        self.open_handlers.pop(name, None)
        self.indexer.drop_repo(name)
        self._active_changes.discard(name)
        return {"closed": True}

    # ------------------------------------------------ tool registration
    def _register_handlers(self):
        def schema(**props):
            return {"type": "object", "properties": props, "required": list(props)}

        guidelines_text = textwrap.dedent(
            """
            ### dazbuild Guidelines

            • **Always** call `dazbuild_start_change` before any write/add.
            • Edit at the *smallest hierarchy node* you can.
            • Add a `unittest` block (real tests, no mocks) to **every** Python file.
            • Never invoke `unittest.main()`; `dazbuild_end_change` runs tests.
            • Keep all code files under 8192 bytes; refactor if larger.

            `dazbuild_end_change` runs pylint + tests + file size checks. If failures occur it returns
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
                "Begin (or join) change session.",
            ),
            "end_change": (
                schema(name={"type": "string"}, message={"type": "string"}),
                "Commit edits (pylint + tests + file sizes). Returns success boolean.",
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
            "update_instructions": (
                schema(
                    name={"type": "string"},
                    instructions={"type": "string"},
                ),
                "Update repository-specific instructions for future changes.",
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
                elif cmd == "update_instructions":
                    self._update_repo_instructions(args["name"], args["instructions"])
                    res = {"updated": True, "instructions": args["instructions"]}
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
