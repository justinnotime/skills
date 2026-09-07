"""Explicit private configuration and atomic local record writes."""

import json
import math
import os
import re
import tempfile
from pathlib import Path


def default_config():
    return os.environ.get("GOOGLE_DOCS_AUTHORITY_CONFIG") or str(
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "google-docs-authority/config.json"
    )


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path, root_override=None):
    path = Path(os.path.expandvars(str(path))).expanduser().resolve()
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("schema") != "google-docs-authority/v1"
        or set(value)
        - {
            "schema",
            "read_token_file",
            "write_token_file",
            "pageless",
            "registry",
            "mirror",
            "render",
        }
    ):
        raise ValueError("config-schema-invalid")

    def resolve(text, base=path.parent):
        if not isinstance(text, (str, os.PathLike)) or not str(text):
            raise ValueError("config-path-required")
        expanded = os.path.expandvars(str(text))
        if re.search(r"\$(?:\w+|\{[^}]+\})", expanded):
            raise ValueError("config-path-variable-unresolved")
        target = Path(expanded).expanduser()
        return ((base / target) if not target.is_absolute() else target).resolve()

    if value.get("write_token_file"):
        value["write_token_file"] = resolve(value["write_token_file"])
    if value.get("read_token_file"):
        value["read_token_file"] = resolve(value["read_token_file"])
    value.setdefault("pageless", False)
    if type(value["pageless"]) is not bool:
        raise ValueError("config-pageless-invalid")
    if "registry" in value:
        registry = value["registry"]
        allowed = {
            "repository_root",
            "output",
            "source_directories",
            "mirror_directory",
            "source_lists",
        }
        if not isinstance(registry, dict) or set(registry) - allowed:
            raise ValueError("config-registry-invalid")
        root = resolve(root_override or registry.get("repository_root"))
        if not root.is_dir():
            raise ValueError("config-repository-root-missing")
        registry["repository_root"] = root

        def within_root(text):
            result = resolve(text, root)
            if not result.is_relative_to(root):
                raise ValueError("config-registry-path-outside-repository")
            return result

        registry["output"] = within_root(registry.get("output"))
        directories = registry.get("source_directories", [])
        if not isinstance(directories, list):
            raise ValueError("config-source-directories-invalid")
        registry["source_directories"] = [within_root(item) for item in directories]
        if any(not item.is_dir() for item in registry["source_directories"]):
            raise ValueError("config-source-directory-missing")
        if registry.get("mirror_directory"):
            registry["mirror_directory"] = within_root(registry["mirror_directory"])
        sources = registry.get("source_lists", {})
        if not isinstance(sources, dict) or any(
            not isinstance(k, str) or not k for k in sources
        ):
            raise ValueError("config-source-lists-invalid")
        registry["source_lists"] = {
            name: resolve(location) for name, location in sources.items()
        }
        if registry["output"] in {
            path,
            value.get("write_token_file"),
            *registry["source_lists"].values(),
        }:
            raise ValueError("config-output-must-be-distinct")

    def command(argv):
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or not arg for arg in argv)
        ):
            raise ValueError("config-command-invalid")
        result = [os.path.expandvars(os.path.expanduser(arg)) for arg in argv]
        if any(re.search(r"\$(?:\w+|\{[^}]+\})", arg) for arg in result):
            raise ValueError("config-command-variable-unresolved")
        return result

    if "mirror" in value:
        mirror = value["mirror"]
        allowed = {
            "repository_root",
            "output_directory",
            "source_list",
            "discovered_list",
            "state_file",
            "status_file",
            "cache_directory",
            "cache_link",
            "engine",
            "allow_unauthenticated",
            "redact_command",
            "redact_enabled",
            "mask_tiers",
            "image_shrink_floor",
            "allow_image_shrink",
            "allow_no_pillow",
            "pandoc_command",
            "pandoc_memory_max",
            "pandoc_timeout",
            "readme_header",
        }
        if not isinstance(mirror, dict) or set(mirror) - allowed:
            raise ValueError("config-mirror-invalid")
        root = resolve(root_override or mirror.get("repository_root"))
        if not root.is_dir():
            raise ValueError("config-repository-root-missing")
        mirror["repository_root"] = root
        for key in ("output_directory", "source_list", "discovered_list", "cache_link"):
            if key not in mirror and key in {"discovered_list", "cache_link"}:
                continue
            if key == "cache_link":
                raw = mirror[key]
                if not isinstance(raw, str) or not raw:
                    raise ValueError("config-path-required")
                expanded = Path(os.path.expandvars(raw)).expanduser()
                if re.search(r"\$(?:\w+|\{[^}]+\})", str(expanded)):
                    raise ValueError("config-path-variable-unresolved")
                target = expanded if expanded.is_absolute() else root / expanded
                target = target.parent.resolve() / target.name
            else:
                target = resolve(mirror.get(key), root)
            if target == root or not target.is_relative_to(root):
                raise ValueError("config-mirror-path-outside-repository")
            mirror[key] = target
        for key in ("state_file", "cache_directory", "status_file"):
            if key == "status_file" and key not in mirror:
                continue
            target = resolve(mirror.get(key))
            if target == root or target.is_relative_to(root):
                raise ValueError("config-mirror-runtime-path-inside-repository")
            mirror[key] = target
        if mirror["state_file"] in {
            path,
            value.get("read_token_file"),
            value.get("write_token_file"),
        }:
            raise ValueError("config-output-must-be-distinct")
        if "status_file" in mirror and mirror["status_file"] in {
            path,
            mirror["state_file"],
            mirror["cache_directory"],
            value.get("read_token_file"),
            value.get("write_token_file"),
        }:
            raise ValueError("config-output-must-be-distinct")
        for key, default in (
            ("allow_unauthenticated", False),
            ("redact_enabled", True),
            ("allow_image_shrink", False),
            ("allow_no_pillow", False),
        ):
            mirror.setdefault(key, default)
            if type(mirror[key]) is not bool:
                raise ValueError("config-mirror-boolean-invalid")
        mirror.setdefault("engine", "markdown")
        if mirror["engine"] not in {"markdown", "html"}:
            raise ValueError("config-mirror-engine-invalid")
        mirror.setdefault("mask_tiers", ["hard", "ctx"])
        if (
            not isinstance(mirror["mask_tiers"], list)
            or not mirror["mask_tiers"]
            or any(tier not in {"hard", "ctx", "heur"} for tier in mirror["mask_tiers"])
        ):
            raise ValueError("config-mirror-mask-tiers-invalid")
        if mirror["redact_enabled"] or "redact_command" in mirror:
            mirror["redact_command"] = command(mirror.get("redact_command"))
        mirror["pandoc_command"] = command(mirror.get("pandoc_command", ["pandoc"]))
        mirror.setdefault("image_shrink_floor", 0.7)
        floor = mirror["image_shrink_floor"]
        if (
            type(floor) not in {int, float}
            or not math.isfinite(floor)
            or not 0 < floor <= 1
        ):
            raise ValueError("config-mirror-image-floor-invalid")
        mirror.setdefault("pandoc_timeout", 300)
        if type(mirror["pandoc_timeout"]) is not int or mirror["pandoc_timeout"] <= 0:
            raise ValueError("config-mirror-timeout-invalid")
        if mirror.get("pandoc_memory_max") is not None and not re.fullmatch(
            r"[1-9][0-9]*[KMGT]?", str(mirror["pandoc_memory_max"])
        ):
            raise ValueError("config-mirror-memory-limit-invalid")
        if "readme_header" in mirror and not isinstance(mirror["readme_header"], str):
            raise ValueError("config-mirror-header-invalid")
    if "render" in value:
        render = value["render"]
        if not isinstance(render, dict) or set(render) - {"pdftoppm_command"}:
            raise ValueError("config-render-invalid")
        render["pdftoppm_command"] = command(
            render.get("pdftoppm_command", ["pdftoppm"])
        )
    return value
