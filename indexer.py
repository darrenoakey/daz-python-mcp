# indexer.py – vector index backed by ChromaDB (character-accurate)

from pathlib import Path
from typing import Any, Dict, List

import chromadb
from chromadb.config import Settings

from handler_base import chunk_text


# --------------------------------------------------------------------------- #
#  ChromaDB wrapper
# --------------------------------------------------------------------------- #
class CodeIndexer:
    # constructor: create/retrieve the collection
    def __init__(self):
        self.cli = chromadb.Client(
            Settings(
                persist_directory=str(Path.cwd() / "chroma_db"),
                anonymized_telemetry=False,
            )
        )
        try:
            self.col = self.cli.get_collection("code_chunks")
        except Exception:
            self.col = self.cli.create_collection("code_chunks")

    # ----------------------------------------------------------------------- #
    #  Helpers
    # ----------------------------------------------------------------------- #
    def _add_file(self, repo: str, abs_path: Path, rel: str):
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
        docs, metas, ids = [], [], []
        for chunk, off in chunk_text(text):
            docs.append(chunk)
            metas.append({"repo": repo, "file": rel, "offset": off})
            ids.append(f"{repo}:{rel}:{off}")
        if docs:
            self.col.add(documents=docs, metadatas=metas, ids=ids)

    # ----------------------------------------------------------------------- #
    #  Public API
    # ----------------------------------------------------------------------- #
    # bulk index when repo opens
    def index_repository(self, repo: str, root: Path, structs: Dict[str, Any]):
        self.col.delete(where={"repo": repo})
        for rel in structs:
            self._add_file(repo, root / rel, rel)

    # re-index one file after write/add
    def update_file(self, repo: str, root: Path, rel: str):
        # New: conform to strict filter requirement (single operator key)
        self.col.delete(
            where={
                "$and": [
                    {"repo": repo},
                    {"file": rel},
                ]
            }
        )
        self._add_file(repo, root / rel, rel)

    # vector search
    def search(self, repo: str, query: str, limit: int):
        res = self.col.query(query_texts=[query], n_results=limit, where={"repo": repo})
        return [
            {"file": meta["file"], "snippet": doc}
            for doc, meta in zip(res["documents"][0], res["metadatas"][0])
        ]
