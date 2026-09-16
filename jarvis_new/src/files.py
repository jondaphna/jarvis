"""Your own documents, findable by voice.

"Where's that invoice?" and "what does my business plan say about pricing?"
are the same question asked two ways, so they are one tool: find the files
that match, then read the best one and quote the part that matters.

Three decisions worth stating.

**It is free and local.** No embedding API, no index to build, no service to
sign up for. Ranking is filename match, then how often your words appear in the
file, then how recently you touched it - which for "find the invoice from
March" is not meaningfully worse than a vector search, and costs nothing and
sends nothing anywhere.

**It only reads.** Finding a file on a spoken word is safe. Changing one is
not, and there is no write, move or delete in here to be talked into. Anything
that edits your files stays behind the permission system in the other half.

**It stays where you said.** Searching is confined to folders you named -
Documents, Desktop, Downloads and the JARVIS workspace by default. A path
climbing out of those with `..` is refused rather than normalised, because a
voice assistant that can be talked into reading your SSH keys is not a feature.

The excerpt it finds does go to the model, which is the entire point of asking
it a question about your document. Nothing else leaves the machine.
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Read as text without any help. Deliberately conservative: a file type that
#: turns out to be binary produces mojibake in the model's context, which is
#: worse than not finding it.
TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".log", ".py", ".js", ".ts", ".tsx",
    ".jsx", ".html", ".css", ".sql", ".sh", ".bat", ".ps1", ".xml", ".srt",
    ".vtt", ".tex", ".org",
})

#: Need a library, which may not be installed. Handled by saying so.
DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx"})

#: Folders nobody means when they say "my files". Skipping them is the
#: difference between finding your pricing notes and finding a copy of
#: left-pad that happens to contain the word.
SKIP_FOLDERS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".next", "dist",
    "build", ".cache", "site-packages", ".idea", ".vscode", "AppData",
    "Library", ".Trash", "$RECYCLE.BIN", "System Volume Information",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", "vendor",
})

#: Bigger than this and it is a database or a video, not a document.
MAX_BYTES = 8_000_000

#: How much of the best match to read back. Enough to answer a question about
#: a document, short enough not to bury the conversation.
EXCERPT_CHARS = 4000

#: How many files to name. More than this and it stops being an answer.
MAX_HITS = 5


#: The folders people mean by "my files" when they haven't said. Named here
#: rather than configured, because a tool you must set up before it works is a
#: tool that never gets used.
DEFAULT_FOLDER_NAMES = ("Documents", "Desktop", "Downloads", "JARVIS Workspace")


def default_roots() -> list[Path]:
    """Those folders, minus the ones this machine doesn't have."""
    home = Path.home()
    return [home / name for name in DEFAULT_FOLDER_NAMES if (home / name).is_dir()]


def readable(path: Path, max_bytes: int = MAX_BYTES) -> bool:
    """Is this a document worth opening at all?"""
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES and suffix not in DOCUMENT_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= max_bytes
    except OSError:
        return False


def extract_text(path: Path, limit: int = 400_000) -> str:
    """The words in a file, or "" when there aren't any to be had.

    PDFs and Word documents need a library that may not be installed. Rather
    than failing the whole search, they come back empty and the file is still
    findable by name - and the tool says plainly what it couldn't open.
    """
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return path.read_text("utf-8", errors="replace")[:limit]
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        try:
            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "")
                             for page in reader.pages)[:limit]
        except Exception:
            return ""
    if suffix == ".docx":
        try:
            import docx
        except ImportError:
            return ""
        try:
            return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)[:limit]
        except Exception:
            return ""
    return ""


def words_of(query: str) -> list[str]:
    """The meaningful words in what was asked, lowercased."""
    stop = {"the", "a", "an", "my", "in", "of", "on", "for", "to", "and",
            "what", "where", "is", "does", "say", "about", "find", "file",
            "files", "document", "documents", "me"}
    words = re.findall(r"[\w']+", (query or "").lower())
    kept = [w for w in words if len(w) > 1 and w not in stop]
    return kept or words


def excerpt_around(text: str, words: list[str], size: int = EXCERPT_CHARS) -> str:
    """The part of the document the question was actually about.

    The first N characters of a long file are almost never the answer, so this
    centres on the first place the words appear.
    """
    if len(text) <= size:
        return text.strip()
    lowered = text.lower()
    positions = [lowered.find(word) for word in words]
    positions = [p for p in positions if p >= 0]
    centre = min(positions) if positions else 0
    start = max(0, centre - size // 3)
    piece = text[start:start + size].strip()
    return ("..." + piece if start else piece) + ("..." if start + size < len(text) else "")


class FileTools:
    """Finding and reading your own documents. Read-only, local, free."""

    def roots(self) -> list[Path]:
        """Every folder this is allowed to look in."""
        try:
            from jarvis.config import Settings

            settings = Settings.load()
            named = [str(settings.get("autonomy.workspace", "") or "")]
            named += [str(p) for p in (settings.get("autonomy.extra_workspaces") or [])]
            named += [str(p) for p in (settings.get("files.folders") or [])]
        except Exception:
            named = []

        found = [Path(name).expanduser() for name in named if name.strip()]
        found = [path for path in found if path.is_dir()]
        for path in default_roots():
            if path not in found:
                found.append(path)
        return found

    def resolve_folder(self, folder: str) -> Path | None:
        """A folder to narrow the search to, checked against what is allowed.

        None means "everywhere you allowed". A path outside those is refused
        rather than quietly searched: the confinement is the point.
        """
        name = (folder or "").strip()
        if not name:
            return None
        roots = self.roots()
        candidate = Path(name).expanduser()
        if candidate.is_absolute():
            options = [candidate]
        else:
            # A name can mean a root itself ("Documents") or something inside
            # one ("Documents/invoices"). Both are things people say.
            options = [root for root in roots if root.name.lower() == name.lower()]
            options += [root / name for root in roots]
        for option in options:
            try:
                resolved = option.resolve()
            except OSError:
                continue
            if not resolved.is_dir():
                continue
            for root in roots:
                try:
                    resolved.relative_to(root.resolve())
                except ValueError:
                    continue
                return resolved
        raise ValueError(
            f"I'm not allowed to look in {name!r}. Add it in settings if you "
            f"want me to.")

    def walk(self, folder: Path | None) -> list[Path]:
        """Every document worth considering, under one folder or all of them."""
        roots = [folder] if folder is not None else self.roots()
        found: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if any(part in SKIP_FOLDERS or part.startswith(".")
                       for part in path.relative_to(root).parts[:-1]):
                    continue
                if path.is_file() and readable(path):
                    found.append(path)
                if len(found) > 20_000:          # a runaway folder tree
                    return found
        return found

    def score(self, path: Path, words: list[str]) -> tuple[float, str]:
        """How well this file answers the question, and its text."""
        name = path.name.lower()
        points = sum(8.0 for word in words if word in name)

        try:
            text = extract_text(path)
        except Exception:
            text = ""
        if text:
            lowered = text.lower()
            for word in words:
                hits = lowered.count(word)
                if hits:
                    # Diminishing returns: a file saying it twenty times is
                    # not twenty times better than one saying it twice.
                    points += min(hits, 10) * 1.0
            # Every word present beats one word many times.
            if all(word in lowered for word in words):
                points += 6.0

        if points:
            with contextlib.suppress(OSError):
                # A tie-break, not a factor: newer is usually the one meant.
                points += min(path.stat().st_mtime / 1e12, 2.0)
        return points, text

    @property
    def tools(self) -> list:
        return [self.search_my_files]

    @function_tool()
    async def search_my_files(self, context: RunContext, query: str,
                              folder: str = "") -> str:
        """Find something in the user's own files, and read it.

        Use this for anything about their documents: "where's the invoice from
        March", "what does my business plan say about pricing", "find the
        script I wrote about the wedding video", "which file has the client
        list in it".

        It searches by filename AND by what is inside the files, so a vague
        description works. It reads the best match back to you, so you can
        answer the question rather than only saying where the file is.

        It only reads - it never changes, moves or deletes anything.

        Not for web pages: use read_web_page for those.

        Args:
            query: What they're looking for, in their words.
            folder: Optional. Narrow it to one folder, like "Documents" or
                "Downloads", when they said where to look.
        """
        import asyncio

        cleaned = (query or "").strip()
        if not cleaned:
            raise ToolError("Look for what?")
        try:
            where = self.resolve_folder(folder)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        try:
            # Walking a disk blocks. Doing it on the event loop would stop the
            # audio for as long as the search takes, which sounds like a
            # dropped call.
            return await asyncio.to_thread(self._search, cleaned, where)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"I couldn't search your files: {exc}") from exc

    def _search(self, query: str, folder: Path | None) -> str:
        words = words_of(query)
        scored: list[tuple[float, Path, str]] = []
        unreadable: set[str] = set()

        for path in self.walk(folder):
            try:
                points, text = self.score(path, words)
            except Exception:
                continue
            if not text and path.suffix.lower() in DOCUMENT_SUFFIXES:
                unreadable.add(path.suffix.lower())
            if points > 0:
                scored.append((points, path, text))

        if not scored:
            where = f" in {folder.name}" if folder else ""
            note = ""
            if unreadable:
                kinds = " and ".join(sorted(s.lstrip(".") for s in unreadable))
                note = (f" I can see {kinds} files but can't read inside them "
                        f"on this machine, so I only matched their names.")
            return f"I found nothing about {query!r}{where}.{note}"

        scored.sort(key=lambda row: row[0], reverse=True)
        _, best_path, best_text = scored[0]

        lines = [f"Best match: {best_path.name} ({best_path})"]
        others = scored[1:MAX_HITS]
        if others:
            lines.append("Also: " + ", ".join(
                f"{path.name} ({path.parent})" for _, path, _ in others))
        if best_text.strip():
            lines.append(f"\nFrom {best_path.name}:\n"
                         + excerpt_around(best_text, words))
        elif best_path.suffix.lower() in DOCUMENT_SUFFIXES:
            lines.append(f"\nI matched the name but can't read inside a "
                         f"{best_path.suffix.lstrip('.')} on this machine.")
        return "\n".join(lines)
