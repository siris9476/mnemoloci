from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .core.errors import ValidationError
from .core.hashing import sha256_bytes
from .markdown import slugify, split_frontmatter

INDEXED_ROOTS = ("wiki", "memory")
GENERATED = {"wiki/index.md", "wiki/log.md"}


@dataclass
class Page:
    path: str
    slug: str
    frontmatter: dict[str, Any]
    body: str
    sha256: str
    error: str | None = None

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title") or self.slug)

    @property
    def aliases(self) -> list[str]:
        raw = self.frontmatter.get("aliases") or []
        return [str(a) for a in raw] if isinstance(raw, list) else []

    @property
    def merged_into(self) -> str | None:
        """A page merged into another is kept as a short stub pointing to it (nothing is deleted)."""
        value = self.frontmatter.get("mergedInto")
        return str(value) if value else None

    @property
    def keys(self) -> set[str]:
        return {self.slug, slugify(self.title), *(slugify(a) for a in self.aliases)}


def load_page(vault: Path, rel: str) -> Page:
    data = (Path(vault) / rel).read_bytes()
    slug = Path(rel).stem
    try:
        frontmatter, body = split_frontmatter(data.decode("utf-8"))
        return Page(rel, slug, frontmatter or {}, body, sha256_bytes(data))
    except (ValidationError, UnicodeDecodeError) as exc:
        return Page(rel, slug, {}, "", sha256_bytes(data), error=str(exc))


def iter_pages(vault: Path, roots: tuple[str, ...] = INDEXED_ROOTS) -> Iterator[Page]:
    vault = Path(vault)
    for root in roots:
        base = vault / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(vault).as_posix()
            if rel not in GENERATED:
                yield load_page(vault, rel)


def alias_map(pages: list[Page]) -> dict[str, str]:
    """Map every slugified title/alias to its page slug; first page (sorted) wins.
    Merge stubs are skipped: their names live on as aliases of the page they merged into."""
    mapping: dict[str, str] = {}
    for page in sorted(pages, key=lambda p: p.path):
        if page.merged_into:
            continue
        for key in sorted(page.keys):
            mapping.setdefault(key, page.slug)
    return mapping
