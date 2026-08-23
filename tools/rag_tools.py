import os
import tempfile
import subprocess
from typing import List, Dict, Any

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.documents import Document


# Load embeddings once and reuse — this model is downloaded on first run.
_EMBEDDINGS = None


def get_embeddings():
    global _EMBEDDINGS
    if _EMBEDDINGS is None:
        _EMBEDDINGS = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _EMBEDDINGS


def clone_repo_at_ref(repo_full_name: str, ref: str) -> str:
    """
    Shallow-clones the repo at a specific branch/commit into a temp dir.
    Returns the local path. Caller is responsible for cleanup if desired.
    """
    tmp_dir = tempfile.mkdtemp(prefix="repo_rag_")
    token = os.environ.get("GITHUB_TOKEN", "")
    clone_url = f"https://{token}@github.com/{repo_full_name}.git"

    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, clone_url, tmp_dir],
        check=True,
        capture_output=True,
    )
    return tmp_dir


def _should_index(filepath: str) -> bool:
    """Skip binary, vendor, and non-code files."""
    skip_dirs = ("node_modules", ".git", "venv", "__pycache__", "dist", "build")
    code_exts = (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rb", ".c", ".cpp", ".cs")
    if any(part in filepath for part in skip_dirs):
        return False
    return filepath.endswith(code_exts)


def build_vector_store(repo_local_path: str) -> FAISS:
    """
    Walks the repo, chunks all code files, and builds a FAISS index.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
        separators=["\nclass ", "\ndef ", "\nfunction ", "\n\n", "\n", " "],
    )

    documents: List[Document] = []
    for root, _, files in os.walk(repo_local_path):
        for fname in files:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, repo_local_path)

            if not _should_index(rel_path):
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception:
                continue

            if not content.strip():
                continue

            chunks = splitter.split_text(content)
            for chunk in chunks:
                documents.append(Document(page_content=chunk, metadata={"source": rel_path}))

    if not documents:
        # Avoid FAISS crashing on empty input
        documents = [Document(page_content="", metadata={"source": "empty"})]

    return FAISS.from_documents(documents, get_embeddings())


def retrieve_related_context(
    vector_store: FAISS, changed_file_content: str, k: int = 5
) -> List[str]:
    """
    Given the content of a changed file (or diff hunk), retrieve the
    k most related code chunks from elsewhere in the repo.
    """
    results = vector_store.similarity_search(changed_file_content, k=k)
    return [f"[{doc.metadata.get('source', 'unknown')}]\n{doc.page_content}" for doc in results]