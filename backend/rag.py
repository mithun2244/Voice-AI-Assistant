"""
Local ChromaDB vector store for the agent's knowledge base:
the owner's resume and GitHub project data.

`seed_store()` loads plain-text/markdown files from ./knowledge into a
persistent Chroma collection, embedding them with an NVIDIA NIM embedding
model. `get_retriever()` returns a LangChain retriever the graph queries
each turn.

Seed the store once:

    python backend/rag.py
"""

from __future__ import annotations

import os
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_nvidia_ai_endpoints import NVIDIAEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Load the project-root .env explicitly (it lives one level up from backend/),
# so this works no matter which directory the process is launched from.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Persist alongside this file so the DB survives restarts.
PERSIST_DIR = str(Path(__file__).parent / "chroma_db")
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
COLLECTION = "resume_and_projects"

# knowledge/README.md is instructions for the user, not agent knowledge.
_SKIP_FILES = {"README.md"}

# NVIDIA NIM embedding model (swap for any model your key can access).
_embeddings = NVIDIAEmbeddings(model="nvidia/nv-embedqa-e5-v5")


def _vectorstore() -> Chroma:
    return Chroma(
        collection_name=COLLECTION,
        embedding_function=_embeddings,
        persist_directory=PERSIST_DIR,
    )


def get_retriever(k: int = 4):
    """Return a retriever over the resume / project knowledge base."""
    return _vectorstore().as_retriever(search_kwargs={"k": k})


def seed_store() -> None:
    """(Re)load everything under ./knowledge into the vector store.

    Idempotent: drops any existing collection first so re-running after you
    edit resume.md / projects.md replaces the data instead of duplicating it.
    """
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    files = [
        p
        for p in KNOWLEDGE_DIR.glob("**/*")
        if p.suffix in {".md", ".txt"} and p.name not in _SKIP_FILES
    ]
    if not files:
        print(f"No .md/.txt files found in {KNOWLEDGE_DIR}. "
              "Drop your resume and project notes there, then re-run.")
        return

    # Reset the collection so seeding is idempotent (avoids duplicate chunks).
    client = chromadb.PersistentClient(path=PERSIST_DIR)
    try:
        client.delete_collection(COLLECTION)
        print(f"Cleared existing '{COLLECTION}' collection.")
    except Exception:
        pass  # collection didn't exist yet — fine

    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
    texts, metadatas = [], []
    for path in files:
        chunks = splitter.split_text(path.read_text(encoding="utf-8"))
        texts.extend(chunks)
        metadatas.extend({"source": path.name} for _ in chunks)
        print(f"  {path.name}: {len(chunks)} chunks")

    store = _vectorstore()
    store.add_texts(texts=texts, metadatas=metadatas)
    print(f"[OK] Seeded {len(texts)} chunks from {len(files)} file(s) into '{COLLECTION}'.")
    print(f"     Collection now holds {store._collection.count()} documents.")


if __name__ == "__main__":
    if not os.getenv("NVIDIA_API_KEY"):
        print("[!] NVIDIA_API_KEY is not set — check the project-root .env.")
    seed_store()
