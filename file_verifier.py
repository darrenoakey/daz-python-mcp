#!/usr/bin/env python3
"""
File verification module for dazbuild.

This module handles all file verification logic including:
- Mock/stub detection
- File size limits
- Unit test execution
- Keyring usage detection in tests
- unittest.main detection
- Pylint checks

The verify() method works in two modes:
1. Single file mode (when rel_path is provided):
   - Runs ALL checks on that one file (mock/stub, size, keyring in tests, tests, unittest.main, pylint)
   - Used after write/add operations for immediate feedback

2. Whole project mode (when rel_path is None, all_files provided):
   - Checks ALL files for basic issues (mock/stub, size, test presence, keyring, unittest.main) in parallel
   - Then runs project-wide pylint and unittest discover in parallel
   - Used by end_change to validate the entire project
   - Parallel execution speeds up verification significantly
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FutureTimeoutError,
)
from pathlib import Path
from typing import Dict, Any, List, Optional

# Prevent tokenizer deadlocks when forking
os.environ["TOKENIZERS_PARALLELISM"] = "false"
# Prevent .pyc files during test runs
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


class FileVerifier:
    """Handles all file verification operations."""

    MAX_FILE_SIZE = 8192

    # Define code file extensions that need size checking
    CODE_EXTENSIONS = {
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

    def __init__(self):
        """Initialize the verifier."""
        pass

    def verify(
        self,
        root: Path,
        rel_path: Optional[str] = None,
        all_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Verify either a single file or the whole project.

        Args:
            root: Project root directory
            rel_path: Relative path to a single file to verify (optional)
            all_files: List of all files in the project (needed for project-wide checks)

        Returns:
            dict with:
            - success: bool
            - error: str (if failed)
            - diagnostics: dict of detailed info
        """
        diagnostics = {}

        if rel_path:
            # Single file verification
            diagnostics["mode"] = "single_file"
            diagnostics["file"] = rel_path

            # Run all checks on the single file
            return self._verify_single_file(root, rel_path, diagnostics)
        else:
            # Whole project verification
            diagnostics["mode"] = "whole_project"

            if all_files:
                # Check all individual files for basic issues in parallel
                failed_file_check = None
                try:
                    with ThreadPoolExecutor(max_workers=8) as executor:
                        # Submit verification tasks for each file
                        future_to_file = {
                            executor.submit(
                                self._check_file_basic_issues, root, file
                            ): file
                            for file in all_files
                        }

                        # Collect results
                        for future in as_completed(
                            future_to_file, timeout=120
                        ):  # 2 minute timeout for file checks
                            file = future_to_file[future]
                            try:
                                result = future.result()
                                if not result["success"]:
                                    failed_file_check = (file, result["error"])
                                    # Cancel remaining futures
                                    for f in future_to_file:
                                        f.cancel()
                                    break
                            except Exception as exc:
                                failed_file_check = (
                                    file,
                                    f"Error during verification: {str(exc)}",
                                )
                                # Cancel remaining futures
                                for f in future_to_file:
                                    f.cancel()
                                break
                except FutureTimeoutError:
                    return {
                        "success": False,
                        "error": "File verification timed out after 2 minutes",
                        "diagnostics": diagnostics,
                    }

                if failed_file_check:
                    file, error = failed_file_check
                    diagnostics["failed_file"] = file
                    return {
                        "success": False,
                        "error": f"{file}: {error}",
                        "diagnostics": diagnostics,
                    }

            # Run project-wide checks
            return self._verify_whole_project(root, diagnostics)

    def _check_file_basic_issues(self, root: Path, file: str) -> Dict[str, Any]:
        """Check a single file for basic issues (mock/stub, size, test presence, keyring, unittest.main)."""
        if file.endswith(".py"):
            # Check for mock/stub
            file_path = root / file
            if file_path.exists():
                content = file_path.read_text()
                if re.search(r"\b(mock|stub)\b", content, re.IGNORECASE):
                    return {
                        "success": False,
                        "error": "Mock objects, stub objects, or mock/stub tests are never allowed",
                    }

                # Check for unittest.main
                if "unittest.main" in content:
                    return {
                        "success": False,
                        "error": "contains unittest.main() which is not allowed",
                    }

                # Check for test presence
                if not self._has_tests(content):
                    return {"success": False, "error": "No unit tests found in file"}

                # Check for keyring in tests
                keyring_check = self._check_keyring_in_tests(content)
                if not keyring_check["success"]:
                    return {"success": False, "error": keyring_check["error"]}

        # Check file size for all code files
        if self._is_code_file(file):
            file_path = root / file
            if file_path.exists():
                file_size = file_path.stat().st_size
                if file_size > self.MAX_FILE_SIZE:
                    return {
                        "success": False,
                        "error": f"exceeds {self.MAX_FILE_SIZE} byte limit ({self._format_file_size(file_size)})",
                    }

        return {"success": True}

    def _verify_single_file(
        self, root: Path, rel_path: str, diagnostics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Verify a single file with all checks."""
        file_path = root / rel_path

        # 1. Check for mock/stub (all files)
        if file_path.exists():
            content = file_path.read_text()
            if re.search(r"\b(mock|stub)\b", content, re.IGNORECASE):
                return {
                    "success": False,
                    "error": "Mock objects, stub objects, or mock/stub tests are never allowed - all tests should test real functionality",
                    "diagnostics": diagnostics,
                }

        # 2. Check file size (code files only)
        if self._is_code_file(rel_path):
            file_size = file_path.stat().st_size
            diagnostics["size_bytes"] = file_size
            if file_size > self.MAX_FILE_SIZE:
                return {
                    "success": False,
                    "error": f"File exceeds {self.MAX_FILE_SIZE} byte limit ({self._format_file_size(file_size)}). "
                    f"This file should be refactored to be smaller - especially see if any commonality "
                    f"can be pulled out and used by other files",
                    "diagnostics": diagnostics,
                }

        # Python-specific checks
        if rel_path.endswith(".py"):
            # Check for keyring usage in tests
            keyring_check = self._check_keyring_in_tests(content)
            if not keyring_check["success"]:
                return {
                    "success": False,
                    "error": keyring_check["error"],
                    "diagnostics": diagnostics,
                }

            # 3. Run unit tests
            test_result = self._run_file_tests(root, rel_path)
            diagnostics.update(test_result["diagnostics"])
            if not test_result["success"]:
                return {
                    "success": False,
                    "error": test_result["error"],
                    "diagnostics": diagnostics,
                }

            # 4. Check for unittest.main
            if "unittest.main" in content:
                return {
                    "success": False,
                    "error": "File contains unittest.main() which is not allowed",
                    "diagnostics": diagnostics,
                }

            # 5. Run pylint on single file
            pylint_result = self._run_pylint_file(root, rel_path)
            diagnostics.update(pylint_result["diagnostics"])
            if not pylint_result["success"]:
                return {
                    "success": False,
                    "error": pylint_result["error"],
                    "diagnostics": diagnostics,
                }

        return {"success": True, "diagnostics": diagnostics}

    def _verify_whole_project(
        self, root: Path, diagnostics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Run project-wide verification (unittest discover and pylint) in parallel."""

        diagnostics["parallel_execution"] = True
        start_time = time.time()

        try:
            # Run tests and pylint in parallel
            with ThreadPoolExecutor(max_workers=2) as executor:
                # Submit both tasks
                test_future = executor.submit(self._run_project_tests, root)
                pylint_future = executor.submit(self._run_pylint_project, root)

                # Track which tasks are running
                diagnostics["tasks_started"] = ["project_tests", "project_pylint"]

                # Collect results as they complete
                results = {}
                completed_tasks = []

                for future in as_completed(
                    [test_future, pylint_future], timeout=600
                ):  # 10 minute total timeout
                    if future == test_future:
                        results["tests"] = future.result()
                        completed_tasks.append("project_tests")
                        diagnostics["project_tests_time"] = time.time() - start_time
                    else:
                        results["pylint"] = future.result()
                        completed_tasks.append("project_pylint")
                        diagnostics["project_pylint_time"] = time.time() - start_time

                diagnostics["tasks_completed"] = completed_tasks

        except FutureTimeoutError:
            # Figure out which task(s) timed out
            timed_out_tasks = []
            if "tests" not in results:
                timed_out_tasks.append("project_tests")
            if "pylint" not in results:
                timed_out_tasks.append("project_pylint")

            diagnostics["timed_out_tasks"] = timed_out_tasks
            return {
                "success": False,
                "error": f"Verification timed out after 10 minutes. Tasks that didn't complete: {', '.join(timed_out_tasks)}",
                "diagnostics": diagnostics,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Unexpected error during parallel verification: {str(exc)}",
                "diagnostics": {**diagnostics, "exception": traceback.format_exc()},
            }

        # Check test results
        if "tests" in results:
            diagnostics.update(results["tests"]["diagnostics"])
            if not results["tests"]["success"]:
                return {
                    "success": False,
                    "error": results["tests"]["error"],
                    "diagnostics": diagnostics,
                }

        # Check pylint results
        if "pylint" in results:
            diagnostics.update(results["pylint"]["diagnostics"])
            if not results["pylint"]["success"]:
                return {
                    "success": False,
                    "error": results["pylint"]["error"],
                    "diagnostics": diagnostics,
                }

        diagnostics["total_verification_time"] = time.time() - start_time
        return {"success": True, "diagnostics": diagnostics}

    def _is_code_file(self, rel_path: str) -> bool:
        """Check if a file is a code file based on extension."""
        return any(rel_path.endswith(ext) for ext in self.CODE_EXTENSIONS)

    def _format_file_size(self, size_bytes: int) -> str:
        """Format file size in a human-readable way."""
        if size_bytes < 1024:
            return f"{size_bytes} bytes"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"

    def _has_tests(self, content: str) -> bool:
        """Check if Python file has tests."""
        has_testcase = bool(re.search(r"\bunittest\.TestCase\b", content, re.I))
        has_test_class = bool(
            re.search(r"class\s+\w*Test\w*\s*\([^)]*unittest\.TestCase", content, re.I)
        )
        has_test_func = bool(re.search(r"def\s+test_\w+\s*\(", content))
        return has_testcase or has_test_class or has_test_func

    def _check_keyring_in_tests(self, content: str) -> Dict[str, Any]:
        """Check if keyring is used in test code (after TestCase)."""
        # Find where TestCase appears in the file
        testcase_match = re.search(r"\bTestCase\b", content, re.IGNORECASE)

        if testcase_match:
            # Get content after TestCase
            content_after_testcase = content[testcase_match.end() :]

            # Check if keyring appears after TestCase
            if re.search(r"\bkeyring\b", content_after_testcase, re.IGNORECASE):
                return {
                    "success": False,
                    "error": "Tests should never access keyring - the word 'keyring' appears in test code",
                }

        return {"success": True}

    def _run_file_tests(self, root: Path, rel_path: str) -> Dict[str, Any]:
        """Run unit tests for a single Python file."""
        file_path = root / rel_path
        content = file_path.read_text()

        # First check if the file has any tests
        if not self._has_tests(content):
            return {
                "success": False,
                "error": "No unit tests found in file",
                "diagnostics": {
                    "test_check": [
                        "didn't find the word 'unittest.TestCase'",
                        "didn't find any class inheriting from TestCase",
                        "didn't find any function starting with 'test_'",
                    ]
                },
            }

        # Run the tests
        try:
            module_path = rel_path.replace("/", ".").replace(".py", "")
            output = subprocess.check_output(
                [sys.executable, "-m", "unittest", module_path, "-v"],
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=60,  # 60 second timeout
            )

            # Check if any tests were actually run
            if "Ran 0 tests" in output:
                return {
                    "success": False,
                    "error": "No tests were executed (Ran 0 tests)",
                    "diagnostics": {"test_output": output},
                }

            return {"success": True, "diagnostics": {"test_output": output}}

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Unit tests timed out after 60 seconds",
                "diagnostics": {"test_error": "Tests exceeded timeout limit"},
            }
        except subprocess.CalledProcessError as exc:
            return {
                "success": False,
                "error": "Unit tests failed",
                "diagnostics": {"test_output": exc.output},
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Error running tests: {str(exc)}",
                "diagnostics": {"test_error": traceback.format_exc()},
            }

    def _run_project_tests(self, root: Path) -> Dict[str, Any]:
        """Run all unit tests in the project."""
        try:
            output = subprocess.check_output(
                [sys.executable, "-m", "unittest", "discover", "-v", "-p", "*.py"],
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=300,  # 5 minute timeout for whole project
            )

            if "Ran 0 tests" in output:
                return {
                    "success": False,
                    "error": "No tests discovered in project",
                    "diagnostics": {"unittest_output": output},
                }

            return {"success": True, "diagnostics": {"unittest_output": output}}

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Project tests timed out after 5 minutes",
                "diagnostics": {"unittest_error": "Tests exceeded timeout limit"},
            }
        except subprocess.CalledProcessError as exc:
            return {
                "success": False,
                "error": "Unit tests failed",
                "diagnostics": {"unittest_failures": exc.output},
            }

    def _run_pylint_file(self, root: Path, rel_path: str) -> Dict[str, Any]:
        """Run pylint on a single Python file."""
        pylint_cmd = (
            ["pylint"] if shutil.which("pylint") else [sys.executable, "-m", "pylint"]
        )
        pylint_cmd += ["-f", "json", "--exit-zero", rel_path]

        try:
            pylint_json = subprocess.check_output(
                pylint_cmd,
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=30,  # 30 second timeout per file
            )

            pylint_results = json.loads(pylint_json or "[]")
            issues = [i for i in pylint_results if i["type"] in {"error", "warning"}]

            if issues:
                # Format issues for readability
                issue_summary = []
                for issue in issues[:5]:  # Show first 5 issues
                    issue_summary.append(
                        f"{issue['type']}: {issue['message']} "
                        f"(line {issue.get('line', '?')})"
                    )

                return {
                    "success": False,
                    "error": f"Pylint found {len(issues)} error(s)/warning(s)",
                    "diagnostics": {
                        "pylint_issues": issues,
                        "pylint_summary": issue_summary,
                    },
                }

            return {"success": True, "diagnostics": {"pylint_clean": True}}

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Pylint timed out after 30 seconds",
                "diagnostics": {"pylint_error": "Pylint exceeded timeout limit"},
            }
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            return {
                "success": False,
                "error": f"Pylint invocation failed: {str(exc)}",
                "diagnostics": {"pylint_error": str(exc)},
            }

    def _run_pylint_project(self, root: Path) -> Dict[str, Any]:
        """Run pylint on the entire project."""
        pylint_cmd = (
            ["pylint"] if shutil.which("pylint") else [sys.executable, "-m", "pylint"]
        )
        pylint_cmd += ["-f", "json", "--exit-zero", "."]

        try:
            pylint_json = subprocess.check_output(
                pylint_cmd,
                cwd=root,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=300,  # 5 minute timeout for whole project
            )

            pylint_results = json.loads(pylint_json or "[]")
            issues = [i for i in pylint_results if i["type"] in {"error", "warning"}]

            if issues:
                return {
                    "success": False,
                    "error": f"Pylint reported {len(issues)} issue(s)",
                    "diagnostics": {"pylint": pylint_results},
                }

            return {"success": True, "diagnostics": {"pylint": pylint_results}}

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Pylint timed out after 5 minutes",
                "diagnostics": {"pylint_error": "Pylint exceeded timeout limit"},
            }
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            return {
                "success": False,
                "error": "Pylint invocation failed",
                "diagnostics": {"pylint_error": str(exc)},
            }
