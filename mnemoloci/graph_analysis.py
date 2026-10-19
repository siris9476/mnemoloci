"""What the link graph says about the wiki. Pure Python, deterministic, read-only.

- people / entities: entity pages ranked by how much they matter now:
  (1 + pages linking them) x (1 + sources) x recency. Ranking only: nothing fades.
- analyze: hubs (most connected pages), bridges (pages on many shortest paths
  between others, Brandes betweenness), communities (Louvain modularity),
  surprising links (between different communities) and isolated pages.
"""

from collections import Counter, deque
from datetime import date
from typing import Any
from pathlib import Path

from .core.errors import MnemoError
from .markdown import wikilink_targets
from .pages import Page, alias_map, iter_pages
from .vault import require_vault


def _graph(vault: Path) -> tuple[dict[str, Page], dict[str, set[str]], dict[str, int]]:
    """Content pages (no answers, no merge stubs) -> undirected adjacency + inbound link counts."""
    pages = {p.slug: p for p in iter_pages(vault, ("wiki",))
             if not p.error and not p.merged_into and p.frontmatter.get("kind") not in (None, "query")}
    amap = alias_map(list(pages.values()))
    adj: dict[str, set[str]] = {s: set() for s in pages}
    inbound: Counter = Counter()
    for slug, page in pages.items():
        for target in set(wikilink_targets(page.body)):
            dst = amap.get(target)
            if dst in pages and dst != slug:
                adj[slug].add(dst)
                adj[dst].add(slug)
                inbound[dst] += 1
    return pages, adj, dict(inbound)


def _recency(updated: Any) -> float:
    try:
        days = (date.today() - date.fromisoformat(str(updated)[:10])).days
    except ValueError:
        return 0.5
    return 1 / (1 + max(days, 0) / 90)


def entities(vault: Path, *, entity_type: str | None = "person", limit: int = 50) -> dict[str, Any]:
    require_vault(vault)
    pages, adj, inbound = _graph(vault)
    out = []
    for slug, page in pages.items():
        fm = page.frontmatter
        if fm.get("kind") != "entity" or (entity_type and fm.get("entityType") != entity_type):
            continue
        sources = [s for s in fm.get("sources") or [] if isinstance(s, dict)]
        score = (1 + inbound.get(slug, 0)) * (1 + len(sources)) * _recency(fm.get("updated"))
        out.append({"slug": slug, "path": page.path, "title": page.title, "summary": fm.get("summary"),
                    "entity_type": fm.get("entityType"), "mentioned_by": inbound.get(slug, 0),
                    "sources": len(sources), "updated": fm.get("updated"), "score": round(score, 3),
                    "related": sorted(pages[n].title for n in adj[slug])[:8]})
    out.sort(key=lambda e: (-e["score"], e["title"].casefold()))
    return {"entities": out[:limit], "type": entity_type, "total": len(out)}


def _betweenness(adj: dict[str, set[str]]) -> dict[str, float]:
    """Brandes' algorithm for unweighted graphs, normalised to 0..1."""
    score = dict.fromkeys(adj, 0.0)
    for s in sorted(adj):
        stack, pred = [], {v: [] for v in adj}
        sigma, dist = dict.fromkeys(adj, 0), dict.fromkeys(adj, -1)
        sigma[s], dist[s] = 1, 0
        queue = deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in sorted(adj[v]):
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = dict.fromkeys(adj, 0.0)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
            if w != s:
                score[w] += delta[w]
    n = len(adj)
    norm = (n - 1) * (n - 2) if n > 2 else 1
    return {v: s / norm for v, s in score.items()}


def _local_moves(graph: dict[str, dict[str, float]]) -> tuple[dict[str, str], bool]:
    """Louvain phase 1: move each node to the neighbouring community with the best modularity
    gain, in a fixed order, until nothing moves. Self-loops hold a community's internal weight."""
    two_m = sum(sum(n.values()) for n in graph.values())
    comm = {v: v for v in graph}
    if not two_m:
        return comm, False
    deg = {v: sum(graph[v].values()) for v in graph}
    tot = dict(deg)
    moved, improved = False, True
    while improved:
        improved = False
        for v in sorted(graph):
            here = comm[v]
            links: Counter = Counter()
            for w, weight in graph[v].items():
                if w != v:
                    links[comm[w]] += weight
            tot[here] -= deg[v]
            best, gain = here, links.get(here, 0.0) - tot[here] * deg[v] / two_m
            for c in sorted(links):
                g = links[c] - tot[c] * deg[v] / two_m
                if g > gain + 1e-12:
                    best, gain = c, g
            tot[best] += deg[v]
            if best != here:
                comm[v], improved, moved = best, True, True
    return comm, moved


def _communities(adj: dict[str, set[str]], levels: int = 10) -> dict[str, str]:
    """Deterministic Louvain (modularity): communities of pages more linked to each other than
    to the rest. Each community is labelled by its alphabetically first page."""
    graph = {v: {w: 1.0 for w in adj[v]} for v in adj}
    member = {v: v for v in adj}
    for _ in range(levels):
        comm, moved = _local_moves(graph)
        if not moved:
            break
        member = {v: comm[c] for v, c in member.items()}
        folded: dict[str, dict[str, float]] = {c: {} for c in set(comm.values())}
        for v, neighbours in graph.items():
            for w, weight in neighbours.items():
                row = folded[comm[v]]
                row[comm[w]] = row.get(comm[w], 0.0) + weight
        graph = folded
    groups: dict[str, list[str]] = {}
    for v, c in member.items():
        groups.setdefault(c, []).append(v)
    return {v: min(groups[c]) for v, c in member.items()}


def analyze(vault: Path, *, top: int = 10, betweenness: bool = True) -> dict[str, Any]:
    """betweenness=False skips Brandes (O(V*E), the dominant cost on a large vault -- ~15s at 2500
    pages/5600 links, measured) for a caller that only needs communities/surprising_links, like the
    digest, and doesn't want `bridges` at all."""
    require_vault(vault)
    pages, adj, _ = _graph(vault)
    title = {s: p.title for s, p in pages.items()}
    edges = sum(len(n) for n in adj.values()) // 2
    degree = {v: len(n) for v, n in adj.items()}
    between = _betweenness(adj) if betweenness and len(adj) <= 3000 else {}
    label = _communities(adj)
    groups: dict[str, list[str]] = {}
    for v, l in label.items():
        if adj[v]:
            groups.setdefault(l, []).append(v)
    communities = []
    for members in sorted(groups.values(), key=lambda m: (-len(m), sorted(m)[0])):
        if len(members) < 2:
            continue
        hub = min(members, key=lambda v: (-degree[v], v))  # most connected page names the community
        communities.append({"label": title[hub], "size": len(members),
                            "pages": sorted((title[m] for m in members), key=str.casefold)[:15],
                            "slugs": sorted(members)})  # all of them (pages is a preview)
    big = {l for l, m in groups.items() if len(m) >= 3}
    surprising = sorted(({"a": title[a], "b": title[b], "a_slug": a, "b_slug": b,
                          "between": [title[label[a]], title[label[b]]]}
                         for a in sorted(adj) for b in sorted(adj[a])  # every edge once, not every pair
                         if a < b and label[a] != label[b] and label[a] in big and label[b] in big),
                        key=lambda e: (e["a"], e["b"]))
    return {
        "pages": len(pages), "links": edges,
        "hubs": [{"title": title[v], "slug": v, "links": degree[v]}
                 for v in sorted(adj, key=lambda v: (-degree[v], v))[:top] if degree[v]],
        "bridges": [{"title": title[v], "slug": v, "score": round(between[v], 4)}
                    for v in sorted(between, key=lambda v: (-between[v], v))[:top] if between[v] > 0],
        "communities": communities,
        "surprising_links": surprising[:top],
        "isolated": sorted(title[v] for v in adj if not adj[v])[:50],
        "isolated_slugs": sorted(v for v in adj if not adj[v]),
        "community_of": {v: title[label[v]] for v in adj if adj[v]},
    }


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def backfill(vault: Path, page_path: str, *, at: str | None = None, window_days: int = 14,
             hops: int = 2, limit: int = 20) -> dict[str, Any]:
    """Pages near `page_path` in the link graph AND close in time to `at` (default: the anchor
    page's own `updated` date) -- what was going on around here, not what reads like it (graph + time proximity,
    never text similarity). A different lens from search: useful when a failure or a decision
    doesn't share vocabulary with whatever caused it, so semantic/keyword search would miss it but
    "what else changed near here around the same time" catches it."""
    require_vault(vault)
    pages, adj, _ = _graph(vault)
    by_path = {p.path: s for s, p in pages.items()}
    slug = by_path.get(page_path)
    if slug is None:
        raise MnemoError("NO_SUCH_PAGE", f"no wiki page {page_path}")
    anchor_date = _parse_date(at) if at else _parse_date(pages[slug].frontmatter.get("updated"))
    if anchor_date is None:
        raise MnemoError("NO_ANCHOR_DATE", f"{page_path} has no usable `updated` date; pass `at` explicitly")
    visited = {slug: 0}
    queue = deque([slug])
    while queue:
        v = queue.popleft()
        if visited[v] >= hops:
            continue
        for w in sorted(adj[v]):
            if w not in visited:
                visited[w] = visited[v] + 1
                queue.append(w)
    results = []
    for v, dist in visited.items():
        if v == slug:
            continue
        d = _parse_date(pages[v].frontmatter.get("updated"))
        if d is None:
            continue
        delta = abs((d - anchor_date).days)
        if delta <= window_days:
            results.append({"slug": v, "title": pages[v].title, "path": pages[v].path, "hops": dist,
                            "updated": pages[v].frontmatter.get("updated"), "days_from_anchor": delta})
    results.sort(key=lambda r: (r["hops"], r["days_from_anchor"], r["title"].casefold()))
    return {"anchor": {"slug": slug, "title": pages[slug].title, "path": page_path, "at": anchor_date.isoformat()},
            "window_days": window_days, "hops": hops, "results": results[:limit]}
