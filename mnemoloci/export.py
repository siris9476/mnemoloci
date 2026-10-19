"""Exports: the knowledge graph (GraphML, JSON, self-contained offline HTML) and
the whole vault as a JSON envelope per page (fields: kind, confidence, provenance, citations, freshness)."""

import html
import json
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from . import i18n
from .api import graph
from .core.hashing import sha256_file
from .markdown import citations, wikilink_targets
from .pages import iter_pages
from .sources import load_sources, resolve_root
from .vault import require_vault

FORMATS = ("graphml", "json", "html", "cypher", "sql")


def _cypher_str(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _cypher(g: dict[str, Any]) -> str:
    out = ["// Mnemoloci knowledge graph -- run with `cypher-shell` or the Neo4j browser"]
    for n in g["nodes"]:
        props = ", ".join(f"{k}: {_cypher_str(v)}" if isinstance(v, str) else f"{k}: {v}"
                          for k, v in (("path", n["path"]), ("title", n["title"]), ("kind", n["kind"]),
                                       ("confidence", n["confidence"])) if v is not None)
        out.append(f"MERGE (n:Page {{slug: {_cypher_str(n['id'])}}}) SET n += {{{props}}};")
    for e in g["links"]:
        out.append(f"MATCH (a:Page {{slug: {_cypher_str(e['source'])}}}), (b:Page {{slug: {_cypher_str(e['target'])}}}) "
                   f"MERGE (a)-[:LINKS_TO]->(b);")
    return "\n".join(out) + "\n"


def _sql_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _sql(g: dict[str, Any]) -> str:
    out = [
        "-- Mnemoloci knowledge graph -- run against Postgres (or any SQL database) as-is",
        "CREATE TABLE IF NOT EXISTS pages (slug TEXT PRIMARY KEY, path TEXT, title TEXT, kind TEXT, confidence DOUBLE PRECISION);",
        "CREATE TABLE IF NOT EXISTS links (source TEXT REFERENCES pages(slug), target TEXT REFERENCES pages(slug));",
    ]
    for n in g["nodes"]:
        conf = "NULL" if n["confidence"] is None else n["confidence"]
        out.append(f"INSERT INTO pages (slug, path, title, kind, confidence) VALUES "
                   f"({_sql_str(n['id'])}, {_sql_str(n['path'])}, {_sql_str(n['title'])}, {_sql_str(n['kind'])}, {conf});")
    for e in g["links"]:
        out.append(f"INSERT INTO links (source, target) VALUES ({_sql_str(e['source'])}, {_sql_str(e['target'])});")
    return "\n".join(out) + "\n"


def _graphml(g: dict[str, Any]) -> str:
    keys = [("kind", "string"), ("title", "string"), ("path", "string"), ("confidence", "double")]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">']
    out += [f'  <key id="{k}" for="node" attr.name="{k}" attr.type="{t}"/>' for k, t in keys]
    out.append('  <graph id="mnemoloci" edgedefault="directed">')
    for n in g["nodes"]:
        out.append(f"    <node id={quoteattr(n['id'])}>")
        for k, _ in keys:
            value = n.get(k)
            if value is not None:
                out.append(f'      <data key="{k}">{escape(str(value))}</data>')
        out.append("    </node>")
    for i, e in enumerate(g["links"]):
        out.append(f'    <edge id="e{i}" source={quoteattr(e["source"])} target={quoteattr(e["target"])}/>')
    out += ["  </graph>", "</graphml>"]
    return "\n".join(out) + "\n"


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mnemoloci — graph</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">
<style>
body{margin:0;font:14px system-ui,sans-serif;background:#fff;color:#222}
@media (prefers-color-scheme:dark){body{background:#15171c;color:#ddd}}
header{padding:10px 16px;border-bottom:1px solid #8884}#info{padding:0 16px;min-height:1.4em;color:#888}
canvas{display:block;width:100vw;height:calc(100vh - 70px)}
</style></head><body>
<header><b>Mnemoloci</b> — __COUNT__. Trascina per spostare, rotella per zoom, passa sopra un nodo per il titolo.</header>
<div id="info"></div><canvas id="c"></canvas>
<script>
const G = __DATA__;
const colors = {concept:"#4f7cff", entity:"#e0873a", comparison:"#2fb37a", overview:"#b05bd6", memory:"#8a8f98"};
const c = document.getElementById("c"), ctx = c.getContext("2d"), info = document.getElementById("info");
const dark = matchMedia("(prefers-color-scheme: dark)").matches;
const idx = new Map(G.nodes.map((n, i) => [n.id, i]));
const N = G.nodes.map((n, i) => ({...n, x: Math.cos(i) * 200 + Math.random(), y: Math.sin(i) * 200 + Math.random(), vx: 0, vy: 0}));
const E = G.links.map(l => [idx.get(l.source), idx.get(l.target)]).filter(([a, b]) => a !== undefined && b !== undefined);
let view = {x: 0, y: 0, k: 1}, drag = null, hover = null;
function resize() { c.width = c.clientWidth * devicePixelRatio; c.height = c.clientHeight * devicePixelRatio; }
function step() {
  for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {
    const a = N[i], b = N[j]; let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01;
    const f = 1800 / d2; a.vx += dx * f / Math.sqrt(d2); a.vy += dy * f / Math.sqrt(d2); b.vx -= dx * f / Math.sqrt(d2); b.vy -= dy * f / Math.sqrt(d2);
  }
  for (const [s, t] of E) { const a = N[s], b = N[t]; const dx = b.x - a.x, dy = b.y - a.y; a.vx += dx * 0.01; a.vy += dy * 0.01; b.vx -= dx * 0.01; b.vy -= dy * 0.01; }
  for (const n of N) { n.vx -= n.x * 0.002; n.vy -= n.y * 0.002; if (n !== drag) { n.x += n.vx *= 0.85; n.y += n.vy *= 0.85; } }
}
function draw() {
  const w = c.width, h = c.height, s = devicePixelRatio * view.k;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, w, h);
  ctx.setTransform(s, 0, 0, s, w / 2 + view.x * devicePixelRatio, h / 2 + view.y * devicePixelRatio);
  ctx.strokeStyle = dark ? "#3a3f4a" : "#cfd3da"; ctx.lineWidth = 1 / view.k;
  for (const [a, b] of E) { ctx.beginPath(); ctx.moveTo(N[a].x, N[a].y); ctx.lineTo(N[b].x, N[b].y); ctx.stroke(); }
  for (const n of N) {
    ctx.fillStyle = colors[n.kind] || "#888"; ctx.beginPath(); ctx.arc(n.x, n.y, n === hover ? 8 : 6, 0, 7); ctx.fill();
    if (view.k > 0.8 || n === hover) { ctx.fillStyle = dark ? "#ddd" : "#333"; ctx.font = `${11 / view.k}px system-ui`; ctx.fillText(n.title, n.x + 9, n.y + 4); }
  }
}
function toWorld(e) { const r = c.getBoundingClientRect(); return {x: (e.clientX - r.left - r.width / 2 - view.x) / view.k, y: (e.clientY - r.top - r.height / 2 - view.y) / view.k}; }
function pick(p) { return N.find(n => (n.x - p.x) ** 2 + (n.y - p.y) ** 2 < 100 / view.k); }
let pan = null;
c.onmousedown = e => { const p = toWorld(e); drag = pick(p); if (!drag) pan = {x: e.clientX - view.x, y: e.clientY - view.y}; };
c.onmousemove = e => { const p = toWorld(e); if (drag) { drag.x = p.x; drag.y = p.y; } else if (pan) { view.x = e.clientX - pan.x; view.y = e.clientY - pan.y; }
  hover = pick(p); info.textContent = hover ? `${hover.title} — ${hover.kind} — ${hover.path}` : ""; };
onmouseup = () => { drag = null; pan = null; };
c.onwheel = e => { e.preventDefault(); view.k = Math.min(5, Math.max(0.2, view.k * (e.deltaY < 0 ? 1.1 : 0.9))); };
onresize = resize; resize();
(function loop() { step(); draw(); requestAnimationFrame(loop); })();
</script></body></html>
"""


def _html(g: dict[str, Any]) -> str:
    # JSON inside <script>: escape "<" so page titles can never close the script tag.
    data = json.dumps(g, ensure_ascii=False).replace("<", "\\u003c")
    count = html.escape(f"{len(g['nodes'])} pagine, {len(g['links'])} collegamenti")
    return _HTML.replace("__DATA__", data).replace("__COUNT__", count)


def export_graph(vault: Path, fmt: str, out: str) -> dict[str, Any]:
    if fmt not in FORMATS:
        raise ValueError(fmt)
    g = graph(vault)
    text = {"graphml": _graphml, "json": lambda x: json.dumps(x, ensure_ascii=False, indent=1), "html": _html,
            "cypher": _cypher, "sql": _sql}[fmt](g)
    Path(out).write_text(text, "utf-8")
    return {"format": fmt, "out": str(Path(out).resolve()), "nodes": len(g["nodes"]), "links": len(g["links"])}


LLMS_SECTIONS = [("concept", "Concetti"), ("entity", "Entità"), ("comparison", "Confronti"),
                 ("overview", "Panoramiche"), ("query", "Risposte salvate")]
LLMS_SECTIONS_EN = [("concept", "Concepts"), ("entity", "Entities"), ("comparison", "Comparisons"),
                    ("overview", "Overviews"), ("query", "Saved answers")]


def export_llms(vault: Path, out_dir: str, *, title: str = "Second brain") -> dict[str, Any]:
    """llms.txt (an index with one line per page) and llms-full.txt (every page's text) for other
    agents and tools. Merge stubs are left out."""
    require_vault(vault)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from .context_pack import is_private

    pages = [p for p in iter_pages(vault, ("wiki",))
             if not p.error and not p.merged_into and p.frontmatter.get("kind") and not is_private(p.frontmatter)]
    it = i18n.lang(vault) == "it"
    index_lines = [f"# {title}", "", ("> Conoscenza personale compilata da Mnemoloci: pagine con citazioni alle fonti." if it
                                      else "> Personal knowledge compiled by Mnemoloci: pages with citations to their sources."), ""]
    full = [f"# {title} — " + ("testo completo" if it else "full text"), ""]
    for kind, label in (LLMS_SECTIONS if it else LLMS_SECTIONS_EN):
        group = sorted((p for p in pages if p.frontmatter.get("kind") == kind), key=lambda p: p.title.casefold())
        if not group:
            continue
        index_lines += [f"## {label}", ""]
        for p in group:
            summary = " ".join(str(p.frontmatter.get("summary") or "").split())
            index_lines.append(f"- [{p.title}]({p.path})" + (f": {summary}" if summary else ""))
            full += [f"---\n\n## {p.title}\n\n" + ("Percorso" if it else "Path") + f": {p.path}\n", p.body.strip(), ""]
        index_lines.append("")
    (out / "llms.txt").write_text("\n".join(index_lines).rstrip() + "\n", "utf-8")
    (out / "llms-full.txt").write_text("\n".join(full).rstrip() + "\n", "utf-8")
    return {"out": str(out.resolve()), "pages": len(pages), "files": ["llms.txt", "llms-full.txt"]}


JSONLD_TYPE = {"person": "Person", "organization": "Organization", "place": "Place", "event": "Event",
               "tool": "SoftwareApplication", "project": "CreativeWork"}


def export_jsonld(vault: Path, out: str, *, base: str = "urn:mnemoloci:") -> dict[str, Any]:
    """schema.org JSON-LD of the wiki: concepts as
    DefinedTerm, entities by their type, overviews/comparisons as Article, saved answers as Question.
    Private pages and merge stubs are left out."""
    require_vault(vault)
    from .context_pack import is_private
    from .pages import alias_map

    pages = [p for p in iter_pages(vault, ("wiki",))
             if not p.error and not p.merged_into and p.frontmatter.get("kind") and not is_private(p.frontmatter)]
    amap = alias_map(pages)
    graph = []
    for p in pages:
        fm = p.frontmatter
        kind = fm.get("kind")
        node: dict[str, Any] = {
            "@id": base + p.slug,
            "@type": JSONLD_TYPE.get(fm.get("entityType"), "Thing") if kind == "entity" else
            {"concept": "DefinedTerm", "overview": "Article", "comparison": "Article", "query": "Question"}.get(kind, "Thing"),
            "name": p.title, "description": fm.get("summary"), "inLanguage": fm.get("language"),
            "dateModified": str(fm.get("updated") or "") or None,
            "citation": sorted({str(s.get("key")) for s in fm.get("sources") or [] if isinstance(s, dict)}) or None,
            "mentions": [{"@id": base + amap[t]} for t in sorted(set(wikilink_targets(p.body))) if t in amap and amap[t] != p.slug] or None,
        }
        if p.aliases:
            node["alternateName"] = p.aliases
        if kind == "query" and fm.get("question"):
            node["text"] = fm["question"]
        graph.append({k: v for k, v in node.items() if v not in (None, "", [])})
    Path(out).write_text(json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=1), "utf-8")
    return {"out": str(Path(out).resolve()), "nodes": len(graph)}


def export_marp(vault: Path, note: str, out: str, *, theme: str = "default") -> dict[str, Any]:
    """A Marp slide deck from a page or a briefing: one slide
    per `##` section, [[links]] shown as their text, citations collected on a final sources slide."""
    import re

    from .core.paths import resolve_in_vault, validate_relpath
    from .markdown import CITATION, WIKILINK, split_frontmatter

    require_vault(vault)
    path = Path(note)
    if not path.is_file():
        path = resolve_in_vault(Path(vault), validate_relpath(note))
    fm, body = split_frontmatter(path.read_text("utf-8"))
    title = str((fm or {}).get("title") or path.stem)
    sources: list[str] = []

    def cite(m: re.Match) -> str:
        ref = f"{m.group(1)} righe {m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
        if ref not in sources:
            sources.append(ref)
        return f"<sup>{sources.index(ref) + 1}</sup>"

    text = CITATION.sub(cite, body)
    text = WIKILINK.sub(lambda m: (m.group(0)[2:-2].split("|", 1)[1] if "|" in m.group(0) else m.group(1)), text)
    text = re.sub(r"(?ms)^> \[!quote\][^\n]*\n(?:>.*\n?)*", "", text)  # original-language blocks
    text = re.sub(r"(?m)^# .*\n", "", text).strip()
    sections = re.split(r"(?m)^(?=## )", text)
    slides = [f"# {title}\n\n{str((fm or {}).get('summary') or '').strip()}"]
    slides += [s.strip() for s in sections if s.strip()]
    if sources:
        slides.append("## Fonti\n\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(sources, 1)))
    deck = f"---\nmarp: true\ntheme: {theme}\npaginate: true\n---\n\n" + "\n\n---\n\n".join(slides) + "\n"
    Path(out).write_text(deck, "utf-8")
    return {"out": str(Path(out).resolve()), "slides": len(slides), "sources": len(sources)}


def export_vault(vault: Path, out: str, project_id: str | None = None) -> dict[str, Any]:
    require_vault(vault)
    registry = load_sources(vault)["sources"]
    file_sha: dict[str, str | None] = {}

    def current(key: str) -> str | None:
        if key not in file_sha:
            name, _, rel = key.partition("/")
            entry = registry.get(name)
            path = resolve_root(vault, entry) / rel if entry and rel else None
            file_sha[key] = sha256_file(path) if path and path.is_file() else None
        return file_sha[key]

    pages = []
    for p in iter_pages(vault):
        if p.error:
            continue
        fm = p.frontmatter
        srcs = [s for s in fm.get("sources") or [] if isinstance(s, dict) and "key" in s]
        states = ["orphaned" if current(s["key"]) is None else "stale" if current(s["key"]) != s.get("sha256") else "fresh"
                  for s in srcs]
        freshness = "orphaned" if "orphaned" in states else "stale" if "stale" in states else "fresh" if states else "n/a"
        pages.append({
            "id": f"{project_id}:{p.slug}" if project_id else p.slug,
            "path": p.path, "slug": p.slug, "title": p.title, "kind": fm.get("kind") or fm.get("memoryKind"),
            "summary": fm.get("summary"), "aliases": p.aliases, "language": fm.get("language"),
            "confidence": fm.get("confidence"), "provenanceState": fm.get("provenanceState"),
            "contradictedBy": fm.get("contradictedBy") or [], "sources": srcs,
            "citations": [{"file": k, "start": a, "end": b} for k, a, b in citations(p.body)],
            "links": sorted(set(wikilink_targets(p.body))), "freshnessStatus": freshness,
            "sha256": p.sha256, "body": p.body,
        })
    envelope = {"schema": "mnemoloci.export.v1", "project_id": project_id,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pages": pages}
    Path(out).write_text(json.dumps(envelope, ensure_ascii=False, indent=1), "utf-8")
    return {"out": str(Path(out).resolve()), "pages": len(pages)}
