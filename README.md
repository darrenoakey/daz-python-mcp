![](banner.jpg)

# daz-python-mcp

## Purpose

This project provides a Managed Code Protocol (MCP) server for Python code navigation and modification within a defined set of repositories. It exposes tools for code outlining, content retrieval, writing, adding new elements, and searching. It also enforces code quality checks (pylint and unit tests) before committing changes.

## Usage

This MCP server exposes the following tools, all prefixed with `dazbuild_`:

*   **guidelines**:  Provides guidance on how to use the dazbuild tools.
*   **list_repositories**: Lists the repositories configured in `config.json`.
*   **open_repository**: Opens a repository and indexes its files for code navigation and modification. Requires a `name` parameter.
*   **close_repository**: Closes a repository, releasing memory.  Requires a `name` parameter.
*   **start_change**: Starts a change session for a repository. This *must* be called before any write or add operations. Requires a `name` parameter.
*   **end_change**: Ends a change session, running pylint and unit tests.  Commits changes if all tests pass. Requires `name` and `message` parameters.
*   **outline**: Returns the code hierarchy of a file within a repository. Requires `name` and `reference` parameters.
*   **get**: Gets the content of a code element at a specific reference. Requires `name` and `reference` parameters.
*   **write**: Replaces the content of a code element at a specific reference. Requires `name`, `reference`, and `content` parameters. *Requires `start_change` first.*
*   **add**: Adds a new code element or file. Requires `name`, `type`, `parent_reference`, `object_name`, and `content` parameters. *Requires `start_change` first.*
*   **search**: Performs a vector search within a repository. Requires `name` and `query` parameters, and optionally accepts a `limit` parameter.

## Configuration

The `config.json` file defines the repositories that the MCP server can access.  It should contain a `repositories` section with key-value pairs where the key is the repository name and the value is the absolute path to the repository on the file system.  Example:

```json
{
  "repositories": {
    "example": "/Users/darrenoakey/src/daz-python-mcp/example"
  }
}
```

## Examples

Here are some example interactions using the MCP protocol (these are examples of how the client would invoke the tools; the specific implementation depends on the MCP client):

1.  **List repositories:**

    ```json
    {"method": "call_tool", "params": {"name": "dazbuild_list_repositories", "args": {}}}
    ```

2.  **Open a repository named "example":**

    ```json
    {"method": "call_tool", "params": {"name": "dazbuild_open_repository", "args": {"name": "example"}}}
    ```

3.  **Start a change session for the "example" repository:**

    ```json
    {"method": "call_tool", "params": {"name": "dazbuild_start_change", "args": {"name": "example"}}}
    ```

4.  **Write content to a file:**

    ```json
    {"method": "call_tool", "params": {"name": "dazbuild_write", "args": {"name": "example", "reference": "myfile.py", "content": "print(\"Hello, world!\")"}}}
    ```

5.  **End a change session (commit changes):**

    ```json
    {"method": "call_tool", "params": {"name": "dazbuild_end_change", "args": {"name": "example", "message": "Update myfile.py"}}}
    ```

## Installation

1.  Ensure you have Python 3.7+ installed.
2.  Install the required Python packages. It is highly recommended to create a virtual environment before installation.

    ```bash
    python -m venv .venv
    source .venv/bin/activate  # or .venv\Scripts\activate on Windows
    pip install -r requirements.txt # if a requirements.txt is present
    pip install mcp chromadb tree_sitter_language_pack pylint unittest # if no requirements.txt is present, install individually
    ```
3.  Create a `config.json` file in the same directory as `daz-python-mcp.py` and configure your repositories as described in the Configuration section.

## Running the Server

Execute the `daz-python-mcp.py` script:

```bash
python daz-python-mcp.py
```

The server will then be running and listening for MCP requests via standard input/output.
