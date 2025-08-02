#!/usr/bin/env python3
# daz-python-mcp.py
#
# THIS MCP EXPOSES *ONLY* dazbuild_* TOOLS — DO NOT STRIP THE PREFIX.
#
# ─────────────────────────────────────────────────────────────────────────────
#  CRITICAL WORKFLOW SUMMARY  (also returned via dazbuild_guidelines)
# ─────────────────────────────────────────────────────────────────────────────
# 1.  dazbuild_start_change   →   repository must be clean.           (relaxed)
# 2.  dazbuild_write / dazbuild_add / dazbuild_delete / … (use the smallest Thing ref).
# 3.  dazbuild_end_change  (runs verifications in parallel for speed)
#     • no mocks or stubs allowed in any files
#     • all code files must be smaller than 8192 bytes
#     • every .py file must contain at least one unittest.TestCase test
#     • tests must never access keyring (word 'keyring' cannot appear after TestCase)
#     • no file may invoke unittest.main()
#     • pylint (score must be 10/10, no errors or warnings)
#     • python -m unittest discover   (all tests must pass)
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Any, Dict, List, Set

# Prevent tokenizer deadlocks when forking subprocesses
os.environ["TOKENIZERS_PARALLELISM"] = "false"
# Prevent .pyc files during test runs
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from indexer import CodeIndexer
from handler_base import get_handler_for, Thing
from file_verifier import FileVerifier


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
        self.verifier = FileVerifier()
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

            1. **No Mocks or Stubs**: No file may contain the words "mock" or "stub" - all tests should test real functionality
            2. **File Size Limit**: All code files must be smaller than 8192 bytes
            3. **Unit Tests**: Every .py file must contain at least one unittest.TestCase test
            4. **No Keyring in Tests**: Tests should never access keyring - the word 'keyring' cannot appear after TestCase
            5. **No unittest.main()**: Files cannot invoke unittest.main() directly
            6. **Pylint Score**: Must be 10/10 with no errors or warnings

            You can use `dazbuild_verify` to test these rules on any file without making changes.

            ## Best Practices for Using Dazbuild

            ### Work with Small References
            - Always edit at the **smallest hierarchy node** possible
            - Instead of rewriting entire files, target specific functions, methods, or classes
            - Use `dazbuild_outline` to see the structure and find the right reference
            - Example: Edit `myfile.py::MyClass::my_method` instead of `myfile.py`

            ### Workflow Pattern
            1. `dazbuild_start_change` - Begin your change session
            2. Use `dazbuild_get` to examine current code
            3. Use `dazbuild_verify` to check files before editing (optional but recommended)
            4. Use `dazbuild_write`, `dazbuild_add`, or `dazbuild_delete` for targeted changes
            5. `dazbuild_end_change` - Validate and commit

            ### File Size Management
            - Keep code files under 8192 bytes
            - If a file grows too large, refactor into multiple smaller files
            - Split large classes into smaller, focused classes
            - Extract utility functions into separate modules

            ### Code Documentation Requirements
            - **File Headers**: Every file MUST have a comprehensive comment at the top explaining:
              - What the file is for
              - Its main purpose and functionality
              - How it relates to other files in the project
              - Any important dependencies or interactions
            - **Function Documentation**: Every function (private or public) MUST have a docstring that explains:
              - What the function does
              - Parameters and their types
              - Return value and type
              - Any exceptions that might be raised
              - Example usage when appropriate

            ### Testing Strategy
            - Add real unit tests to every Python file (no mocks or stubs allowed)
            - Tests must never access the system keyring
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
    def _list_repositories(self):
        if not self.repos:
            self._load_config()
        return {"repositories": {name: str(path) for name, path in self.repos.items()}}

    # ------------------------------------------------ git helpers
    def _git(self, root: Path, *args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    def _git_list_files(self, root: Path):
        try:
            all_files = self._git(
                root, "ls-files", "--others", "--cached", "--exclude-standard"
            ).splitlines()
            existing_files = [rel for rel in all_files if (root / rel).exists()]
            return existing_files
        except Exception:
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
            return {"clean": True, "files": []}

    # ------------------------------------------------ change session
    def _start_change(self, repo: str):
        if repo in self._active_changes:
            return {"success": True, "message": "Already in change session"}
        self._active_changes.add(repo)
        return {"success": True, "message": "Change session started"}

    # ------------------------------------------------ end_change
    def _end_change(self, repo: str, message: str):
        root = self.repos[repo]

        # Get all files to verify
        rel_files = list(self.open_handlers[repo].keys())

        # Run verification on the whole project
        verify_result = self.verifier.verify(root, all_files=rel_files)

        if not verify_result["success"]:
            return {
                "success": False,
                "message": verify_result["error"],
                "diagnostics": verify_result["diagnostics"],
            }

        # All good → commit
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
            "diagnostics": verify_result["diagnostics"],
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
    def _get(self, repo, ref):
        return {"content": self.open_handlers[repo][ref.split("::")[0]].get(ref)}

    def _write(self, repo, ref, content):
        if repo not in self._active_changes:
            raise Exception("Must call dazbuild_start_change first")

        root = self.repos[repo]
        rel_file = ref.split("::")[0]

        # Perform the write
        h = self.open_handlers[repo][rel_file]
        h.write(ref, content)
        self.indexer.update_file(repo, root, rel_file)

        # Verify the file after writing
        verify_result = self.verifier.verify(root, rel_file)

        if not verify_result["success"]:
            # Revert the write by reloading the handler
            self.open_handlers[repo][rel_file] = get_handler_for(root / rel_file)
            self.indexer.update_file(repo, root, rel_file)
            return {
                "success": False,
                "error": verify_result["error"],
                "diagnostics": verify_result["diagnostics"],
            }

        return {"success": True, "diagnostics": verify_result["diagnostics"]}

    def _delete(self, repo, reference):
        """Delete a file from the repository."""
        if repo not in self._active_changes:
            raise Exception("Must call dazbuild_start_change first")

        root = self.repos[repo]

        if "::" in reference:
            raise Exception(
                "Can only delete whole files, not parts of files. Use dazbuild_write to modify file contents."
            )

        rel_path = reference

        if rel_path not in self.open_handlers[repo]:
            raise Exception(f"File {rel_path} not found in repository")

        # Remove from filesystem
        file_path = root / rel_path
        if file_path.exists():
            file_path.unlink()

        # Remove from open handlers
        del self.open_handlers[repo][rel_path]

        return {"success": True, "deleted": rel_path}

    def _add(self, repo, obj_type, parent, name, content):
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

            # Verify the new file
            verify_result = self.verifier.verify(root, rel)

            if not verify_result["success"]:
                # Remove the file if verification fails
                path.unlink()
                del self.open_handlers[repo][rel]
                return {
                    "success": False,
                    "error": verify_result["error"],
                    "diagnostics": verify_result["diagnostics"],
                }

            return {
                "success": True,
                "added_file": rel,
                "diagnostics": verify_result["diagnostics"],
            }
        else:
            # Adding to existing file
            rel_file = parent.split("::")[0]
            handler = self.open_handlers[repo][rel_file]
            handler.add(parent, name, content)
            self.indexer.update_file(repo, root, rel_file)

            # Verify the modified file
            verify_result = self.verifier.verify(root, rel_file)

            if not verify_result["success"]:
                # Reload the handler to revert changes
                self.open_handlers[repo][rel_file] = get_handler_for(root / rel_file)
                self.indexer.update_file(repo, root, rel_file)
                return {
                    "success": False,
                    "error": verify_result["error"],
                    "diagnostics": verify_result["diagnostics"],
                }

            return {
                "success": True,
                "added": name,
                "diagnostics": verify_result["diagnostics"],
            }

    def _verify(self, repo: str, reference: str):
        """Verify a file without making changes."""
        root = self.repos[repo]
        rel_file = reference.split("::")[0]

        if rel_file not in self.open_handlers[repo]:
            return {
                "success": False,
                "error": f"File {rel_file} not found in repository",
            }

        # Run verification on the file
        verify_result = self.verifier.verify(root, rel_file)

        return verify_result

    def _search(self, repo, query, limit):
        return {"matches": self.indexer.search(repo, query, limit)}

    # ------------------------------------------------ repo open/close
    def _open(self, name):
        root = self.repos[name]
        status_info = self._git_check_status(root)
        files = self._git_list_files(root)
        self.open_handlers[name] = {rel: get_handler_for(root / rel) for rel in files}
        self.indexer.index_repository(
            name, root, {k: h.structure for k, h in self.open_handlers[name].items()}
        )

        instructions = self._get_repo_instructions(name)
        result = {"opened": True, "files": files, "instructions": instructions}

        if not status_info["clean"]:
            result["change_in_progress"] = True
            result["changed_files"] = status_info["files"]
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

            • **Always** call `dazbuild_start_change` before any write/add/delete.
            • Edit at the *smallest hierarchy node* you can.
            • No mocks or stubs allowed - all tests must test real functionality.
            • Tests must never access keyring - 'keyring' cannot appear after TestCase.
            • Add a `unittest` block (real tests, no mocks/stubs) to **every** Python file.
            • Never invoke `unittest.main()`; `dazbuild_end_change` runs tests.
            • Keep all code files under 8192 bytes; refactor if larger.
            • **Document everything**: File headers and function docstrings are required.
            • **Tests run automatically**: write/add operations verify files and fail if verification fails.
            • **Use dazbuild_verify** to check if a file passes all rules without making changes.

            `dazbuild_end_change` runs all verifications in parallel for speed. If failures occur it returns
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
                "Commit edits (runs full verification). Returns success boolean.",
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
                "Replace content at reference. Verifies file after write; fails if verification fails.",
            ),
            "delete": (
                schema(
                    name={"type": "string"},
                    reference={"type": "string"},
                ),
                "Delete a file from the repository.",
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
                "Add new thing or file. Verifies after add; fails if verification fails.",
            ),
            "verify": (
                schema(
                    name={"type": "string"},
                    reference={"type": "string"},
                ),
                "Verify a file without making changes. Runs all checks: mock/stub detection, file size, unit tests, keyring, unittest.main, and pylint.",
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

        @self.server.list_resources()
        async def list_resources():
            # Return empty list - this server doesn't provide resources
            return []

        @self.server.list_prompts()
        async def list_prompts():
            # Return empty list - this server doesn't provide prompts
            return []

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
                elif cmd == "delete":
                    res = self._delete(args["name"], args["reference"])
                elif cmd == "add":
                    res = self._add(
                        args["name"],
                        args["type"],
                        args["parent_reference"],
                        args["object_name"],
                        args["content"],
                    )
                elif cmd == "verify":
                    res = self._verify(args["name"], args["reference"])
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
            print(f"Error loading config: {str(exc)}", file=sys.stderr)
            return
        async with mcp.server.stdio.stdio_server() as (r, w):
            await self.server.run(
                r,
                w,
                InitializationOptions(
                    server_name="daz-python-code-navigator",
                    server_version="4.3.0",
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
