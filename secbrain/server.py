"""
SecBrain FastMCP vault server.

Run directly:
    VAULT_PATH=~/.secbrain/vault MCP_API_KEY=<key> python -m secbrain.server

Or as a module entry point via uvicorn.
"""

from __future__ import annotations

import hashlib
import html.parser
import json
import os
import re
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from secbrain import __version__ as _SERVER_VERSION

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Optional: chromadb for semantic search (falls back to substring search if absent)
try:
    import chromadb  # type: ignore
    from chromadb.utils import embedding_functions  # type: ignore
    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT = Path(os.environ.get("VAULT_PATH", Path.home() / ".secbrain" / "vault")).expanduser()
API_KEY = os.environ.get("MCP_API_KEY", "")
PORT = int(os.environ.get("MCP_PORT", "8765"))

# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "SecBrain",
    instructions=(
        "Vault memory server for Claude Code. "
        "Use vault_start_session at the beginning of every session, "
        "vault_end_session at the end, and log bugs/features/decisions as you discover them."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ensure_vault():
    """Create vault directory structure if it doesn't exist."""
    for sub in ["_system", "projects", "skills/global", "raw", "wiki"]:
        (VAULT / sub).mkdir(parents=True, exist_ok=True)
    log = VAULT / "_system" / "log.md"
    if not log.exists():
        log.write_text("# SecBrain Session Log\n\n")


class _HTMLStripper(html.parser.HTMLParser):
    """Minimal HTML -> plain text converter using only stdlib."""
    _SKIP = {"script", "style", "nav", "footer", "header", "aside"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._SKIP:
            self._skip_depth += 1
        if tag.lower() in {"br", "p", "div", "h1", "h2", "h3", "h4", "li"}:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Collapse excessive blank lines
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_to_text(html_content: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html_content)
    return stripper.get_text()


def _chroma_collection(name: str):
    """Return a ChromaDB collection for semantic search, or None if unavailable."""
    if not _CHROMA_AVAILABLE:
        return None
    db_path = str(VAULT / "_system" / "chroma")
    client = chromadb.PersistentClient(path=db_path)
    ef = embedding_functions.DefaultEmbeddingFunction()
    return client.get_or_create_collection(name=name, embedding_function=ef)


def _chroma_add(collection_name: str, doc_id: str, text: str, metadata: dict):
    """Add or update a document in a ChromaDB collection."""
    col = _chroma_collection(collection_name)
    if col is None:
        return
    try:
        col.upsert(ids=[doc_id], documents=[text[:8000]], metadatas=[metadata])
    except Exception:
        pass  # Semantic search is additive — never block on failure


def _chroma_search(collection_name: str, query: str, n: int = 5) -> list[dict]:
    """Search a ChromaDB collection; returns list of {id, text, metadata}."""
    col = _chroma_collection(collection_name)
    if col is None:
        return []
    try:
        results = col.query(query_texts=[query], n_results=min(n, col.count()))
        out = []
        for i, doc in enumerate(results["documents"][0]):
            out.append({
                "id": results["ids"][0][i],
                "text": doc,
                "metadata": results["metadatas"][0][i] if results.get("metadatas") else {},
            })
        return out
    except Exception:
        return []


def _project_dir(project: str) -> Path:
    pd = VAULT / "projects" / project
    pd.mkdir(parents=True, exist_ok=True)
    return pd


def _append(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def _db() -> sqlite3.Connection:
    db_path = VAULT / "_system" / "entities.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            file_path TEXT,
            importance INTEGER DEFAULT 3,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(project, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            from_entity TEXT NOT NULL,
            relation TEXT NOT NULL,
            to_entity TEXT NOT NULL,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def _upsert_entity(
    project: str,
    name: str,
    etype: str,
    description: str,
    file_path: str,
    importance: int,
):
    conn = _db()
    now = _ts()
    conn.execute("""
        INSERT INTO entities (project, name, type, description, file_path, importance, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, name) DO UPDATE SET
            description=excluded.description,
            file_path=excluded.file_path,
            importance=excluded.importance,
            updated_at=excluded.updated_at
    """, (project, name, etype, description, file_path, importance, now, now))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_start_session(project: str, cwd: str = "") -> str:
    """
    Call at the start of every Claude Code session.
    Returns a lightweight index — titles and counts only, NOT full content.
    Use vault_recall(project, query) to fetch specific items as needed.
    """
    _ensure_vault()
    pd = _project_dir(project)
    out = [f"# SecBrain Session — {project}\n**Started:** {_ts()}\n"]

    _append(VAULT / "_system" / "log.md",
            f"\n## Session Start [{_ts()}]\n**Project:** {project}\n**CWD:** {cwd}\n")

    # Version update notice (cached, non-blocking)
    update_notice = _check_version()
    if update_notice:
        out.append(f"> **{update_notice}**\n")

    # Scan prompt (first session only)
    overview = pd / "overview.md"
    if not overview.exists():
        out.append(
            "**No codebase index yet.** Call `vault_scan_project(project, cwd)` "
            "before anything else.\n"
        )
    else:
        # Just the first 5 lines of overview as a quick reminder
        lines = overview.read_text().splitlines()
        out.append("## Project\n" + "\n".join(lines[:5]) + "\n")

    # Inline the full index (titles only — lightweight)
    out.append(vault_get_index(project))

    # User model (short — behaviour preferences)
    user_model = pd / "user_model.md"
    if user_model.exists():
        lines = user_model.read_text().splitlines()
        out.append("## User Preferences\n" + "\n".join(lines[:15]) + "\n")

    out.append(
        "## How to use this vault\n"
        "- **Before working on anything**: `vault_recall(project, query)` "
        "— pulls bugs, decisions, skills relevant to your topic.\n"
        "- **Log as you go**: `vault_log_bug/feature/task/decision(project, ..., tags='ui,auth')`\n"
        "- **New skill solved**: `vault_save_skill(project, ...)`\n"
        "- **End of session**: `vault_end_session(project, summary)`\n"
    )

    return "\n".join(out)


@mcp.tool()
def vault_get_index(project: str) -> str:
    """
    Returns a compact table of contents for the project vault — titles and counts only.
    Use this to know what exists, then call vault_recall(project, query) for specifics.
    Much lighter than reading full vault content.
    """
    _ensure_vault()
    pd = _project_dir(project)
    sections = []

    def _extract_titles(filepath: Path, limit: int = 20) -> list[str]:
        """Pull ### heading titles from a markdown file."""
        if not filepath.exists():
            return []
        titles = []
        for line in filepath.read_text().splitlines():
            if line.startswith("### "):
                titles.append(line[4:].strip())
        return titles[-limit:]  # most recent first

    # Bugs
    bug_titles = _extract_titles(pd / "bugs.md")
    if bug_titles:
        sections.append(f"**Bugs ({len(bug_titles)}):**\n" +
                        "\n".join(f"  - {t}" for t in bug_titles))

    # Open tasks
    tasks_file = pd / "tasks.md"
    if tasks_file.exists():
        open_tasks, done_tasks = [], []
        for line in tasks_file.read_text().splitlines():
            if line.startswith("### "):
                title = line[4:].strip()
                if "[DONE]" in title:
                    done_tasks.append(title)
                else:
                    open_tasks.append(title)
        if open_tasks:
            sections.append(f"**Open Tasks ({len(open_tasks)}):**\n" +
                            "\n".join(f"  - {t}" for t in open_tasks[-10:]))
        if done_tasks:
            sections.append(f"**Completed Tasks:** {len(done_tasks)}")

    # Features
    feat_titles = _extract_titles(pd / "features.md")
    if feat_titles:
        sections.append(f"**Features ({len(feat_titles)}):**\n" +
                        "\n".join(f"  - {t}" for t in feat_titles[-10:]))

    # Decisions
    dec_titles = _extract_titles(pd / "decisions.md")
    if dec_titles:
        sections.append(f"**Decisions ({len(dec_titles)}):**\n" +
                        "\n".join(f"  - {t}" for t in dec_titles[-8:]))

    # Skills
    proj_idx = VAULT / "skills" / project / "index.md"
    glob_idx = VAULT / "skills" / "global" / "index.md"
    skill_lines = []
    if proj_idx.exists():
        skill_lines += [f"  [project] {l[2:].split('(')[0].strip()}"
                        for l in proj_idx.read_text().splitlines() if l.startswith("- ")]
    if glob_idx.exists():
        skill_lines += [f"  [global]  {l[2:].split('(')[0].strip()}"
                        for l in glob_idx.read_text().splitlines() if l.startswith("- ")]
    if skill_lines:
        sections.append(f"**Skills ({len(skill_lines)}):**\n" + "\n".join(skill_lines[-10:]))

    # Ingested docs count
    raw_idx = VAULT / "raw" / "index.md"
    if raw_idx.exists():
        doc_count = sum(1 for l in raw_idx.read_text().splitlines() if l.startswith("- "))
        if doc_count:
            sections.append(f"**Ingested Docs:** {doc_count} — use `vault_search_docs(project, query)`")

    if not sections:
        return "## Vault Index\n*(Empty — nothing logged yet)*\n"

    return "## Vault Index\n" + "\n\n".join(sections) + "\n"


@mcp.tool()
def vault_end_session(project: str, summary: str) -> str:
    """
    Call at the end of every Claude Code session with a summary of what was accomplished.
    """
    _ensure_vault()
    _append(
        VAULT / "_system" / "log.md",
        f"\n## Session End [{_ts()}]\n**Project:** {project}\n**Summary:** {summary}\n"
    )
    pd = _project_dir(project)
    _append(pd / "sessions.md", f"\n### [{_ts()}]\n{summary}\n")
    return f"Session logged for '{project}'."


# ---------------------------------------------------------------------------
# Logging tools
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_log_bug(
    project: str,
    title: str,
    description: str,
    severity: str = "medium",
    file_path: str = "",
    tags: str = "",
) -> str:
    """
    Log a bug or error discovered during the session.
    severity: low | medium | high | critical
    tags: comma-separated area tags e.g. 'ui,mobile,auth' — used for targeted recall
    """
    _ensure_vault()
    pd = _project_dir(project)
    entry = f"\n### [{_ts()}] [{severity.upper()}] {title}\n"
    if tags:
        entry += f"**Tags:** {tags}\n"
    entry += f"{description}\n"
    if file_path:
        entry += f"**File:** {file_path}\n"
    _append(pd / "bugs.md", entry)
    _upsert_entity(project, title, "bug", f"{tags} {description}"[:200], file_path,
                   _severity_score(severity))
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] BUG [{severity}] {project}: {title} [{tags}]\n")
    return f"Bug logged: {title}"


@mcp.tool()
def vault_log_feature(
    project: str,
    title: str,
    description: str,
    status: str = "implemented",
    tags: str = "",
) -> str:
    """
    Log a feature implemented or planned during the session.
    status: planned | in_progress | implemented
    tags: comma-separated area tags e.g. 'ui,payments,api'
    """
    _ensure_vault()
    pd = _project_dir(project)
    entry = f"\n### [{_ts()}] [{status.upper()}] {title}\n"
    if tags:
        entry += f"**Tags:** {tags}\n"
    entry += f"{description}\n"
    _append(pd / "features.md", entry)
    _upsert_entity(project, title, "feature", f"{tags} {description}"[:200], "", 3)
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] FEATURE [{status}] {project}: {title} [{tags}]\n")
    return f"Feature logged: {title}"


@mcp.tool()
def vault_log_decision(
    project: str,
    title: str,
    rationale: str,
    tags: str = "",
) -> str:
    """
    Log an architectural or design decision made during the session.
    tags: comma-separated area tags e.g. 'architecture,ui,database'
    """
    _ensure_vault()
    pd = _project_dir(project)
    entry = f"\n### [{_ts()}] {title}\n"
    if tags:
        entry += f"**Tags:** {tags}\n"
    entry += f"**Rationale:** {rationale}\n"
    _append(pd / "decisions.md", entry)
    _upsert_entity(project, title, "decision", f"{tags} {rationale}"[:200], "", 3)
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] DECISION {project}: {title} [{tags}]\n")
    return f"Decision logged: {title}"


# ---------------------------------------------------------------------------
# Recall / search
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_recall(project: str, query: str, tags: str = "") -> str:
    """
    Search vault for anything related to query across bugs, features, tasks, decisions, sessions,
    and ingested docs. Uses semantic search if ChromaDB is available.
    tags: optional comma-separated filter e.g. 'ui,mobile' — narrows results to matching tags.
    Example: vault_recall('myapp', 'layout', tags='ui') before working on UI changes.
    """
    _ensure_vault()
    pd = _project_dir(project)
    results = []

    # Semantic search over ingested docs (ChromaDB)
    semantic = _chroma_search(f"docs_{project}", query, n=3)
    if semantic:
        results.append("### Docs (semantic)")
        for hit in semantic:
            meta = hit["metadata"]
            src = meta.get("url") or meta.get("source", "")
            results.append(f"**{meta.get('title', 'Untitled')}** [{src}]\n{hit['text'][:400]}\n")

    # Tag pre-filter: if tags provided, only return entries that mention those tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()] if tags else []

    # Full-text search across project markdown files (cap per file to protect context)
    for fname in ["bugs.md", "features.md", "tasks.md", "decisions.md", "sessions.md", "overview.md"]:
        fpath = pd / fname
        if not fpath.exists():
            continue
        content = fpath.read_text()
        lower_content = content.lower()
        if query.lower() not in lower_content:
            continue
        # Tag filter: skip this file section if tags don't match
        if tag_list and not any(t in lower_content for t in tag_list):
            continue
        lines = content.splitlines()
        matching, in_section, section_buf = [], False, []
        for line in lines:
            if line.startswith("### "):
                # Save previous section if it matched
                if in_section and section_buf:
                    matching.extend(section_buf[:8])  # max 8 lines per entry
                section_buf = [line]
                title_lower = line.lower()
                query_match = query.lower() in title_lower
                tag_match = not tag_list or any(t in title_lower for t in tag_list)
                in_section = query_match or tag_match
            elif in_section:
                section_buf.append(line)
                # Also check tags in body
                if tag_list and any(t in line.lower() for t in tag_list):
                    in_section = True
            if len(matching) >= 20:
                break
        if in_section and section_buf:
            matching.extend(section_buf[:8])
        if matching:
            results.append(f"### From {fname}\n" + "\n".join(matching))

    # Entity graph search
    conn = _db()
    rows = conn.execute(
        "SELECT name, type, description FROM entities "
        "WHERE project=? AND (name LIKE ? OR description LIKE ?) "
        "ORDER BY importance DESC LIMIT 10",
        (project, f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    if rows:
        results.append("### Matched Entities")
        for name, etype, desc in rows:
            results.append(f"- **[{etype}]** {name}: {desc[:150] if desc else ''}")

    return "\n\n".join(results) if results else f"No results found for '{query}' in project '{project}'."


# ---------------------------------------------------------------------------
# Overview / entity graph
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_update_overview(project: str, overview: str) -> str:
    """
    Update the project overview. Called when you have a clearer understanding of the project.
    """
    _ensure_vault()
    pd = _project_dir(project)
    content = f"# {project} — Overview\n**Updated:** {_ts()}\n\n{overview}\n"
    (pd / "overview.md").write_text(content)
    _append(VAULT / "_system" / "log.md", f"- [{_ts()}] OVERVIEW updated: {project}\n")
    return f"Overview updated for '{project}'."


@mcp.tool()
def vault_link_entities(
    project: str,
    from_entity: str,
    relation: str,
    to_entity: str,
) -> str:
    """
    Create a relationship between two entities in the knowledge graph.
    e.g. vault_link_entities(project, "AuthBug", "causes", "LoginFailure")
    """
    _ensure_vault()
    conn = _db()
    conn.execute(
        "INSERT INTO relations (project, from_entity, relation, to_entity, created_at) VALUES (?, ?, ?, ?, ?)",
        (project, from_entity, relation, to_entity, _ts())
    )
    conn.commit()
    conn.close()
    return f"Linked: {from_entity} --[{relation}]--> {to_entity}"


# ---------------------------------------------------------------------------
# General read / write / list
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_read(path: str) -> str:
    """
    Read a file from the vault. path is relative to vault root.
    e.g. "projects/myapp/bugs.md" or "_system/log.md"
    """
    _ensure_vault()
    target = VAULT / path
    if not target.exists():
        return f"File not found: {path}"
    if not target.is_file():
        return f"Not a file: {path}"
    return target.read_text()


@mcp.tool()
def vault_write(path: str, content: str) -> str:
    """
    Write content to a vault file. path is relative to vault root.
    Creates parent directories as needed. Overwrites existing content.
    """
    _ensure_vault()
    target = VAULT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Written: {path}"


@mcp.tool()
def vault_append(path: str, content: str) -> str:
    """
    Append content to a vault file. path is relative to vault root.
    """
    _ensure_vault()
    target = VAULT / path
    _append(target, content)
    return f"Appended to: {path}"


@mcp.tool()
def vault_list(path: str = "") -> str:
    """
    List files and directories in a vault path. Defaults to vault root.
    """
    _ensure_vault()
    target = VAULT / path if path else VAULT
    if not target.exists():
        return f"Path not found: {path}"
    if target.is_file():
        return str(target)
    items = sorted(target.iterdir())
    lines = []
    for item in items:
        prefix = "📁 " if item.is_dir() else "📄 "
        lines.append(f"{prefix}{item.name}")
    return "\n".join(lines) if lines else "(empty)"


@mcp.tool()
def vault_list_projects() -> str:
    """
    List all projects registered in the vault.
    """
    _ensure_vault()
    projects_dir = VAULT / "projects"
    if not projects_dir.exists():
        return "No projects yet."
    projects = [p.name for p in sorted(projects_dir.iterdir()) if p.is_dir()]
    return "\n".join(projects) if projects else "No projects yet."


# ---------------------------------------------------------------------------
# Self-improvement tools
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_save_skill(
    project: str,
    title: str,
    problem: str,
    solution: str,
    code_example: str = "",
    tags: str = "",
    scope: str = "project",
) -> str:
    """
    Extract a reusable solution pattern from experience.
    scope: 'project' (project-specific) or 'global' (applies everywhere).
    Call after solving a complex bug or implementing a non-obvious pattern.
    Only save if: non-obvious, reusable, and proven to work.
    """
    _ensure_vault()
    folder = "global" if scope == "global" else project
    skills_dir = VAULT / "skills" / folder
    skills_dir.mkdir(parents=True, exist_ok=True)

    slug = title.lower().replace(" ", "-").replace("/", "-")[:50]
    skill_file = skills_dir / f"{slug}.md"

    if skill_file.exists():
        existing = skill_file.read_text()
        content = existing + f"\n\n---\n### Refined [{_ts()}]\n{solution}\n"
        if code_example:
            content += f"\n```\n{code_example}\n```\n"
    else:
        content = (
            f"# {title}\n"
            f"**Tags:** {tags}\n"
            f"**Scope:** {scope}\n"
            f"**Created:** {_ts()}\n\n"
            f"## Problem\n{problem}\n\n"
            f"## Solution\n{solution}\n"
        )
        if code_example:
            content += f"\n## Example\n```\n{code_example}\n```\n"

    skill_file.write_text(content)

    # Update skills index
    index = skills_dir / "index.md"
    _append(index, f"- [{title}]({slug}.md) -- {tags} [{_ts()}]\n")

    # Index in entity graph
    _upsert_entity(project, title, "skill", problem[:200], "", 4)

    # Index in ChromaDB for semantic recall
    _chroma_add(
        "skills",
        f"{scope}:{slug}",
        f"{title}\n{problem}\n{solution}",
        {"project": project, "scope": scope, "title": title, "tags": tags},
    )

    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] SKILL saved ({scope}) {project}: {title}\n")
    return f"Skill saved: {title}"


@mcp.tool()
def vault_recall_skill(query: str, project: str = "") -> str:
    """
    Find relevant learned skills before starting work on a problem.
    Searches both project-specific and global skills.
    Uses semantic search if ChromaDB is available.
    Call at the start of complex tasks to avoid re-solving known problems.
    """
    _ensure_vault()
    out = []

    # Semantic search via ChromaDB
    semantic = _chroma_search("skills", query, n=5)
    if semantic:
        out.append("### Matching Skills (semantic)\n")
        for hit in semantic:
            meta = hit["metadata"]
            scope = meta.get("scope", "?")
            proj = meta.get("project", "")
            out.append(f"**[{scope}]** {meta.get('title', hit['id'])}\n{hit['text'][:600]}\n")

    # Fallback / supplement: filesystem search
    if not out:
        search_paths = [str(VAULT / "skills" / "global")]
        if project:
            proj_path = str(VAULT / "skills" / project)
            if Path(proj_path).exists():
                search_paths.append(proj_path)

        for path in search_paths:
            if not Path(path).exists():
                continue
            try:
                r = subprocess.run(
                    ["grep", "-r", "-i", "-l", "--include=*.md", query, path],
                    capture_output=True, text=True, timeout=10,
                )
                for fpath in r.stdout.strip().splitlines():
                    p = Path(fpath)
                    if p.name == "index.md":
                        continue
                    content = p.read_text()
                    scope = "global" if "global" in fpath else project
                    out.append(f"### [{scope}] {p.stem}\n{content[:600]}\n")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                for md_file in Path(path).glob("*.md"):
                    if md_file.name == "index.md":
                        continue
                    content = md_file.read_text()
                    if query.lower() in content.lower():
                        scope = "global" if "global" in path else project
                        out.append(f"### [{scope}] {md_file.stem}\n{content[:600]}\n")

    # Entity graph supplement
    conn = _db()
    rows = conn.execute(
        "SELECT name, description FROM entities "
        "WHERE type='skill' AND (name LIKE ? OR description LIKE ?) "
        "ORDER BY importance DESC LIMIT 5",
        (f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    if rows and not out:
        out.append("### Matched Skills (graph)")
        for name, desc in rows:
            out.append(f"- **{name}**: {desc[:200] if desc else ''}")

    return "\n\n".join(out) if out else "No matching skills found. This may be a new pattern worth saving."


@mcp.tool()
def vault_improve_skill(
    project: str,
    skill_name: str,
    refinement: str,
    scope: str = "project",
) -> str:
    """
    Refine an existing skill after applying and improving it.
    Call when you apply a skill and discover a better approach or edge case.
    """
    _ensure_vault()
    folder = "global" if scope == "global" else project
    slug = skill_name.lower().replace(" ", "-")[:50]
    skill_file = VAULT / "skills" / folder / f"{slug}.md"

    if not skill_file.exists():
        return f"Skill not found: {skill_name}. Use vault_save_skill to create it."

    improvement = f"\n\n---\n### Improvement [{_ts()}]\n{refinement}\n"
    _append(skill_file, improvement)
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] SKILL improved: {skill_name}\n")
    return f"Skill improved: {skill_name}"


@mcp.tool()
def vault_update_user_model(project: str, observation: str) -> str:
    """
    Update the user model for this project with a new observation.
    Call when you notice a consistent pattern: preferred code style,
    common mistake types, tools they like, architectural preferences, etc.
    These observations personalize future sessions.
    """
    _ensure_vault()
    pd = _project_dir(project)
    user_model = pd / "user_model.md"
    if not user_model.exists():
        user_model.write_text(
            f"# User Model — {project}\n\n"
            "Observations about how this developer works.\n\n"
        )
    _append(user_model, f"\n- [{_ts()}] {observation}\n")
    return "User model updated."


# ---------------------------------------------------------------------------
# Health check (HTTP GET /)
# ---------------------------------------------------------------------------


def _severity_score(severity: str) -> int:
    return {"low": 2, "medium": 3, "high": 4, "critical": 5}.get(severity.lower(), 3)


def _check_version() -> str | None:
    """
    Check PyPI for a newer version of secbrain.
    Caches result for 24h in vault/_system/version_check.json.
    Returns an update notice string, or None if up to date / check failed.
    """
    cache_file = VAULT / "_system" / "version_check.json"
    try:
        # Use cached result if < 24h old
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            age_hours = (datetime.now(timezone.utc).timestamp() - cached.get("ts", 0)) / 3600
            if age_hours < 24:
                latest = cached.get("latest")
                if latest and latest != _SERVER_VERSION:
                    return (f"[SecBrain] Update available: v{_SERVER_VERSION} -> v{latest}. "
                            f"Run `secbrain update` to upgrade.")
                return None

        # Hit PyPI JSON API
        req = urllib.request.Request(
            "https://pypi.org/pypi/secbrain/json",
            headers={"User-Agent": f"secbrain/{_SERVER_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        latest = data["info"]["version"]

        # Cache result
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "latest": latest,
            "ts": datetime.now(timezone.utc).timestamp(),
        }))

        if latest != _SERVER_VERSION:
            return (f"[SecBrain] Update available: v{_SERVER_VERSION} -> v{latest}. "
                    f"Run `secbrain update` to upgrade.")
    except Exception:
        pass  # Never block session start on a version check
    return None


# ---------------------------------------------------------------------------
# Codebase scanning
# ---------------------------------------------------------------------------

# Files/dirs to skip when scanning a project
_SCAN_SKIP = {
    ".git", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".secbrain", ".firecrawl",
}
_SCAN_ENTRY_PATTERNS = {
    "package.json", "pyproject.toml", "setup.py", "cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "composer.json",
    "readme.md", "readme.rst", "readme.txt",
    "main.py", "app.py", "index.js", "index.ts", "main.ts",
    "main.go", "main.rs", "app.ts", "server.py", "server.ts",
}
_SCAN_MAX_FILE_BYTES = 8_000   # read at most 8KB per file for the summary
_SCAN_MAX_FILES = 30           # cap how many files we read in detail


@mcp.tool()
def vault_scan_project(project: str, cwd: str) -> str:
    """
    Scan the project directory and store a structured overview in the vault.
    Call this ONCE on first session when no overview exists.
    Returns a short confirmation — the full overview is stored for future sessions.
    Does NOT dump the codebase into context; stores it in the vault instead.
    """
    _ensure_vault()
    root = Path(cwd).expanduser().resolve()
    if not root.exists():
        return f"Directory not found: {cwd}"

    # --- Walk file tree ---
    all_files: list[Path] = []
    entry_files: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored dirs in-place
        dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP and not d.startswith(".")]
        for fname in filenames:
            fp = Path(dirpath) / fname
            all_files.append(fp)
            if fname.lower() in _SCAN_ENTRY_PATTERNS:
                entry_files.append(fp)

    # Build compact file tree (relative paths, skip binaries/heavy dirs)
    tree_lines = []
    for f in sorted(all_files)[:200]:
        rel = f.relative_to(root)
        tree_lines.append(str(rel))
    file_tree = "\n".join(tree_lines)

    # Read key entry-point files
    key_content_parts = []
    files_read = 0
    for fp in sorted(entry_files, key=lambda p: len(p.parts)):
        if files_read >= _SCAN_MAX_FILES:
            break
        try:
            content = fp.read_bytes()[:_SCAN_MAX_FILE_BYTES].decode("utf-8", errors="replace")
            rel = fp.relative_to(root)
            key_content_parts.append(f"### {rel}\n```\n{content}\n```")
            files_read += 1
        except Exception:
            pass

    # Detect stack from file patterns
    stack_hints = []
    names_lower = {f.name.lower() for f in all_files}
    if "package.json" in names_lower:
        stack_hints.append("Node.js/JavaScript")
    if any(n in names_lower for n in ["pyproject.toml", "setup.py", "requirements.txt"]):
        stack_hints.append("Python")
    if "cargo.toml" in names_lower:
        stack_hints.append("Rust")
    if "go.mod" in names_lower:
        stack_hints.append("Go")
    if any(n.endswith(".tsx") or n.endswith(".jsx") for n in names_lower):
        stack_hints.append("React")
    if any(n.endswith(".vue") for n in names_lower):
        stack_hints.append("Vue")
    if "next.config.js" in names_lower or "next.config.ts" in names_lower:
        stack_hints.append("Next.js")

    # Compose the overview document stored in vault
    overview_doc = (
        f"# {project} — Codebase Overview\n"
        f"**Scanned:** {_ts()}\n"
        f"**Root:** {root}\n"
        f"**Stack:** {', '.join(stack_hints) or 'unknown'}\n"
        f"**Total files:** {len(all_files)}\n\n"
        f"## File Tree\n```\n{file_tree}\n```\n\n"
        f"## Key Files\n" + "\n\n".join(key_content_parts)
    )

    pd = _project_dir(project)
    (pd / "overview.md").write_text(overview_doc[:40_000])  # cap stored doc at 40KB

    # Also store raw for search
    slug = f"scan-{project}"
    raw_file = VAULT / "raw" / f"{slug}.md"
    raw_file.write_text(overview_doc[:50_000])
    _append(VAULT / "raw" / "index.md",
            f"- [Codebase scan: {project}]({slug}.md) -- {root} [{_ts()}]\n")

    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] SCAN {project}: {len(all_files)} files, stack: {', '.join(stack_hints)}\n")

    return (
        f"Codebase indexed: {len(all_files)} files scanned, "
        f"{files_read} key files read. "
        f"Stack detected: {', '.join(stack_hints) or 'unknown'}. "
        f"Overview stored — will load on next vault_start_session."
    )


# ---------------------------------------------------------------------------
# Task tracking (todo/doing/review/done — borrowed from Archon pattern)
# ---------------------------------------------------------------------------

TASK_STATUSES = {"todo", "doing", "review", "done"}
TASK_ASSIGNEES = {"user", "ai", "both"}


@mcp.tool()
def vault_log_task(
    project: str,
    title: str,
    description: str,
    status: str = "todo",
    assignee: str = "ai",
) -> str:
    """
    Log a task with a status lifecycle.
    status: todo | doing | review | done
    assignee: user | ai | both
    Use this for concrete work items. Use vault_log_feature for completed capabilities.
    """
    _ensure_vault()
    pd = _project_dir(project)
    status = status.lower() if status.lower() in TASK_STATUSES else "todo"
    assignee = assignee.lower() if assignee.lower() in TASK_ASSIGNEES else "ai"

    entry = (
        f"\n### [{_ts()}] [{status.upper()}] [{assignee.upper()}] {title}\n"
        f"{description}\n"
    )
    _append(pd / "tasks.md", entry)
    _upsert_entity(project, title, "task", description[:200], "", 3)
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] TASK [{status}] {project}: {title}\n")
    return f"Task logged: {title} ({status})"


@mcp.tool()
def vault_update_task(
    project: str,
    title: str,
    new_status: str,
    note: str = "",
) -> str:
    """
    Update the status of an existing task.
    new_status: todo | doing | review | done
    Call when work on a task progresses or completes.
    """
    _ensure_vault()
    pd = _project_dir(project)
    tasks_file = pd / "tasks.md"
    if not tasks_file.exists():
        return f"No tasks file for project '{project}'."

    new_status = new_status.lower() if new_status.lower() in TASK_STATUSES else "todo"
    content = tasks_file.read_text()

    # Find the task section and append a status update note
    if title.lower() not in content.lower():
        return f"Task '{title}' not found. Use vault_log_task to create it first."

    update = f"\n  - **Status -> {new_status.upper()}** [{_ts()}]"
    if note:
        update += f": {note}"
    update += "\n"

    # Insert update after the task's heading line
    pattern = re.compile(
        r"(###[^\n]*" + re.escape(title) + r"[^\n]*\n)",
        re.IGNORECASE,
    )
    new_content = pattern.sub(r"\1" + update.replace("\\", "\\\\"), content, count=1)
    if new_content == content:
        # Fallback: just append
        _append(tasks_file, f"\n**{title}** -> {new_status.upper()} [{_ts()}]{': ' + note if note else ''}\n")
    else:
        tasks_file.write_text(new_content)

    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] TASK UPDATE [{new_status}] {project}: {title}\n")
    return f"Task updated: {title} -> {new_status}"


# ---------------------------------------------------------------------------
# Code example index (borrowed from Archon pattern)
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_log_code_example(
    project: str,
    description: str,
    code: str,
    language: str = "",
    source_file: str = "",
    tags: str = "",
) -> str:
    """
    Save a code example or snippet to the searchable code index.
    Call when you write or discover a non-obvious pattern worth saving.
    Different from vault_save_skill — this is for raw reusable code snippets.
    """
    _ensure_vault()
    pd = _project_dir(project)

    lang_tag = language or "python"
    entry = (
        f"\n### [{_ts()}] {description}\n"
        f"**Tags:** {tags}  **Language:** {lang_tag}\n"
    )
    if source_file:
        entry += f"**Source:** {source_file}\n"
    entry += f"\n```{lang_tag}\n{code}\n```\n"

    _append(pd / "code_examples.md", entry)

    # Index in ChromaDB if available for semantic search
    doc_id = hashlib.md5(f"{project}:{description}:{code[:100]}".encode()).hexdigest()
    _chroma_add(
        f"code_{project}",
        doc_id,
        f"{description}\n{code[:2000]}",
        {"project": project, "language": lang_tag, "tags": tags, "source": source_file},
    )

    _upsert_entity(project, description[:80], "code_example", description[:200], source_file, 3)
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] CODE EXAMPLE {project}: {description[:60]}\n")
    return f"Code example saved: {description[:60]}"


@mcp.tool()
def vault_search_code(project: str, query: str) -> str:
    """
    Search saved code examples for a project.
    Uses semantic search if ChromaDB is available, otherwise substring search.
    Call before implementing something — you may have already solved it.
    """
    _ensure_vault()
    pd = _project_dir(project)

    # Try semantic search first
    semantic = _chroma_search(f"code_{project}", query)
    if semantic:
        out = [f"### Code Examples matching '{query}' (semantic search)\n"]
        for hit in semantic:
            meta = hit["metadata"]
            out.append(
                f"**{meta.get('tags', '')}** [{meta.get('language', '')}]  "
                f"*{meta.get('source', '')}*\n"
                f"{hit['text'][:600]}\n"
            )
        return "\n".join(out)

    # Fallback: substring search in code_examples.md
    code_file = pd / "code_examples.md"
    if not code_file.exists():
        return "No code examples saved yet."

    content = code_file.read_text()
    if query.lower() not in content.lower():
        return f"No code examples matching '{query}'."

    lines = content.splitlines()
    results, in_block = [], False
    for line in lines:
        if line.startswith("###"):
            in_block = query.lower() in line.lower()
        if in_block:
            results.append(line)
        if len(results) > 40:
            break

    return "\n".join(results) if results else f"No code examples matching '{query}'."


# ---------------------------------------------------------------------------
# URL / file ingestion (Archon-inspired — no vector DB required)
# ---------------------------------------------------------------------------


@mcp.tool()
def vault_ingest_url(
    url: str,
    project: str,
    title: str = "",
    tags: str = "",
) -> str:
    """
    Fetch a URL and store its content in the vault for future recall.
    Useful for ingesting documentation pages, blog posts, API references.
    Content is searchable via vault_recall and vault_search_docs.
    No vector DB required — uses plain text storage.
    """
    _ensure_vault()

    # Fetch page
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SecBrain/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"Failed to fetch '{url}': {e}"

    # Convert to plain text
    text = _html_to_text(raw)
    if len(text) < 50:
        return f"Page at '{url}' returned too little content to store."

    # Derive title from <title> tag if not provided
    if not title:
        m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE)
        title = m.group(1).strip() if m else url.split("/")[-1] or url

    # Save to raw/ with a stable filename
    slug = hashlib.md5(url.encode()).hexdigest()[:12]
    raw_file = VAULT / "raw" / f"{slug}.md"
    header = f"# {title}\n**Source:** {url}\n**Tags:** {tags}\n**Ingested:** {_ts()}\n\n"
    raw_file.write_text(header + text[:50_000])  # cap at 50k chars

    # Update raw index
    index = VAULT / "raw" / "index.md"
    _append(index, f"- [{title}]({slug}.md) -- {url} [{_ts()}]\n")

    # Add to ChromaDB if available
    _chroma_add(
        f"docs_{project}",
        slug,
        f"{title}\n{text[:8000]}",
        {"project": project, "url": url, "title": title, "tags": tags},
    )

    _upsert_entity(project, title, "document", f"From {url}", str(raw_file), 3)
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] INGESTED URL {project}: {title} ({url})\n")
    return f"Ingested: '{title}' ({len(text):,} chars) -> raw/{slug}.md"


@mcp.tool()
def vault_ingest_file(
    file_path: str,
    project: str,
    title: str = "",
    tags: str = "",
) -> str:
    """
    Ingest a local file (markdown, txt, or PDF) into the vault.
    PDF support requires: pip install pypdf2
    """
    _ensure_vault()
    path = Path(file_path).expanduser()
    if not path.exists():
        return f"File not found: {file_path}"

    ext = path.suffix.lower()
    text = ""

    if ext in {".md", ".txt", ".rst"}:
        text = path.read_text(encoding="utf-8", errors="replace")
    elif ext == ".pdf":
        try:
            import PyPDF2  # type: ignore
            reader = PyPDF2.PdfReader(str(path))
            text = "\n\n".join(
                p.extract_text() or "" for p in reader.pages
            )
        except ImportError:
            return "PDF support requires: pip install PyPDF2"
        except Exception as e:
            return f"Failed to read PDF: {e}"
    else:
        return f"Unsupported file type: {ext}. Supported: .md .txt .rst .pdf"

    if len(text.strip()) < 20:
        return f"File appears empty: {file_path}"

    if not title:
        title = path.stem.replace("-", " ").replace("_", " ").title()

    slug = hashlib.md5(str(path.resolve()).encode()).hexdigest()[:12]
    raw_file = VAULT / "raw" / f"{slug}.md"
    header = f"# {title}\n**Source:** {file_path}\n**Tags:** {tags}\n**Ingested:** {_ts()}\n\n"
    raw_file.write_text(header + text[:50_000])

    index = VAULT / "raw" / "index.md"
    _append(index, f"- [{title}]({slug}.md) -- {file_path} [{_ts()}]\n")

    _chroma_add(
        f"docs_{project}",
        slug,
        f"{title}\n{text[:8000]}",
        {"project": project, "source": file_path, "title": title, "tags": tags},
    )

    _upsert_entity(project, title, "document", f"From {file_path}", str(raw_file), 3)
    _append(VAULT / "_system" / "log.md",
            f"- [{_ts()}] INGESTED FILE {project}: {title}\n")
    return f"Ingested: '{title}' ({len(text):,} chars) -> raw/{slug}.md"


@mcp.tool()
def vault_search_docs(project: str, query: str) -> str:
    """
    Search ingested documentation (URLs and files) for a project.
    Uses semantic search if ChromaDB is available, otherwise full-text search.
    """
    _ensure_vault()

    # Semantic search first
    semantic = _chroma_search(f"docs_{project}", query)
    if semantic:
        out = [f"### Documentation matching '{query}' (semantic)\n"]
        for hit in semantic:
            meta = hit["metadata"]
            out.append(
                f"**{meta.get('title', 'Untitled')}**  "
                f"[{meta.get('url') or meta.get('source', '')}]\n"
                f"{hit['text'][:500]}\n"
            )
        return "\n".join(out)

    # Fallback: search raw/ files
    raw_dir = VAULT / "raw"
    if not raw_dir.exists():
        return "No documents ingested yet. Use vault_ingest_url or vault_ingest_file."

    results = []
    for md_file in sorted(raw_dir.glob("*.md")):
        if md_file.name == "index.md":
            continue
        content = md_file.read_text(errors="replace")
        if query.lower() in content.lower():
            # Return first 600 chars of the file as a snippet
            results.append(f"### {md_file.stem}\n{content[:600]}\n")
        if len(results) >= 5:
            break

    return "\n\n".join(results) if results else f"No documents matching '{query}'."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the correct Bearer token."""

    async def dispatch(self, request: Request, call_next):
        # Skip auth if no key is configured (dev/local mode)
        if not API_KEY:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {API_KEY}":
            return await call_next(request)
        return JSONResponse(
            {"error": "Unauthorized — invalid or missing API key"},
            status_code=401,
        )


def run_server():
    """Start the MCP server. Called by CLI or systemd."""
    _ensure_vault()
    middleware = [Middleware(_BearerAuthMiddleware)] if API_KEY else None
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=PORT,
        middleware=middleware,
    )


if __name__ == "__main__":
    run_server()
