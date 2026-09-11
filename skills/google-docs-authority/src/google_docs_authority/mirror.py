"""Incremental Google Docs mirrors with caller-owned sources, credentials and storage.

Both native Markdown and HTML rendering preserve stable document identities,
tab layouts, attachment hashes and existing synchronization state. Publication
is delegated to the configured caller; this module does not commit or push.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml

from google_docs_authority import config
from google_docs_authority.fingerprint import fingerprint
from google_docs_authority.mirror_status import (
    TrashedDocument,
    failure_details,
    http_body,
    load_status,
    read_get,
    record_result,
    retryable,
)

# Configuration is loaded before any filesystem or network operation. Imports
# remain inert, including when callers inspect the pure rendering functions.
OUT_REPO = OUTPUT_DIR = STATE_FILE = CACHE_LINK = DEFAULT_CACHE_TARGET = None
SOURCES_YAML = DISCOVERED_YAML = TOKEN_FILE = None
STATUS_FILE = None
CACHE_LINK_CONFIGURED = False
MASK_ENABLED = True
MASK_TIERS = {"hard", "ctx"}
REDACT_COMMAND = []
ALLOW_UNAUTHENTICATED = False
OFFLINE = False
IMAGE_SHRINK_FLOOR = 0.7
ALLOW_IMAGE_SHRINK = ALLOW_NO_PIL = False
PANDOC_MEM_MAX = None
PANDOC_TIMEOUT_S = 300
PANDOC_COMMAND = ["pandoc"]
DEFAULT_README_HEADER = (
    "<!-- Synced from Google Docs by google-docs-authority/scripts/sync.\n"
    "     Do not edit by hand — edit the source doc and re-sync. -->\n\n"
)
README_HEADER = DEFAULT_README_HEADER
_token_cache = {"token": None, "exp": 0.0}


def configure(settings, state_override=None, config_path=None):
    """Apply a validated mirror profile without touching its paths."""
    global OUT_REPO, OUTPUT_DIR, STATE_FILE, CACHE_LINK, DEFAULT_CACHE_TARGET
    global SOURCES_YAML, DISCOVERED_YAML, TOKEN_FILE, CACHE_LINK_CONFIGURED
    global MASK_ENABLED, MASK_TIERS, REDACT_COMMAND, ALLOW_UNAUTHENTICATED
    global IMAGE_SHRINK_FLOOR, ALLOW_IMAGE_SHRINK, ALLOW_NO_PIL
    global PANDOC_MEM_MAX, PANDOC_TIMEOUT_S, PANDOC_COMMAND, README_HEADER
    global STATUS_FILE
    section = settings["mirror"]
    OUT_REPO = section["repository_root"]
    OUTPUT_DIR = section["output_directory"]
    SOURCES_YAML = section["source_list"]
    DISCOVERED_YAML = section.get("discovered_list")
    STATE_FILE = (
        Path(state_override).expanduser().resolve()
        if state_override
        else section["state_file"]
    )
    if STATE_FILE.is_relative_to(OUT_REPO):
        raise ValueError("mirror-state-path-inside-repository")
    if STATE_FILE in {
        Path(config_path).expanduser().resolve() if config_path else None,
        SOURCES_YAML,
        DISCOVERED_YAML,
        settings.get("read_token_file"),
        settings.get("write_token_file"),
    }:
        raise ValueError("mirror-state-path-conflict")
    STATUS_FILE = section.get("status_file")
    if STATUS_FILE == STATE_FILE:
        raise ValueError("mirror-status-path-conflict")
    DEFAULT_CACHE_TARGET = section["cache_directory"]
    CACHE_LINK_CONFIGURED = bool(section.get("cache_link"))
    CACHE_LINK = section.get("cache_link") or DEFAULT_CACHE_TARGET
    TOKEN_FILE = settings.get("read_token_file")
    MASK_ENABLED = section.get("redact_enabled", True)
    MASK_TIERS = set(section.get("mask_tiers", ["hard", "ctx"]))
    REDACT_COMMAND = section.get("redact_command", [])
    ALLOW_UNAUTHENTICATED = section.get("allow_unauthenticated", False)
    IMAGE_SHRINK_FLOOR = section.get("image_shrink_floor", 0.7)
    ALLOW_IMAGE_SHRINK = section.get("allow_image_shrink", False)
    ALLOW_NO_PIL = section.get("allow_no_pillow", False)
    PANDOC_MEM_MAX = section.get("pandoc_memory_max")
    PANDOC_TIMEOUT_S = section.get("pandoc_timeout", 300)
    PANDOC_COMMAND = section.get("pandoc_command", ["pandoc"])
    README_HEADER = section.get("readme_header", DEFAULT_README_HEADER)
    _token_cache.update(token=None, exp=0.0)


def redact_secrets(md: str, tiers: set | None = None) -> str:
    """Use the configured stdin/JSON redaction interface; failures stop sync."""
    if not MASK_ENABLED:
        return md
    use = tiers if tiers is not None else MASK_TIERS
    if not use or not use <= {"hard", "ctx", "heur"} or not REDACT_COMMAND:
        raise ValueError("mirror-redaction-config-invalid")
    command = [
        part.replace("@tiers@", ",".join(sorted(use))) for part in REDACT_COMMAND
    ]
    result = subprocess.run(
        command, input=md, text=True, capture_output=True, timeout=120
    )
    if result.returncode:
        raise ValueError("mirror-redaction-command-failed")
    try:
        record = json.loads(result.stdout)
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("text"), str)
            or type(record.get("replacements")) is not int
            or record["replacements"] < 0
        ):
            raise ValueError("invalid record")
    except (ValueError, TypeError):
        raise ValueError("mirror-redaction-response-invalid") from None
    if record["replacements"]:
        print(f"  NOTE redacted {record['replacements']} secret span(s)")
    return record["text"]


def get_access_token() -> str | None:
    """Refresh an explicitly selected token; never silently ignore bad auth."""
    from google_docs_authority.oauth import refresh_access_token

    if TOKEN_FILE is None:
        if ALLOW_UNAUTHENTICATED:
            return None
        raise ValueError("mirror-read-token-required")
    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"] - 60:
        return _token_cache["token"]
    token = refresh_access_token(TOKEN_FILE)
    _token_cache.update(token=token, exp=now + 300)
    return token


def open_request(request, **kwargs):
    """Do not forward credentials through HTTP redirects."""
    from google_docs_authority.oauth import open_request as request_opener

    return request_opener(request, **kwargs)


def read_request(request, *, timeout):
    return read_get(request, opener=open_request, timeout=timeout)


def checked_component(value):
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("mirror-path-component-invalid")
    return value


def document_path(directory, relative):
    """Reject paths escaping their document, including existing symlinks."""
    candidate = directory / relative
    if not candidate.resolve().is_relative_to(directory.resolve()):
        raise ValueError("mirror-document-path-outside-root")
    return candidate


def output_directory(slug):
    target = document_path(OUTPUT_DIR, checked_component(slug))
    if target.is_symlink():
        raise ValueError("mirror-document-directory-symlink")
    return target


def rename_doc_dir(old: str, new: str) -> None:
    checked_component(old)
    checked_component(new)
    output_directory(old).rename(output_directory(new))
    old_cache, new_cache = cache_directory(old), cache_directory(new)
    if old_cache.exists() and not new_cache.exists():
        old_cache.rename(new_cache)
    print("  NOTE document directory renamed to follow its upstream title")


def doc_mask_tiers(doc: dict) -> set | None:
    raw = doc.get("mask_tier")
    if not raw:
        return None
    return {t.strip() for t in str(raw).split(",") if t.strip()}


EXPORT_URL = "https://docs.google.com/document/d/{doc_id}/export?format=html"
SOURCE_URL = "https://docs.google.com/document/d/{doc_id}/edit"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3/files/"


def drive_meta(doc_id: str, fields: str) -> dict | None:
    token = get_access_token()
    if not token:
        return None
    url = f"{DRIVE_API}{doc_id}?fields={fields}&supportsAllDrives=true"
    data = json.loads(
        read_request(
            Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=60
        )
    )
    if not isinstance(data, dict):
        raise ValueError("mirror-drive-metadata-invalid")
    return data


def google_asset_url(url):
    """Export links and image URLs must remain on HTTPS Google asset hosts."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    allowed = host in {
        "docs.google.com",
        "drive.google.com",
        "www.googleapis.com",
    } or host.endswith(".googleusercontent.com")
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("mirror-google-asset-url-invalid")
    return url


class GoogleAssetRedirect(HTTPRedirectHandler):
    """Validate every asset redirect and keep credentials on their original host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        google_asset_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        # Allowed URLs are HTTPS on port 443, so only the host can change origin.
        if urlparse(req.full_url).hostname != urlparse(newurl).hostname:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


def open_google_asset_get(url, *, headers, timeout):
    """Follow bounded, validated redirects only for explicitly selected asset GETs."""
    request = Request(google_asset_url(url), headers=headers, method="GET")
    return build_opener(GoogleAssetRedirect()).open(request, timeout=timeout)


def fetch_export_authenticated(doc_id: str, mime: str) -> bytes | None:
    """Fall back only for unsupported formats, never for transport failures."""
    token = get_access_token()
    if not token:
        return None
    url = f"{DRIVE_API}{doc_id}/export?mimeType={quote(mime, safe='')}"
    try:
        return read_request(
            Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=180
        )
    except HTTPError as error:
        body = http_body(error)
        if error.code == 403 and b"exportSizeLimitExceeded" in body:
            metadata = drive_meta(doc_id, "exportLinks")
            link = ((metadata or {}).get("exportLinks") or {}).get(mime)
            if not link:
                return None

            def asset_opener(request, *, timeout):
                return open_google_asset_get(
                    request.full_url,
                    headers=dict(request.header_items()),
                    timeout=timeout,
                )

            return read_get(
                Request(
                    link,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": BROWSER_UA,
                    },
                ),
                opener=asset_opener,
                timeout=300,
            )
        if error.code == 400 and mime == "text/markdown":
            return None
        raise


def fetch_html_authenticated(doc_id: str) -> bytes | None:
    return fetch_export_authenticated(doc_id, "text/html")


TITLE_RE = re.compile(
    b'<p class="[^"]*title[^"]*"[^>]*id="(h\\.[a-z0-9]+)"[^>]*>(.*?)</p>', re.DOTALL
)
EMPTY_P_RE = re.compile(
    b"<p\\b[^>]*>(?:\\s|&nbsp;|<br\\s*/?>|<span\\b[^>]*>\\s*(?:&nbsp;|\\s)*</span>)*</p>",
    re.DOTALL,
)
SPAN_CLASS_RE = re.compile('<span class="c\\d+(?:\\s+c\\d+)*">(.*?)</span>', re.DOTALL)
SPAN_STYLE_RE = re.compile('<span style="[^"]*">(.*?)</span>', re.DOTALL)
TAG_CLASS_ATTR_RE = re.compile('(\\s)class="([^"]*)"')
BLANK_RUN_RE = re.compile("\\n{3,}")
GOOGLE_URL_RE = re.compile(
    "https?://www\\.google\\.com/url\\?q=([^&\\s\\)]+)(?:&[a-zA-Z0-9_]+=[^\\s\\)]*)*"
)
URL_TRACKER_RE = re.compile('(?:&|&amp;)(?:sa|source|ust|usg)=[^&"\\s\\)]*')
IMG_REF_RE = re.compile(
    "!\\[[^\\]]*\\]\\(attachments/([a-f0-9]+)\\.([a-zA-Z0-9]{1,5})\\)"
)
ANCHOR_SLUG_RE = re.compile("[^a-z0-9]+")


def ensure_cache_link() -> None:
    """Create only the configured cache link; preserve other existing paths."""
    if not CACHE_LINK_CONFIGURED:
        DEFAULT_CACHE_TARGET.mkdir(parents=True, exist_ok=True)
        return
    if CACHE_LINK.is_symlink():
        if CACHE_LINK.resolve() != DEFAULT_CACHE_TARGET.resolve():
            raise ValueError("mirror-cache-link-target-mismatch")
        DEFAULT_CACHE_TARGET.mkdir(parents=True, exist_ok=True)
        return
    if CACHE_LINK.exists():
        raise ValueError("mirror-cache-link-path-occupied")
    DEFAULT_CACHE_TARGET.mkdir(parents=True, exist_ok=True)
    CACHE_LINK.parent.mkdir(parents=True, exist_ok=True)
    CACHE_LINK.symlink_to(DEFAULT_CACHE_TARGET)


def write_private_cache(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".export-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def cache_directory(slug):
    target = document_path(DEFAULT_CACHE_TARGET, checked_component(slug))
    if target.is_symlink():
        raise ValueError("mirror-cache-directory-symlink")
    return target


def html_cache_path(slug: str) -> Path:
    return document_path(cache_directory(slug), "last-export.html")


def fetch_html(doc_id: str, slug: str | None = None, from_cache: bool = False) -> bytes:
    cache_path = html_cache_path(slug) if slug else None
    if from_cache:
        if not cache_path or not cache_path.is_file():
            raise ValueError("mirror-html-cache-missing")
        return cache_path.read_bytes()
    html = fetch_html_authenticated(doc_id)
    if html is None:
        if not ALLOW_UNAUTHENTICATED:
            raise ValueError("mirror-html-export-unavailable")
        url = EXPORT_URL.format(doc_id=doc_id)
        req = Request(url, headers={"User-Agent": BROWSER_UA})
        html = read_request(req, timeout=180)
    if not is_valid_doc_html(html):
        raise ValueError("mirror-html-export-invalid")
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_private_cache(cache_path, html)
    return html


def is_valid_doc_html(html: bytes) -> bool:
    return bool(TITLE_RE.search(html))


def extract_doc_title(html: bytes) -> str | None:
    m = re.search(b"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).decode("utf-8", errors="replace").strip()
        title = re.sub("\\s*-\\s*Google Docs\\s*$", "", title).strip()
        if title and title.lower() != "error":
            return title
    m = TITLE_RE.search(html)
    if m:
        text = (
            re.sub(b"<[^>]+>", b"", m.group(2))
            .decode("utf-8", errors="replace")
            .strip()
        )
        if text:
            return text
    return None


DOC_ID_PREFIX_LEN = 8


def derive_slug(title: str | None, doc_id: str, taken: set[str]) -> str:
    slug = doc_dirname(title or "untitled", doc_id)
    if slug in taken:
        n = 2
        while f"{slug}-{n}" in taken:
            n += 1
        slug = f"{slug}-{n}"
    taken.add(slug)
    return slug


def slugify_anchor(text: str, taken: set[str]) -> str:
    base = ANCHOR_SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    taken.add(slug)
    return slug


def preprocess_html(html: bytes) -> tuple[bytes, list[dict]]:
    """Turn exported tab-title paragraphs into stable headings and remove visual spacers."""
    tabs: list[dict] = []
    taken: set[str] = set()

    def repl(m: re.Match) -> bytes:
        inner = m.group(2)
        text = re.sub(b"<[^>]+>", b"", inner).decode("utf-8", errors="replace").strip()
        anchor = slugify_anchor(text, taken)
        tabs.append({"anchor": anchor, "title": text})
        return f'<h1 id="{anchor}">'.encode() + inner + b"</h1>"

    rewritten = TITLE_RE.sub(repl, html)
    while True:
        new = EMPTY_P_RE.sub(b"", rewritten)
        if new == rewritten:
            break
        rewritten = new
    return (rewritten, tabs)


def postprocess_md(md: str) -> str:
    """Remove volatile Google classes, tracker parameters and spacing from HTML exports."""

    def unwrap_url(m: re.Match) -> str:
        return unquote(m.group(1))

    md = GOOGLE_URL_RE.sub(unwrap_url, md)
    md = URL_TRACKER_RE.sub("", md)
    while True:
        new = SPAN_CLASS_RE.sub("\\1", md)
        if new == md:
            break
        md = new
    C_TOKEN_RE = re.compile("^c\\d+$")

    def strip_c_class_tokens(m: re.Match) -> str:
        leading = m.group(1)
        tokens = m.group(2).split()
        keep = [t for t in tokens if not C_TOKEN_RE.match(t)]
        if not keep:
            return ""
        return leading + 'class="' + " ".join(keep) + '"'

    md = TAG_CLASS_ATTR_RE.sub(strip_c_class_tokens, md)
    while True:
        new = SPAN_STYLE_RE.sub("\\1", md)
        if new == md:
            break
        md = new
    md = BLANK_RUN_RE.sub("\n\n", md)
    return md


def parse_images_from_md(md: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for m in IMG_REF_RE.finditer(md):
        sha1, ext = (m.group(1), m.group(2).lower())
        if sha1 in seen:
            continue
        seen.add(sha1)
        out.append({"sha1": sha1, "ext": ext})
    return out


def cache_dir_for(slug: str) -> Path:
    return output_directory(slug) / "attachments"


def all_images_cached(slug: str, images: list[dict]) -> bool:
    """Attachment presence includes LFS pointers; bytes are checked before image decoding."""
    cache_dir = cache_dir_for(slug)
    return all(((cache_dir / f"{img['sha1']}.{img['ext']}").exists() for img in images))


def ensure_attachments_dir(doc_dir: Path, slug: str) -> None:
    link = doc_dir / "attachments"
    if link.is_symlink():
        link.unlink()
    link.mkdir(parents=True, exist_ok=True)


def prune_unreferenced_images(slug: str, images: list[dict]) -> int:
    """Keep only attachments referenced by the accepted manifest."""
    keep = {f"{img['sha1']}.{img['ext']}" for img in images}
    removed = 0
    adir = cache_dir_for(slug)
    if adir.is_dir():
        for f in adir.iterdir():
            if f.is_file() and f.name not in keep:
                f.unlink()
                removed += 1
    return removed


def _guarded_pandoc_cmd(pandoc_argv: list) -> tuple:
    """Apply the caller-selected memory limit when a user systemd bus is available."""
    env = dict(os.environ)
    uid = os.getuid()
    bus = f"/run/user/{uid}/bus"
    if PANDOC_MEM_MAX and shutil.which("systemd-run") and os.path.exists(bus):
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
        return (
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "-p",
                f"MemoryMax={PANDOC_MEM_MAX}",
                "-p",
                "MemorySwapMax=0",
                "--",
                "timeout",
                str(PANDOC_TIMEOUT_S),
            ]
            + pandoc_argv,
            env,
        )
    return (pandoc_argv, env)


def run_pandoc(html_bytes: bytes, doc_dir: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_html = Path(tmp) / "input.html"
        tmp_md = Path(tmp) / "output.md"
        tmp_html.write_bytes(html_bytes)
        pandoc_argv = PANDOC_COMMAND + [
            "--from=html",
            "--to=gfm",
            "--wrap=none",
            "--extract-media=attachments",
            str(tmp_html),
            "-o",
            str(tmp_md),
        ]
        cmd, env = _guarded_pandoc_cmd(pandoc_argv)
        result = subprocess.run(
            cmd,
            cwd=doc_dir,
            capture_output=True,
            text=True,
            timeout=PANDOC_TIMEOUT_S + 30,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError("mirror-pandoc-failed")
        return tmp_md.read_text(encoding="utf-8")


def write_if_different(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    config.atomic_write(path, content)
    return True


MD_EXT_MAP = {"jpeg": "jpg", "svg+xml": "svg", "x-emf": "emf", "tiff": "tif"}
MD_DATA_URI_RE = re.compile("<?data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)>?")
MD_H1_RE = re.compile("^# +(.+?)\\s*$")
IMG_DEF_RE = re.compile("^\\[([^\\]]+)\\]:\\s*(attachments/\\S+)\\s*$", re.M)
DOCS_API = "https://docs.googleapis.com/v1/documents/"


def title_slug(title: str, max_len: int = 60) -> str:
    import unicodedata

    t = unicodedata.normalize("NFC", str(title or "")).strip()
    t = "".join((c if c.isalnum() else " " for c in t))
    t = re.sub("\\s+", "-", t).strip("-")
    t = "".join((c.lower() if c.isascii() else c for c in t))
    return t[:max_len].rstrip("-") or "untitled"


def doc_dirname(title: str, doc_id: str) -> str:
    return f"{title_slug(title)}--{doc_id[:DOC_ID_PREFIX_LEN]}"


def tab_basename(title: str, tab_id: str) -> str:
    checked_component(tab_id)
    return f"{title_slug(title)}--{re.sub('^t[.]', '', tab_id)}"


def md_cache_path(slug: str) -> Path:
    return document_path(cache_directory(slug), "last-export.md")


def fetch_markdown(doc_id: str, slug: str, from_cache: bool = False) -> bytes | None:
    """Read a cached native export offline or download and privately cache a fresh export."""
    cache_path = md_cache_path(slug)
    if from_cache:
        if not cache_path.is_file():
            return None
        return cache_path.read_bytes()
    raw = fetch_export_authenticated(doc_id, "text/markdown")
    if raw is None:
        return None
    if not raw.strip() or raw.lstrip()[:1] == b"<":
        print(
            "  [WARN] markdown export looks wrong (empty or HTML-shaped); falling back to the HTML engine",
            file=sys.stderr,
        )
        return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    write_private_cache(cache_path, raw)
    return raw


def validated_tabs(data, require_titles=False):
    """Reject partial or malformed metadata instead of deleting an existing layout."""
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("tabs"), list)
        or not data["tabs"]
    ):
        raise ValueError("mirror-tabs-response-invalid")
    if require_titles and not isinstance(data.get("title"), str):
        raise ValueError("mirror-document-title-missing")
    seen = set()

    def validate(items):
        if not isinstance(items, list):
            raise ValueError("mirror-tabs-container-invalid")
        for tab in items:
            if not isinstance(tab, dict) or not isinstance(
                tab.get("tabProperties"), dict
            ):
                raise ValueError("mirror-tab-properties-missing")
            props = tab["tabProperties"]
            identity = checked_component(props.get("tabId"))
            if identity in seen:
                raise ValueError("mirror-tab-identity-duplicate")
            seen.add(identity)
            if require_titles and not isinstance(props.get("title"), str):
                raise ValueError("mirror-tab-title-missing")
            validate(tab.get("childTabs", []))

    validate(data["tabs"])
    return data["tabs"]


def fetch_tab_tree(doc_id: str) -> tuple[str | None, list[dict]] | None:
    """Load ordered tab identities without downloading the document body."""
    token = get_access_token()
    if not token:
        return None
    props = "tabProperties(tabId,title)"
    mask = props
    for _ in range(4):
        mask = f"{props},childTabs({mask})"
    url = f"{DOCS_API}{doc_id}?includeTabsContent=true&fields=title,tabs({mask})"
    try:
        data = json.loads(
            read_request(
                Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=120
            )
        )
    except (OSError, ValueError):
        raise ValueError("mirror-tab-tree-fetch-failed") from None

    def walk(tabs: list) -> list[dict]:
        out = []
        for t in tabs or []:
            tp = t.get("tabProperties", {})
            out.append(
                {
                    "id": tp.get("tabId") or "",
                    "title": tp.get("title") or "untitled",
                    "children": walk(t.get("childTabs", [])),
                }
            )
        return out

    tabs = validated_tabs(data, require_titles=True)
    return (data["title"], walk(tabs))


def flatten_tabs(tabs: list[dict], parent: str = "", depth: int = 0) -> list[dict]:
    """A parent tab owns a directory with its content in README.md; leaf tabs use files."""
    flat = []
    for t in tabs:
        base = tab_basename(t["title"], t["id"])
        if t["children"]:
            path = f"{parent}{base}/README.md"
            flat.append({**t, "path": path, "depth": depth})
            flat += flatten_tabs(t["children"], f"{parent}{base}/", depth + 1)
        else:
            flat.append({**t, "path": f"{parent}{base}.md", "depth": depth})
    return flat


LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"


def is_lfs_pointer(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(LFS_POINTER_MAGIC)) == LFS_POINTER_MAGIC
    except OSError:
        return False


def real_image_bytes(path: Path) -> bool:
    return path.is_file() and (not is_lfs_pointer(path))


LFS_SIZE_RE = re.compile(b"^size (\\d+)$", re.M)


def attachment_bytes(path: Path) -> int:
    """Measure original object size through an LFS pointer without downloading its bytes."""
    try:
        if is_lfs_pointer(path):
            m = LFS_SIZE_RE.search(path.read_bytes())
            return int(m.group(1)) if m else 0
        return path.stat().st_size
    except OSError:
        return 0


def pil_available() -> bool:
    try:
        import PIL

        return bool(PIL.__version__)
    except ImportError:
        return False


def image_downgrade_verdict(
    old_images: list[dict], new_images: list[dict], doc_dir: Path
) -> str | None:
    """Refuse a large byte reduction when the old and new image counts are comparable."""
    if not old_images or not new_images or ALLOW_IMAGE_SHRINK:
        return None
    if abs(len(new_images) - len(old_images)) > max(1, 0.1 * len(old_images)):
        return None
    adir = doc_dir / "attachments"
    old_b = sum(
        (attachment_bytes(adir / f"{i['sha1']}.{i['ext']}") for i in old_images)
    )
    new_b = sum(
        (attachment_bytes(adir / f"{i['sha1']}.{i['ext']}") for i in new_images)
    )
    if old_b and new_b and (new_b < old_b * IMAGE_SHRINK_FLOOR):
        return f"image set shrank {old_b / 1048576:.1f}MB -> {new_b / 1048576:.1f}MB ({len(old_images)} -> {len(new_images)} images) below the {IMAGE_SHRINK_FLOOR:.0%} floor. This is what a silently failed original-resolution match looks like (check for LFS-POINTER-SKIPPED above; 'git lfs pull' this doc, then retry). Set mirror.allow_image_shrink=true if the images really did shrink upstream."
    return None


def selftest() -> int:
    import tempfile

    fails = 0

    def check(name: str, ok: bool) -> None:
        nonlocal fails
        if not ok:
            fails += 1
        print(f"  {('OK  ' if ok else 'FAIL')} {name}")

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IHDR" + b"\x00" * 100
    pointer = (
        b"version https://git-lfs.github.com/spec/v1\noid sha256:"
        + b"a" * 64
        + b"\nsize 123456\n"
    )
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "attachments").mkdir()
        real, ptr = (d / "attachments" / "r.png", d / "attachments" / "p.png")
        real.write_bytes(png)
        ptr.write_bytes(pointer)
        check("pointer file detected as pointer", is_lfs_pointer(ptr))
        check("real png not flagged as pointer", not is_lfs_pointer(real))
        check("real_image_bytes true for real png", real_image_bytes(real))
        check("real_image_bytes false for pointer", not real_image_bytes(ptr))
        check(
            "attachment_bytes reads size from pointer", attachment_bytes(ptr) == 123456
        )
        check("attachment_bytes stats real file", attachment_bytes(real) == len(png))
        big = [{"sha1": f"{i:040x}", "ext": "png"} for i in range(3)]
        small = [{"sha1": f"{i + 100:040x}", "ext": "png"} for i in range(3)]
        for i in big:
            (d / "attachments" / f"{i['sha1']}.png").write_bytes(b"\x00" * 300000)
        for i in small:
            (d / "attachments" / f"{i['sha1']}.png").write_bytes(b"\x00" * 80000)
        check(
            "downgrade gate fires on same-count shrink",
            image_downgrade_verdict(big, small, d) is not None,
        )
        check(
            "gate silent when the set grows back",
            image_downgrade_verdict(small, big, d) is None,
        )
        check(
            "gate silent on an unrelated small set",
            image_downgrade_verdict([], small, d) is None,
        )
    check(
        "churn gate matches image-encoding-only churn",
        render_matches_previous(
            "词 ![](attachments/aaa.png) continues\n",
            "词 ![](attachments/bbb.png) continues\n",
        ),
    )
    check(
        "churn gate matches html-engine img/style churn",
        render_matches_previous(
            '<img src="attachments/a.png" style="width:10">\n\nwords stay\n',
            '<img src="attachments/b.png" style="width:20">\n\nwords stay\n',
        ),
    )
    check(
        "churn gate does not match a real word change",
        not render_matches_previous("the words one\n", "the words two\n"),
    )
    check(
        "churn gate never matches a first render",
        not render_matches_previous(None, "anything\n"),
    )
    print(
        f"{('FAIL' if fails else 'OK')} Google Docs mirror self-test: {fails} failure(s)"
    )
    return 1 if fails else 0


def extract_md_images(md: str, slug: str) -> tuple[str, list[dict]]:
    """Decode inline images into content-addressed attachments, replacing LFS placeholders."""
    cache_dir = cache_dir_for(slug)
    cache_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    images: list[dict] = []

    def repl(m: re.Match) -> str:
        mime_sub, b64 = (m.group(1).lower(), m.group(2))
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            return m.group(0)
        sha1 = hashlib.sha1(data).hexdigest()
        existing = sorted(cache_dir.glob(f"{sha1}.*"))
        if existing and real_image_bytes(existing[0]):
            ext = existing[0].suffix.lstrip(".")
        elif existing:
            ext = existing[0].suffix.lstrip(".")
            existing[0].write_bytes(data)
        else:
            ext = MD_EXT_MAP.get(mime_sub, mime_sub)
            (cache_dir / f"{sha1}.{ext}").write_bytes(data)
        if sha1 not in seen:
            seen.add(sha1)
            images.append({"sha1": sha1, "ext": ext})
        return f"attachments/{sha1}.{ext}"

    return (MD_DATA_URI_RE.sub(repl, md), images)


def inline_image_refs(md: str) -> str:
    """Inline reference-style images so tab splitting cannot separate definitions."""
    defs = dict(IMG_DEF_RE.findall(md))
    if not defs:
        return md
    md = IMG_DEF_RE.sub("", md)

    def repl(m: re.Match) -> str:
        path = defs.get(m.group(2))
        return f"![{m.group(1)}]({path})" if path else m.group(0)

    return re.sub("!\\[([^\\]]*)\\]\\[([^\\]]+)\\]", repl, md)


def restore_paragraphs(md: str) -> str:
    """Keep hard breaks inside code and list blocks; normalize real paragraph endings."""
    list_re = re.compile("\\s*(?:[-*+]\\s|\\d+[.)]\\s|>)")
    lines = md.split("\n")
    out: list[str] = []
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if not fence and line.endswith("  ") and line.strip():
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip():
                out.append(line.rstrip() if list_re.match(line) else line)
            else:
                out.append(line.rstrip())
        else:
            out.append(line)
    return "\n".join(out)


CODE_ONLY_LINE_RE = re.compile("^[ \\t]*`([^`]*)`[ \\t]*$")
CODE_ESCAPE_RE = re.compile("\\\\([!-/:-@\\[-`{-~])")


def fuse_code_line_runs(md: str) -> str:
    """Fuse consecutive code-only inline spans into a single literal fenced block."""
    lines = md.split("\n")
    out: list[str] = []
    fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            i += 1
            continue
        if not fence and CODE_ONLY_LINE_RE.match(line):
            run: list[str] = []
            j = i
            while j < len(lines):
                m = CODE_ONLY_LINE_RE.match(lines[j])
                if not m:
                    break
                run.append(CODE_ESCAPE_RE.sub("\\1", m.group(1)))
                j += 1
            if len(run) >= 2:
                out.append("```")
                out.extend(run)
                out.append("```")
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def normalize_export_spacing(md: str) -> str:
    """Remove Google export spacer paragraphs, retaining literal entities in code."""
    if "&nbsp;" not in md:
        return md
    out: list[str] = []
    fence = None
    for line in md.splitlines(keepends=True):
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence is not None:
            out.append(line)
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
            continue
        if marker:
            fence = marker[1]
            out.append(line)
            continue
        if re.fullmatch(r" {0,3}(?:&nbsp;[ \t]*)+\r?\n?", line):
            line = "\n"
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return "".join(out)


def postprocess_native_md(md: str) -> str:
    md = normalize_export_spacing(md)
    md = GOOGLE_URL_RE.sub(lambda m: unquote(m.group(1)), md)
    md = URL_TRACKER_RE.sub("", md)
    md = inline_image_refs(md)
    md = fuse_code_line_runs(md)
    md = restore_paragraphs(md)
    md = BLANK_RUN_RE.sub("\n\n", md)
    return md


def _unescape_md(text: str) -> str:
    return re.sub("\\\\(.)", "\\1", text).strip()


def split_md_by_tabs(md: str, flat: list[dict]) -> tuple[str, list[str]] | None:
    """Match tab titles in order while preserving internal headings and fenced content."""
    lines = md.split("\n")
    bounds: list[int] = []
    expect = 0
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence or expect >= len(flat):
            continue
        m = MD_H1_RE.match(line)
        if m and _unescape_md(m.group(1)) == flat[expect]["title"].strip():
            bounds.append(i)
            expect += 1
    if expect != len(flat):
        return None
    preamble = "\n".join(lines[: bounds[0]]).strip()
    chunks = []
    for k, b in enumerate(bounds):
        end = bounds[k + 1] if k + 1 < len(bounds) else len(lines)
        chunks.append("\n".join(lines[b:end]).strip() + "\n")
    return (preamble, chunks)


def depth_adjust_images(content: str, depth: int) -> str:
    if depth <= 0:
        return content
    return content.replace("](attachments/", "](" + "../" * depth + "attachments/")


def render_index(title: str, source_url: str, flat: list[dict], preamble: str) -> str:
    out = [
        README_HEADER + f"# {title}",
        "",
        f"Source: [{source_url}]({source_url}) (mirror mode — the Google Doc is authoritative; one file per tab below)",
        "",
    ]
    if preamble:
        out += [preamble, ""]
    out.append("Tabs:")
    out += [f"{'    ' * t['depth']}- [{t['title']}]({t['path']})" for t in flat]
    return "\n".join(out) + "\n"


def render_matches_previous(old_body: str | None, new_full: str) -> bool:
    """Compare canonical words to avoid changing images solely because exports re-encoded them."""
    if old_body is None:
        return False
    return fingerprint(normalize_export_spacing(old_body)) == fingerprint(
        normalize_export_spacing(new_full)
    )


def previous_content(doc_dir: Path, manifest_path: Path) -> str | None:
    """Reconstruct prior content in manifest tab order for semantic comparison."""
    if not manifest_path.exists():
        return None
    try:
        old_m = yaml.safe_load(manifest_path.read_text()) or {}
    except Exception:
        return None
    if old_m.get("layout") == "tabs":
        parts = []
        for t in old_m.get("tabs") or []:
            p = document_path(doc_dir, t["path"])
            if p.exists():
                parts.append(
                    re.sub(
                        "\\A<!--.*?-->\\s*",
                        "",
                        p.read_text(encoding="utf-8"),
                        flags=re.DOTALL,
                    )
                )
        return "\n".join(parts) if parts else None
    readme = doc_dir / "README.md"
    if readme.exists():
        return re.sub(
            "\\A<!--.*?-->\\s*", "", readme.read_text(encoding="utf-8"), flags=re.DOTALL
        )
    return None


def cleanup_stale_tabs(doc_dir: Path, manifest_path: Path, keep: set[str]) -> None:
    """Remove only prior tab files that the newly accepted manifest no longer references."""
    if not manifest_path.exists():
        return
    try:
        old_m = yaml.safe_load(manifest_path.read_text()) or {}
    except Exception:
        return
    for t in old_m.get("tabs") or []:
        rel = t.get("path")
        if rel and rel not in keep:
            p = document_path(doc_dir, rel)
            if p.is_file():
                p.unlink()
    for sub in sorted(doc_dir.rglob("*"), reverse=True):
        if sub.is_symlink():
            continue
        if sub.is_dir() and (not any(sub.iterdir())):
            sub.rmdir()


def fetch_inline_object_uris(doc_id: str) -> list[str] | None:
    """Read original-image references from every nested tab without downloading body text."""
    token = get_access_token()
    if not token:
        return None
    props = "tabProperties(tabId),documentTab(inlineObjects)"
    mask = props
    for _ in range(4):
        mask = f"{props},childTabs({mask})"
    url = f"{DOCS_API}{doc_id}?includeTabsContent=true&fields=tabs({mask})"
    try:
        data = json.loads(
            read_request(
                Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=180
            )
        )
    except (OSError, ValueError):
        raise ValueError("mirror-image-metadata-fetch-failed") from None
    uris: list[str] = []

    def walk(tabs: list) -> None:
        for tab in tabs:
            body = tab.get("documentTab", {})
            if not isinstance(body, dict):
                raise ValueError("mirror-tab-body-invalid")
            objects = body.get("inlineObjects", {})
            if not isinstance(objects, dict):
                raise ValueError("mirror-inline-objects-invalid")
            for obj in objects.values():
                if not isinstance(obj, dict) or not isinstance(
                    obj.get("inlineObjectProperties"), dict
                ):
                    raise ValueError("mirror-inline-object-invalid")
                embedded = obj["inlineObjectProperties"].get("embeddedObject", {})
                if not isinstance(embedded, dict):
                    raise ValueError("mirror-embedded-object-invalid")
                if "imageProperties" not in embedded:
                    continue
                image = embedded["imageProperties"]
                if (
                    not isinstance(image, dict)
                    or not isinstance(image.get("contentUri"), str)
                    or not image["contentUri"]
                ):
                    raise ValueError("mirror-original-image-uri-missing")
                uris.append(google_asset_url(image["contentUri"]))
            walk(tab.get("childTabs", []))

    walk(validated_tabs(data))
    return uris


def _img_dims(data: bytes):
    import struct

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 255:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (192, 193, 194, 195):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return (w, h)
            i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return None


def upgrade_images_to_originals(
    doc_id: str, md: str, images: list[dict], slug: str
) -> tuple[str, list[dict], str]:
    """Upgrade only byte-identical or unambiguous pixel-matched images to larger originals."""
    if not images:
        return (md, images, "no images")
    uris = [] if OFFLINE else fetch_inline_object_uris(doc_id)
    if uris is None:
        return (md, images, "originals unavailable (kept export images)")
    token = None if OFFLINE else get_access_token()
    cache_dir = cache_dir_for(slug)
    originals: list[dict] = []
    seen_orig: set[str] = set()
    cached_originals = sorted(cache_dir.iterdir()) if OFFLINE else []
    for uri in cached_originals or uris:
        data = (
            uri.read_bytes()
            if isinstance(uri, Path) and real_image_bytes(uri)
            else None
        )
        if isinstance(uri, Path):
            headers_options = []
        else:
            google_asset_url(uri)
            headers_options = [{"User-Agent": BROWSER_UA}] + (
                [{"Authorization": f"Bearer {token}", "User-Agent": BROWSER_UA}]
                if token
                else []
            )
        last_error = None
        for headers in headers_options:
            try:
                data = read_request(Request(uri, headers=headers), timeout=120)
                break
            except Exception as error:
                last_error = error
                # Only a rejected anonymous request benefits from credentials.
                if (
                    not isinstance(error, HTTPError)
                    or error.code not in {401, 403}
                    or retryable(error)
                ):
                    break
                continue
        if not data:
            if isinstance(uri, Path):
                continue
            raise ValueError("mirror-original-image-fetch-failed") from last_error
        sha1 = hashlib.sha1(data).hexdigest()
        if sha1 in seen_orig:
            continue
        seen_orig.add(sha1)
        existing = sorted(cache_dir.glob(f"{sha1}.*"))
        if existing and real_image_bytes(existing[0]):
            path = existing[0]
        elif existing:
            path = existing[0]
            path.write_bytes(data)
        else:
            kind = (
                "png"
                if data[:8] == b"\x89PNG\r\n\x1a\n"
                else "jpg"
                if data[:2] == b"\xff\xd8"
                else "gif"
                if data[:3] == b"GIF"
                else "img"
            )
            path = cache_dir / f"{sha1}.{kind}"
            path.write_bytes(data)
        originals.append(
            {
                "sha1": sha1,
                "ext": path.suffix.lstrip("."),
                "dims": _img_dims(data),
                "path": path,
            }
        )
    orig_shas = {o["sha1"] for o in originals}
    identical = sum((1 for img in images if img["sha1"] in orig_shas))
    try:
        from PIL import Image

        have_pil = True
    except ImportError:
        have_pil = False
    mapping: dict[str, dict] = {}
    ambiguous = 0
    pointer_srcs = 0
    if have_pil:
        from PIL import ImageChops, ImageStat

        def load(p):
            return Image.open(p).convert("RGB")

        pil_originals = []
        for o in originals:
            try:
                im = load(o["path"])
                pil_originals.append((o, im.size, im))
            except Exception:
                continue
        for img in images:
            if img["sha1"] in orig_shas:
                continue
            src = sorted(cache_dir.glob(f"{img['sha1']}.*"))
            if not src:
                continue
            if not real_image_bytes(src[0]):
                pointer_srcs += 1
                continue
            try:
                exp = load(src[0])
            except Exception:
                continue
            ew, eh = exp.size
            scores = []
            for o, (ow, oh), im in pil_originals:
                if ow < ew or oh < eh:
                    continue
                if abs(ow / oh - ew / eh) / (ew / eh) > 0.02:
                    continue
                small = im.resize((ew, eh), Image.LANCZOS)
                diff = ImageStat.Stat(ImageChops.difference(small, exp)).mean
                scores.append((sum(diff) / len(diff), o))
            scores.sort(key=lambda x: x[0])
            if (
                scores
                and scores[0][0] <= 18
                and (
                    len(scores) == 1
                    or scores[1][0] > scores[0][0] + 8
                    or scores[1][1]["sha1"] == scores[0][1]["sha1"]
                )
            ):
                mapping[img["sha1"]] = scores[0][1]
            elif scores and scores[0][0] <= 18:
                ambiguous += 1
    for esha, o in mapping.items():
        src = sorted(cache_dir.glob(f"{esha}.*"))
        old_ref = f"attachments/{esha}.{src[0].suffix.lstrip('.')}" if src else None
        if old_ref:
            md = md.replace(old_ref, f"attachments/{o['sha1']}.{o['ext']}")
    final: list[dict] = []
    seen_final: set[str] = set()
    for img in images:
        o = mapping.get(img["sha1"])
        rec = {"sha1": o["sha1"], "ext": o["ext"]} if o else img
        if rec["sha1"] not in seen_final:
            seen_final.add(rec["sha1"])
            final.append(rec)
    kept = len(images) - identical - len(mapping)
    stats = (
        f"{identical} already-original, {len(mapping)} upgraded, {kept} kept-export"
        + (f", {ambiguous} ambiguous" if ambiguous else "")
        + (
            f", {pointer_srcs} LFS-POINTER-SKIPPED (run: git lfs pull)"
            if pointer_srcs
            else ""
        )
        + ("" if have_pil else " (Pillow missing: byte-equal upgrades only)")
    )
    return (md, final, stats)


def sync_doc_markdown(
    doc: dict,
    state: dict,
    force: bool,
    from_cache: bool,
    drive_ver: str | None,
    drive_name: str | None,
    docdirs: dict[str, str],
) -> str | None:
    """Render native exports, preserve image quality, split tabs and update in-memory state."""
    doc_id = doc["id"]
    checked_component(doc_id)
    checked_component(doc["slug"])
    current = docdirs.get(doc_id)
    title_live = drive_name or doc.get("title") or current or doc["slug"]
    expected = doc_dirname(title_live, doc_id) if drive_name or not current else current
    if current and expected != current:
        rename_doc_dir(current, expected)
    docdirs[doc_id] = expected
    outdir = expected
    raw = fetch_markdown(doc_id, outdir, from_cache=from_cache)
    if raw is None:
        return None
    if from_cache:
        print(f"  using cached markdown at {md_cache_path(outdir)}")
    md_sha1 = hashlib.sha1(raw).hexdigest()
    print(f"  markdown: {len(raw):,} bytes, sha1={md_sha1[:12]}")
    doc_dir = output_directory(outdir)
    doc_dir.mkdir(parents=True, exist_ok=True)
    readme_path = doc_dir / "README.md"
    manifest_path = doc_dir / "manifest.yaml"
    prev = state.get(doc_id, {})
    state_unchanged = prev.get("mdSha1") == md_sha1
    have_md = readme_path.exists()
    have_manifest = manifest_path.exists()
    old_manifest = {}
    if have_manifest:
        try:
            old_manifest = yaml.safe_load(manifest_path.read_text()) or {}
        except Exception:
            old_manifest = {}
    images_intact = all_images_cached(outdir, old_manifest.get("images") or [])
    tabs_intact = all(
        (
            document_path(doc_dir, t["path"]).exists()
            for t in old_manifest.get("tabs") or []
            if isinstance(t, dict) and t.get("path")
        )
    )
    cache_dir_for(outdir).mkdir(parents=True, exist_ok=True)
    ensure_attachments_dir(doc_dir, outdir)
    need_md = force or not state_unchanged or (not have_md) or (not have_manifest)
    if not need_md and images_intact and tabs_intact:
        print("  unchanged — skipped")
        return "unchanged"
    old_body = previous_content(doc_dir, manifest_path)
    md, images = extract_md_images(raw.decode("utf-8", errors="replace"), outdir)
    md, images, img_stats = upgrade_images_to_originals(doc_id, md, images, outdir)
    if images:
        print(f"  NOTE image originals: {img_stats}")
    md = postprocess_native_md(md)
    md = redact_secrets(md, doc_mask_tiers(doc))
    tree = None if OFFLINE else fetch_tab_tree(doc_id)
    title = title_live
    flat: list[dict] = (
        [
            {**t, "depth": t["path"].count("/")}
            for t in old_manifest.get("tabs", [])
            if t.get("path")
        ]
        if OFFLINE
        else []
    )
    if tree:
        api_title, tabs = tree
        title = api_title or title
        flat = flatten_tabs(tabs)
    split = None
    if len(flat) >= 2:
        split = split_md_by_tabs(md, flat)
        if split is None:
            print(
                f"  [WARN] tab titles did not match the export H1 sequence ({len(flat)} tabs); keeping single-file layout",
                file=sys.stderr,
            )
    new_full = "\n".join(split[1]) if split else md
    fp_same = render_matches_previous(old_body, new_full)
    if (
        fp_same
        and (not force)
        and have_md
        and have_manifest
        and images_intact
        and tabs_intact
    ):
        print(
            "  NOTE content identical to previous render (fingerprint match) — version-only churn; nothing written, state advanced"
        )
        rolled = prune_unreferenced_images(outdir, old_manifest.get("images") or [])
        if rolled:
            print(
                f"  NOTE rolled back {rolled} attachment file(s) from the skipped render"
            )
        new_state = {
            "slug": outdir,
            "engine": "markdown",
            "mdSha1": md_sha1,
            "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "imageCount": len(old_manifest.get("images") or []),
        }
        if drive_ver:
            new_state["driveVersion"] = drive_ver
        state[doc_id] = new_state
        return "unchanged"
    if len(md) < 200:
        print(f"  [WARN] render suspiciously small ({len(md)} chars)", file=sys.stderr)
    if "data:image" in md:
        prune_unreferenced_images(outdir, old_manifest.get("images") or [])
        raise ValueError("mirror-inline-image-decode-failed")

    def refuse(reason: str) -> str:
        print(f"  [FAIL] refusing to write {outdir}: {reason}", file=sys.stderr)
        rolled = prune_unreferenced_images(outdir, old_manifest.get("images") or [])
        if rolled:
            print(
                f"  NOTE rolled back {rolled} attachment file(s) written by this refused render",
                file=sys.stderr,
            )
        return "error"

    verdict = image_downgrade_verdict(old_manifest.get("images") or [], images, doc_dir)
    if verdict:
        return refuse(verdict)
    if images and (not pil_available()) and (not ALLOW_NO_PIL):
        return refuse(
            f"{len(images)} images but Pillow is missing, so originals cannot be matched and this render would mirror the export's downsampled copies. Use an interpreter with PIL (with Pillow installed) or set mirror.allow_no_pillow=true to accept export-resolution images."
        )
    layout = "single"
    written: list[dict] = []
    if split is not None:
        layout = "tabs"
        preamble, chunks = split
        for t, chunk in zip(flat, chunks):
            p = document_path(doc_dir, t["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            content = depth_adjust_images(chunk, t["path"].count("/"))
            write_if_different(p, README_HEADER + content)
            written.append({"id": t["id"], "title": t["title"], "path": t["path"]})
        cleanup_stale_tabs(doc_dir, manifest_path, {t["path"] for t in flat})
        readme_changed = write_if_different(
            readme_path,
            render_index(title, SOURCE_URL.format(doc_id=doc_id), flat, preamble),
        )
    if layout == "single":
        readme_changed = write_if_different(readme_path, README_HEADER + md)
        cleanup_stale_tabs(doc_dir, manifest_path, set())
    if old_body is not None:
        print(
            f"  NOTE content fingerprint vs previous render: {('identical' if fp_same else 'DIFFERS')}"
        )
        if old_manifest and (
            old_manifest.get("tabCount") != len(flat)
            or old_manifest.get("imageCount") != len(images)
        ):
            print(
                f"  NOTE tab/image counts changed: tabs {old_manifest.get('tabCount')} -> {len(flat)}, images {old_manifest.get('imageCount')} -> {len(images)}"
            )
    manifest = {
        "docId": doc_id,
        "slug": outdir,
        "title": title,
        "sourceUrl": SOURCE_URL.format(doc_id=doc_id),
        "layout": layout,
        "tabCount": len(flat),
        "imageCount": len(images),
        "tabs": written,
        "images": images,
    }
    manifest_text = yaml.safe_dump(
        manifest, allow_unicode=True, sort_keys=False, width=100
    )
    manifest_changed = write_if_different(manifest_path, manifest_text)
    pruned = prune_unreferenced_images(outdir, images)
    if pruned:
        print(f"  NOTE pruned {pruned} unreferenced attachment file(s)")
    new_state = {
        "slug": outdir,
        "engine": "markdown",
        "mdSha1": md_sha1,
        "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "imageCount": len(images),
    }
    if drive_ver:
        new_state["driveVersion"] = drive_ver
    state[doc_id] = new_state
    result = (
        ("updated" if prev else "created")
        if readme_changed or manifest_changed
        else "noop"
    )
    print(f"  {result}: layout={layout}, {len(flat)} tabs, {len(images)} images")
    return result


def sync_doc(
    doc: dict,
    state: dict,
    force: bool,
    from_cache: bool = False,
    engine: str = "markdown",
    docdirs: dict | None = None,
) -> str:
    """Skip intact unchanged versions or run the selected renderer with HTML fallback."""
    doc_id = doc["id"]
    checked_component(doc_id)
    checked_component(doc["slug"])
    slug = doc["slug"]
    title = doc.get("title", slug)
    print(f"\n=== {slug} ({doc_id}) ===")
    prev0 = state.get(doc_id, {})
    docdirs = docdirs if docdirs is not None else {}
    cur_dir = docdirs.get(doc_id, slug)
    drive_ver = None
    drive_name = None
    if not from_cache:
        try:
            meta = drive_meta(doc_id, "version,name,trashed")
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ConnectionError,
            IncompleteRead,
        ) as error:
            if not retryable(error):
                raise
            meta = None
            diagnostic = (
                f"http-{error.code}"
                if isinstance(error, HTTPError)
                else "timeout"
                if isinstance(error, TimeoutError)
                else "network"
            )
            print(
                f"WARN document={doc_id} version preflight failed ({diagnostic}); attempting full export",
                file=sys.stderr,
            )
        if meta:
            if meta.get("trashed") is True:
                raise TrashedDocument("mirror-document-trashed")
            drive_ver = meta.get("version")
            drive_name = meta.get("name")
        name_current = drive_name is None or doc_dirname(drive_name, doc_id) == cur_dir
        if (
            drive_ver
            and (not force)
            and name_current
            and (prev0.get("driveVersion") == drive_ver)
            and (output_directory(cur_dir) / "README.md").exists()
            and (output_directory(cur_dir) / "manifest.yaml").exists()
        ):
            try:
                mani = (
                    yaml.safe_load(
                        (output_directory(cur_dir) / "manifest.yaml").read_text()
                    )
                    or {}
                )
            except Exception:
                mani = {}
            imgs = mani.get("images") or []
            tabs_ok = all(
                (
                    document_path(output_directory(cur_dir), t["path"]).exists()
                    for t in mani.get("tabs") or []
                    if isinstance(t, dict) and t.get("path")
                )
            )
            if all_images_cached(cur_dir, imgs) and tabs_ok:
                print(
                    f"  unchanged (drive version {drive_ver}) — skipped without export"
                )
                return "unchanged"
    if engine == "markdown":
        md_result = sync_doc_markdown(
            doc, state, force, from_cache, drive_ver, drive_name, docdirs
        )
        if md_result is not None:
            return md_result
        print("  NOTE markdown engine unavailable for this doc; using the HTML engine")
    slug = docdirs.get(doc_id, slug)
    html = fetch_html(doc_id, slug=slug, from_cache=from_cache)
    if from_cache:
        print(f"  using cached HTML at {html_cache_path(slug)}")
    html_sha1 = hashlib.sha1(html).hexdigest()
    print(f"  HTML: {len(html):,} bytes, sha1={html_sha1[:12]}")
    doc_dir = output_directory(slug)
    doc_dir.mkdir(parents=True, exist_ok=True)
    readme_path = doc_dir / "README.md"
    manifest_path = doc_dir / "manifest.yaml"
    prev = state.get(doc_id, {})
    state_unchanged = prev.get("htmlSha1") == html_sha1
    have_md = readme_path.exists()
    have_manifest = manifest_path.exists()
    cached_images_known = []
    if have_manifest:
        try:
            cached_images_known = (yaml.safe_load(manifest_path.read_text()) or {}).get(
                "images"
            ) or []
        except Exception:
            cached_images_known = []
    images_intact = all_images_cached(slug, cached_images_known)
    cache_dir_for(slug).mkdir(parents=True, exist_ok=True)
    ensure_attachments_dir(doc_dir, slug)
    need_md = force or not state_unchanged or (not have_md) or (not have_manifest)
    need_images = not images_intact
    if not need_md and (not need_images):
        print("  unchanged — skipped")
        return "unchanged"
    print("  preprocessing HTML + running pandoc…")
    attach_before = {p.name for p in (doc_dir / "attachments").iterdir()}
    fixed, tabs = preprocess_html(html)
    md = run_pandoc(fixed, doc_dir)
    md = postprocess_md(md)
    md = redact_secrets(md, doc_mask_tiers(doc))
    images = parse_images_from_md(md)
    old_body = previous_content(doc_dir, manifest_path)
    if (
        not force
        and have_md
        and have_manifest
        and all_images_cached(slug, cached_images_known)
        and render_matches_previous(old_body, md)
    ):
        print(
            "  NOTE content identical to previous render (fingerprint match) — version-only churn; nothing written, state advanced"
        )
        adir = doc_dir / "attachments"
        rolled = 0
        for p in adir.iterdir():
            if p.is_file() and p.name not in attach_before:
                p.unlink()
                rolled += 1
        if rolled:
            print(
                f"  NOTE rolled back {rolled} attachment file(s) from the skipped render"
            )
        new_state = {
            "slug": slug,
            "htmlSha1": html_sha1,
            "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "imageCount": prev.get("imageCount", len(images)),
        }
        if drive_ver:
            new_state["driveVersion"] = drive_ver
        state[doc_id] = new_state
        return "unchanged"
    verdict = image_downgrade_verdict(cached_images_known, images, doc_dir)
    if verdict:
        prune_unreferenced_images(slug, cached_images_known)
        print("  FAIL image size reduction refused", file=sys.stderr)
        return "error"
    if need_md:
        readme_header = README_HEADER
        readme_changed = write_if_different(readme_path, readme_header + md)
        manifest = {
            "docId": doc_id,
            "slug": slug,
            "title": title,
            "sourceUrl": SOURCE_URL.format(doc_id=doc_id),
            "tabCount": len(tabs),
            "imageCount": len(images),
            "tabs": tabs,
            "images": images,
        }
        manifest_text = yaml.safe_dump(
            manifest, allow_unicode=True, sort_keys=False, width=100
        )
        manifest_changed = write_if_different(manifest_path, manifest_text)
        new_state = {
            "slug": slug,
            "htmlSha1": html_sha1,
            "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "imageCount": len(images),
        }
        if drive_ver:
            new_state["driveVersion"] = drive_ver
        state[doc_id] = new_state
        if readme_changed or manifest_changed:
            result = "updated" if prev else "created"
        else:
            result = "noop"
    else:
        result = "images-rebuilt"
    prune_unreferenced_images(slug, images)
    print(f"  {result}: {len(tabs)} tabs, {len(images)} images")
    return result


def load_all_sources() -> tuple[dict, list[dict], list[dict]]:
    """Read caller-curated and optionally discovered source lists without changing them."""
    sources = yaml.safe_load(SOURCES_YAML.read_text()) if SOURCES_YAML.exists() else {}
    manual = (sources or {}).get("docs") or []
    discovered_obj = (
        yaml.safe_load(DISCOVERED_YAML.read_text())
        if DISCOVERED_YAML and DISCOVERED_YAML.exists()
        else {}
    )
    discovered = (discovered_obj or {}).get("docs") or []
    return (sources or {}, manual, discovered)


CROSS_DOC_RE = re.compile(
    "docs\\.google\\.com/document/d/([a-zA-Z0-9_-]{20,})(?=[\\s/?#)\\]\\\"'])"
)


def crawl_for_new_docs(state: dict) -> list[dict]:
    """Probe linked documents and append accessible sources to the configured discovery file."""
    _, manual, discovered = load_all_sources()
    known_ids = {d["id"] for d in manual} | {d["id"] for d in discovered}
    inaccessible = state.setdefault("_inaccessible", {})
    now = datetime.now(timezone.utc)

    def cooling_down(doc_id):
        previous = inaccessible.get(doc_id, {})
        try:
            checked = datetime.fromisoformat(previous["checked_at"])
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            # Recheck legacy, stale or implausibly future-dated entries.
            return timedelta(0) <= now - checked < timedelta(hours=24)
        except (KeyError, ValueError, TypeError):
            return False

    taken_slugs = {d["slug"] for d in manual} | {d["slug"] for d in discovered}
    candidates: dict[str, str] = {}
    for readme in sorted(OUTPUT_DIR.rglob("*.md")):
        parent_slug = readme.relative_to(OUTPUT_DIR).parts[0]
        for m in CROSS_DOC_RE.finditer(
            readme.read_text(encoding="utf-8", errors="replace")
        ):
            cid = m.group(1)
            if cid in known_ids or cid in candidates or cooling_down(cid):
                continue
            candidates[cid] = parent_slug
    if not candidates:
        return []
    print(f"\n=== crawl: {len(candidates)} new candidate doc(s) to probe ===")
    new_entries: list[dict] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for cid, parent_slug in candidates.items():
        print(f"  probing {cid} (linked from {parent_slug})…")
        try:
            html = fetch_html(cid)
        except Exception as error:
            details = failure_details(error)
            print(
                f"WARN document={cid} stage=crawl category={details['category']} "
                f"http_status={details['http_status']}",
                file=sys.stderr,
            )
            if details["category"] != "inaccessible":
                raise
            inaccessible[cid] = {"checked_at": now.isoformat(), **details}
            print("    inaccessible (fetch error)")
            continue
        if not is_valid_doc_html(html):
            inaccessible[cid] = {
                "checked_at": now.isoformat(),
                "reason": "no doc title element (likely login/error page)",
            }
            print("    inaccessible (login or error page)")
            continue
        title = extract_doc_title(html)
        inaccessible.pop(cid, None)
        slug = derive_slug(title, cid, taken_slugs)
        entry = {
            "id": cid,
            "slug": slug,
            "title": title or "(untitled)",
            "added": today,
            "sharing": "authenticated" if TOKEN_FILE else "anyone-with-link",
            "discovered_from": parent_slug,
        }
        new_entries.append(entry)
        print(f"    accessible: title={title!r}, slug={slug}")
    if new_entries:
        existing_obj = (
            yaml.safe_load(DISCOVERED_YAML.read_text())
            if DISCOVERED_YAML.exists()
            else {}
        )
        existing_obj = existing_obj or {"docs": []}
        existing_obj["docs"] = (existing_obj.get("docs") or []) + new_entries
        header = "# Auto-managed by google-docs-authority/scripts/sync --crawl. Do not hand-edit.\n# Curated docs belong in the configured source list.\n\n"
        config.atomic_write(
            DISCOVERED_YAML,
            header
            + yaml.safe_dump(
                existing_obj, allow_unicode=True, sort_keys=False, width=100
            ),
        )
        print(f"  added {len(new_entries)} entries to {DISCOVERED_YAML.name}")
    return new_entries


def validate_sources():
    """Validate selectors before a run can write any document."""
    if not SOURCES_YAML.is_file():
        raise ValueError("mirror-source-list-missing")
    _, manual, discovered = load_all_sources()
    seen_ids, seen_slugs = set(), set()
    for doc in manual + discovered:
        if not isinstance(doc, dict):
            raise ValueError("mirror-source-entry-invalid")
        doc_id = doc.get("id")
        if not isinstance(doc_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id):
            raise ValueError("mirror-document-id-invalid")
        slug = checked_component(doc.get("slug"))
        if doc_id in seen_ids or slug in seen_slugs:
            raise ValueError("mirror-source-duplicate")
        seen_ids.add(doc_id)
        seen_slugs.add(slug)
        if doc.get("title") is not None and not isinstance(doc["title"], str):
            raise ValueError("mirror-document-title-invalid")
        tiers = doc_mask_tiers(doc)
        if tiers is not None and (not tiers or not tiers <= {"hard", "ctx", "heur"}):
            raise ValueError("mirror-document-mask-tiers-invalid")
    return manual + discovered


def doctor():
    """Check selected local dependencies without refreshing credentials."""
    docs = validate_sources()
    if TOKEN_FILE is None and not ALLOW_UNAUTHENTICATED:
        raise ValueError("mirror-read-token-required")
    if TOKEN_FILE is not None:
        token = json.loads(TOKEN_FILE.read_text())
        if not isinstance(token, dict) or any(
            not isinstance(token.get(key), str) or not token[key]
            for key in ("client_id", "client_secret", "refresh_token")
        ):
            raise ValueError("mirror-read-token-invalid")
    if MASK_ENABLED:
        redact_secrets("Synthetic redaction dependency check.\n")
    if not ALLOW_NO_PIL and not pil_available():
        raise ValueError("mirror-pillow-required")
    if not shutil.which(PANDOC_COMMAND[0]):
        raise ValueError("mirror-pandoc-command-missing")
    if (
        CACHE_LINK_CONFIGURED
        and (CACHE_LINK.exists() or CACHE_LINK.is_symlink())
        and (
            not CACHE_LINK.is_symlink()
            or CACHE_LINK.resolve() != DEFAULT_CACHE_TARGET.resolve()
        )
    ):
        raise ValueError("mirror-cache-link-invalid")
    print(
        f"OK mirror configuration and local dependencies ({len(docs)} selected sources)"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=config.default_config())
    parser.add_argument(
        "--root", help="Override the configured repository with the selected worktree"
    )
    parser.add_argument(
        "--state-file", help="Use the caller's transaction-local state file"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--engine", choices=("markdown", "html"))
    parser.add_argument(
        "--only", help="Select one configured document by ID or directory name"
    )
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="Discover linked documents into the configured discovery list",
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Re-render cached exports offline; missing caches fail",
    )
    parser.add_argument(
        "--mask-tier", help="Override redaction tiers (comma-separated)"
    )
    parser.add_argument(
        "--no-mask",
        action="store_true",
        help="Requires redaction already disabled in the private profile",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Validate local configuration and dependencies without Google requests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List selected document IDs without writing or contacting Google",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Read configured private document inspection status without Google requests",
    )
    parser.add_argument(
        "--setup-cache",
        action="store_true",
        help="Create the explicitly configured legacy cache link only",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        return selftest()
    try:
        if args.status and any(
            (
                args.setup_cache,
                args.doctor,
                args.dry_run,
                args.crawl,
                args.force,
                args.from_cache,
            )
        ):
            raise ValueError("mirror-status-arguments-conflict")
        settings = config.load(args.config, root_override=args.root)
        if "mirror" not in settings:
            raise ValueError("mirror-config-required")
        configure(settings, state_override=args.state_file, config_path=args.config)
        global OFFLINE, MASK_TIERS
        OFFLINE = args.from_cache
        if args.no_mask and MASK_ENABLED:
            raise ValueError("mirror-no-mask-requires-private-policy")
        if args.mask_tier:
            MASK_TIERS = set(args.mask_tier.split(","))
            if not MASK_TIERS <= {"hard", "ctx", "heur"}:
                raise ValueError("mirror-mask-tiers-invalid")
        if args.setup_cache:
            ensure_cache_link()
            print("OK configured mirror cache is ready")
            return 0
        if args.doctor:
            return doctor()
        if args.from_cache and args.crawl:
            raise ValueError("mirror-offline-crawl-invalid")
        if args.crawl and DISCOVERED_YAML is None:
            raise ValueError("mirror-crawl-discovery-list-required")
        docs = validate_sources()
        status = load_status(STATUS_FILE)
        state = (
            json.loads(STATE_FILE.read_text())
            if not args.status and STATE_FILE.exists()
            else {}
        )
        if not isinstance(state, dict):
            raise ValueError("mirror-state-invalid")
        docdirs = {}
        for manifest in OUTPUT_DIR.glob("*/manifest.yaml"):
            data = yaml.safe_load(manifest.read_text()) or {}
            if data.get("docId"):
                docdirs[data["docId"]] = checked_component(manifest.parent.name)
        selected = [
            doc
            for doc in docs
            if not args.only
            or args.only in (doc["id"], doc["slug"], docdirs.get(doc["id"]))
        ]
        if args.only and not selected:
            raise ValueError("mirror-selected-document-not-found")
        if args.status:
            if STATUS_FILE is None:
                raise ValueError("mirror-status-file-required")
            print(
                json.dumps(
                    {
                        "schema": status["schema"],
                        "documents": {
                            doc["id"]: status["documents"].get(doc["id"])
                            for doc in selected
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "documents": [doc["id"] for doc in selected],
                        "engine": args.engine
                        or settings["mirror"].get("engine", "markdown"),
                        "crawl": args.crawl,
                    }
                )
            )
            return 0
        ensure_cache_link()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        counts = {}
        rounds = 0
        while True:
            rounds += 1
            for doc in selected:
                failure = None
                try:
                    result = sync_doc(
                        doc,
                        state,
                        force=args.force or args.from_cache,
                        from_cache=args.from_cache,
                        engine=args.engine
                        or settings["mirror"].get("engine", "markdown"),
                        docdirs=docdirs,
                    )
                except Exception as error:
                    failure = failure_details(error)
                    result = "error"
                if result == "error":
                    failure = failure or {
                        "category": "render-refused",
                        "http_status": None,
                    }
                previous_failures = (
                    status["documents"]
                    .get(doc["id"], {})
                    .get("consecutive_failures", 0)
                )
                record = (
                    None
                    if args.from_cache
                    else record_result(STATUS_FILE, status, doc["id"], failure)
                )
                if failure:
                    print(
                        f"FAIL document={doc['id']} category={failure['category']} "
                        f"http_status={failure['http_status']} "
                        f"consecutive_failures={record['consecutive_failures'] if record else 'unrecorded'}",
                        file=sys.stderr,
                    )
                elif previous_failures and record:
                    print(
                        f"OK document={doc['id']} recovered after {previous_failures} failed inspection(s)"
                    )
                counts[result] = counts.get(result, 0) + 1
            if counts.get("error"):
                print("FAIL synchronization state was not advanced", file=sys.stderr)
                return 1
            if not args.crawl:
                break
            new = crawl_for_new_docs(state)
            if not new:
                break
            # Each discovery is processed once; failed batches cannot promote
            # the checkpoint of earlier successful documents in this run.
            selected = new
        config.atomic_write(STATE_FILE, json.dumps(state, indent=2, sort_keys=True))
        print(
            f"OK mirror complete: rounds={rounds} results={json.dumps(counts, sort_keys=True)}"
        )
        return 0
    except Exception as error:
        diagnostic = (
            str(error)
            if isinstance(error, ValueError)
            and re.fullmatch(r"[a-z][a-z0-9-]+", str(error))
            else "mirror-operation-failed"
        )
        print("FAIL " + diagnostic, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
