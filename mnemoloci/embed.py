"""Optional local semantic search: multilingual sentence embeddings, either fastembed +
onnxruntime (default, no API, no cost, no server) or a local Ollama server
(`mnemoloci config embeddings ollama --ollama-model ...`) for whoever already runs one and
wants its embedding models instead. Vectors are a derived cache in index.sqlite keyed by each
page's sha256 AND the model id, so switching provider or model never mixes embedding spaces --
it just makes every page look stale for the new one until `mnemoloci index embed` runs again."""

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

from .core.errors import MnemoError
from .core.lock import VaultLock
from .index import db_path
from .pages import iter_pages
from .vault import CLOUD_EMBEDDING_PROVIDERS, OLLAMA_DEFAULT_URL, get_api_key, load_config, require_vault

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CHUNK_CHARS = 900
OLLAMA_TIMEOUT = 60
CLOUD_TIMEOUT = 60
# Claude itself has no embeddings endpoint -- Anthropic's own docs point to Voyage AI instead.
CLOUD_ENDPOINTS = {"openai": "https://api.openai.com/v1/embeddings", "voyage": "https://api.voyageai.com/v1/embeddings"}
CLOUD_DEFAULT_MODEL = {"openai": "text-embedding-3-small", "voyage": "voyage-4-large"}
_DDL = "CREATE TABLE IF NOT EXISTS vectors(path TEXT, chunk INTEGER, sha TEXT, model TEXT, vec BLOB)"
_model = None


def _provider(vault: Path) -> dict[str, Any]:
    return load_config(vault)["embeddings"]


def model_id(vault: Path) -> str:
    """The `vectors.model` key for the vault's active provider -- fastembed's fixed model, or
    `<provider>:<model>` for ollama/openai/voyage (never bare, so two different models for the
    same provider never collide)."""
    cfg = _provider(vault)
    provider = cfg["provider"]
    if provider == "fastembed":
        return MODEL
    if provider in CLOUD_EMBEDDING_PROVIDERS:
        return f"{provider}:{cfg.get(f'{provider}_model') or CLOUD_DEFAULT_MODEL[provider]}"
    return f"ollama:{cfg['ollama_model']}"


def available(vault: Path) -> bool:
    """Whether the vault's *configured* provider can run: fastembed+numpy for the default, or
    just numpy for ollama/openai/voyage (all three talk plain HTTP, no extra package -- an
    unreachable server or a missing API key surfaces when actually embedding, not here, to keep
    this a cheap, offline check)."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    if _provider(vault)["provider"] != "fastembed":
        return True
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def _ollama_embed(vault: Path, texts: list[str]):
    import numpy as np

    cfg = _provider(vault)
    base = (cfg.get("ollama_base_url") or OLLAMA_DEFAULT_URL).rstrip("/")
    body = json.dumps({"model": cfg["ollama_model"], "input": texts}).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/embed", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("OLLAMA_UNREACHABLE", f"cannot reach Ollama at {base}: {exc}") from exc
    if "embeddings" not in data:
        raise MnemoError("OLLAMA_BAD_RESPONSE", f"unexpected response from {base}/api/embed: {data.get('error', data)}")
    return np.asarray(data["embeddings"], dtype=np.float32)


def _cloud_embed(vault: Path, texts: list[str], provider: str):
    """OpenAI and Voyage share the same request/response shape: POST {model, input}, Bearer auth,
    response {"data": [{"embedding": [...], "index": n}, ...]}."""
    import numpy as np

    key = get_api_key(vault, provider)
    if not key:
        from .vault import API_KEY_ENV

        raise MnemoError("NO_API_KEY", f"no {provider} API key: `mnemoloci config embeddings-key {provider} <key>`, "
                         f"or set the {API_KEY_ENV[provider]} environment variable")
    cfg = _provider(vault)
    model = cfg.get(f"{provider}_model") or CLOUD_DEFAULT_MODEL[provider]
    body = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(CLOUD_ENDPOINTS[provider], data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise MnemoError("CLOUD_EMBED_FAILED", f"{provider} API error {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise MnemoError("CLOUD_EMBED_FAILED", f"cannot reach {provider}: {exc}") from exc
    if "data" not in data:
        raise MnemoError("CLOUD_EMBED_FAILED", f"unexpected response from {provider}: {data}")
    ordered = sorted(data["data"], key=lambda e: e["index"])
    return np.asarray([e["embedding"] for e in ordered], dtype=np.float32)


def _embed(vault: Path, texts: list[str]):
    """Batch-embed `texts` with the vault's active provider, as unit-normalised row vectors."""
    import numpy as np

    provider = _provider(vault)["provider"]
    if provider == "ollama":
        vectors = _ollama_embed(vault, texts)
    elif provider in CLOUD_EMBEDDING_PROVIDERS:
        vectors = _cloud_embed(vault, texts, provider)
    else:
        vectors = np.asarray(list(_embedder().embed(texts, batch_size=32)), dtype=np.float32)
    return vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-12)


def models_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "mnemoloci" / "models"


def _embedder():
    global _model
    if _model is None:
        try:
            import fastembed  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as exc:
            raise MnemoError("EMBEDDINGS_UNAVAILABLE", "install the extra: pip install -e .[semantic]") from exc
        # Keep hub chatter (symlink warning on Windows, progress bars) out of CLI/MCP output.
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("HF_HUB_VERBOSITY", "error")
        from fastembed import TextEmbedding

        models_dir().mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(model_name=MODEL, cache_dir=str(models_dir()))
    return _model


def _clean(text: str) -> str:
    text = re.sub(r"\^\[[^\]\n]*\]", "", text)                      # citations
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)       # [[slug|label]]
    text = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).replace("-", " "), text)
    return re.sub(r"[ \t]+", " ", text)


def chunks_of(title: str, summary: str, body: str) -> list[str]:
    head = f"{title}. {summary}".strip()
    out, buf = [head], ""
    for para in re.split(r"\n\s*\n", _clean(body)):
        para = para.strip()
        if not para:
            continue
        if buf and len(buf) + len(para) > CHUNK_CHARS:
            out.append(f"{title}: {buf}")
            buf = ""
        buf = f"{buf}\n{para}" if buf else para[: CHUNK_CHARS * 2]
    if buf:
        out.append(f"{title}: {buf}")
    return out


def head_vectors(vault: Path) -> dict[str, Any]:
    """path -> unit vector of the page's title+summary chunk, for pages whose embedding is current.
    Empty when embeddings were never computed or numpy is missing."""
    if not available(vault) or not has_vectors(vault):
        return {}
    import numpy as np

    with closing(_connect(vault)) as conn:
        rows = conn.execute(
            "SELECT v.path, v.vec FROM vectors v JOIN pages p ON p.path = v.path AND p.sha256 = v.sha "
            "WHERE v.model = ? AND v.chunk = 0", (model_id(vault),)).fetchall()
    return {path: np.frombuffer(blob, dtype=np.float32) for path, blob in rows}


def _connect(vault: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(vault))
    conn.execute(_DDL)
    return conn


def build(vault: Path) -> dict[str, Any]:
    require_vault(vault)
    model = model_id(vault)
    with VaultLock(vault), closing(_connect(vault)) as conn, conn:
        current = {r[0]: r[1] for r in conn.execute("SELECT DISTINCT path, sha FROM vectors WHERE model = ?", (model,))}
        pages = [p for p in iter_pages(vault) if not p.error]
        todo = [p for p in pages if current.get(p.path) != p.sha256]
        live = {p.path for p in pages}
        removed = [path for path in current if path not in live]
        for path in removed + [p.path for p in todo]:
            conn.execute("DELETE FROM vectors WHERE path = ?", (path,))
        texts, owners = [], []
        for p in todo:
            for i, chunk in enumerate(chunks_of(p.title, str(p.frontmatter.get("summary", "")), p.body)):
                texts.append(chunk)
                owners.append((p.path, i, p.sha256))
        if texts:
            vectors = _embed(vault, texts)
            conn.executemany("INSERT INTO vectors VALUES(?,?,?,?,?)",
                             [(path, i, sha, model, vec.tobytes()) for (path, i, sha), vec in zip(owners, vectors)])
    return {"embedded_pages": len(todo), "chunks": len(texts), "unchanged": len(pages) - len(todo),
            "removed": len(removed), "model": model}


def has_vectors(vault: Path) -> bool:
    if not db_path(vault).exists():
        return False
    with closing(_connect(vault)) as conn:
        return conn.execute("SELECT 1 FROM vectors WHERE model = ? LIMIT 1", (model_id(vault),)).fetchone() is not None


def search(vault: Path, query: str, limit: int = 30) -> list[tuple[str, float]]:
    """Best cosine similarity per page, ignoring vectors of pages edited since embedding."""
    with closing(_connect(vault)) as conn:
        rows = conn.execute(
            "SELECT v.path, v.vec FROM vectors v JOIN pages p ON p.path = v.path AND p.sha256 = v.sha WHERE v.model = ?",
            (model_id(vault),)).fetchall()
    if not rows:
        return []
    import numpy as np

    [q] = _embed(vault, [query])
    matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = matrix @ q
    best: dict[str, float] = {}
    for (path, _), score in zip(rows, scores.tolist()):
        best[path] = max(best.get(path, -1.0), score)
    return sorted(best.items(), key=lambda kv: -kv[1])[:limit]


def stale_count(vault: Path) -> int:
    if not has_vectors(vault):
        return 0
    with closing(_connect(vault)) as conn:
        return conn.execute(
            """SELECT COUNT(*) FROM pages p WHERE NOT EXISTS
               (SELECT 1 FROM vectors v WHERE v.path = p.path AND v.sha = p.sha256 AND v.model = ?)""",
            (model_id(vault),)).fetchone()[0]
