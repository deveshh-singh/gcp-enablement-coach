"""Turn what a visitor gives us into checked input. Nothing here calls the model.

Files are read from memory and never written to disk.
"""
from __future__ import annotations

import html
import io
import ipaddress
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
MAX_MATERIAL_CHARS = 30_000
MIN_MATERIAL_CHARS = 200

PDF = "pdf"
TEXT = "text"


class TooLittleMaterial(Exception):
    """There isn't enough text to write questions from."""


class FileTooLarge(Exception):
    """The upload is over the size limit."""


class UnsupportedFile(Exception):
    """The file type isn't supported, or the file isn't what it claims to be."""


class BadLink(Exception):
    """The link isn't a public web address."""


def clean_material(text: str) -> str:
    """Tidy visitor text, cap its length and check there is enough to use."""
    cleaned = text.replace("\x00", "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) < MIN_MATERIAL_CHARS:
        raise TooLittleMaterial()
    return cleaned[:MAX_MATERIAL_CHARS]


def check_link(link: str) -> str:
    """Return a normalised web address, or refuse it.

    Gemini fetches the page, not this server, so a link can't reach anything
    private to this service. This check only turns away obvious mistakes.
    """
    link = link.strip()
    if "://" not in link:
        link = f"https://{link}"
    if len(link) > 2_000:
        raise BadLink()

    try:
        parts = urlsplit(link)
        host = parts.hostname or ""
    except ValueError:
        raise BadLink()

    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise BadLink()
    if "." not in host or host.endswith((".local", ".internal", ".localhost")):
        raise BadLink()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return link
    raise BadLink()


def kind_of_file(name: str, data: bytes) -> str:
    """Decide how to read an upload, checking the content as well as the name."""
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLarge()

    suffix = PurePosixPath(name or "").suffix.lower()
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise UnsupportedFile()
        return PDF
    if suffix in (".txt", ".md", ".docx"):
        return TEXT
    raise UnsupportedFile()


def text_from_file(name: str, data: bytes) -> str:
    """Return the text of a TXT, Markdown or Word file."""
    if PurePosixPath(name or "").suffix.lower() == ".docx":
        return _text_from_docx(data)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UnsupportedFile()


def _text_from_docx(data: bytes) -> str:
    """A .docx is a zip of XML files, and the body text is in word/document.xml.

    The text is picked out with a pattern rather than an XML parser, and the
    unpacked size is checked first, so a crafted file can't use up the memory.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise UnsupportedFile()
            with archive.open(info) as stream:
                xml = stream.read(MAX_DOCX_XML_BYTES + 1)
    except (zipfile.BadZipFile, KeyError, RuntimeError, OSError):
        raise UnsupportedFile()
    if len(xml) > MAX_DOCX_XML_BYTES:
        raise UnsupportedFile()

    document = xml.decode("utf-8", errors="ignore")
    pieces = []
    for match in re.finditer(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|(</w:p>)", document):
        pieces.append("\n" if match.group(2) else html.unescape(match.group(1)))
    return "".join(pieces)
