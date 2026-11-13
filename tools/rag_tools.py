import os
from typing import List

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document


# Load embeddings once and reuse — this model is downloaded on first run.
_EMBEDDINGS = None


def get_embeddings():
    global _EMBEDDINGS
    if _EMBEDDINGS is None:
        _EMBEDDINGS = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _EMBEDDINGS


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