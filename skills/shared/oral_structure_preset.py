"""Versioned, read-only oral structure seed package; never a client source registry."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping

PACKAGE_ROOT = Path(__file__).parent / "assets" / "oral-structure-v1"
RECEIPT_NAME = "oral-structure-installation"
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


class PresetError(ValueError):
    def __init__(self, message: str, code: str = "preset_invalid") -> None:
        super().__init__(message)
        self.code = code


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class PresetDocument:
    path: str
    content: str
    sha256: str

    @property
    def frontmatter(self) -> str:
        if self.content.startswith("---\n"):
            return self.content[4:].partition("\n---\n")[0]
        return ""


@dataclass(frozen=True)
class OralStructurePreset:
    version: str
    sha256: str
    source_archive_sha256: str
    documents: tuple[PresetDocument, ...]

    @property
    def directories(self) -> tuple[str, ...]:
        return tuple(sorted(
            {parent.as_posix() for doc in self.documents for parent in PurePosixPath(doc.path).parents if parent.as_posix() != "."},
            key=lambda value: (len(PurePosixPath(value).parts), value),
        ))

    def receipt(self, backend: str, objects: Mapping[str, tuple[str, str]]) -> dict:
        return {
            "schema_version": "zsk-oral-structure-installation-v1",
            "package_id": "oral-structure",
            "package_version": self.version,
            "package_sha256": self.sha256,
            "source_archive_sha256": self.source_archive_sha256,
            "source_provenance": "bundled_method_reference_not_client_source_registration",
            "backend": backend,
            "update_policy": "new_libraries_only",
            "files": [
                {"package_path": doc.path, "source_sha256": doc.sha256,
                 "source_frontmatter": doc.frontmatter,
                 "object_ref": objects[doc.path][0], "installed_sha256": objects[doc.path][1]}
                for doc in self.documents
            ],
        }


def _ordinary(path: Path, *, directory: bool = False) -> None:
    attributes = path.lstat()
    if (stat.S_ISLNK(attributes.st_mode)
            or getattr(attributes, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not (stat.S_ISDIR(attributes.st_mode) if directory else stat.S_ISREG(attributes.st_mode))):
        raise PresetError("预置包包含非普通文件或目录。")


def load_preset(package_root: Path | None = None) -> OralStructurePreset:
    root = PACKAGE_ROOT if package_root is None else package_root
    try:
        _ordinary(root, directory=True)
        manifest_path = root / "package.json"
        _ordinary(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict)
                or manifest.get("schema_version") != "zsk-oral-structure-package-v1"
                or manifest.get("package_id") != "oral-structure"
                or not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("package_version", "")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("source_archive_sha256", "")))):
            raise PresetError("预置包版本清单不合法。")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise PresetError("预置包文件清单为空。")
        documents = []
        paths: set[str] = set()
        titles: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise PresetError("预置包文件清单不合法。")
            relative = entry["path"]
            path = PurePosixPath(relative)
            if (not path.parts or path.is_absolute() or path.as_posix() != relative or ".." in path.parts
                    or "\\" in relative or ":" in relative or path.parts[0] != "口播结构"
                    or path.suffix != ".md" or relative.casefold() in paths or path.stem in titles):
                raise PresetError("预置包路径越界或名称重复。")
            paths.add(relative.casefold())
            titles.add(path.stem)
            target = root / path
            for parent in path.parents:
                _ordinary(root / parent, directory=True)
            _ordinary(target)
            content = target.read_text(encoding="utf-8")  # Canonical LF survives Git CRLF checkouts.
            sha256 = digest(content.encode("utf-8"))
            if not content.strip() or sha256 != entry.get("sha256"):
                raise PresetError("预置包正文缺失或摘要不匹配。")
            documents.append(PresetDocument(relative, content, sha256))
        for document in documents:
            for link in WIKILINK.finditer(document.content):
                if link.group(1).partition("#")[0] not in titles:
                    raise PresetError("预置包包含无法解析的内部链接。")
        # No unlisted files or symlink directories can silently enter the distributed package.
        expected = {"package.json", *(doc.path for doc in documents)}
        found = set()
        for path in root.rglob("*"):
            _ordinary(path, directory=path.is_dir())
            if path.is_file():
                found.add(path.relative_to(root).as_posix())
        if found != expected:
            raise PresetError("预置包实际文件与清单不一致。")
        return OralStructurePreset(manifest["package_version"], digest(canonical_json(manifest)),
                                   manifest["source_archive_sha256"], tuple(documents))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PresetError("口播结构预置包缺失、损坏或不可读。") from exc


def feishu_markdown(document: PresetDocument, urls_by_title: Mapping[str, str]) -> str:
    content = document.content
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end < 0:
            raise PresetError("预置文档元数据未闭合。")
        content = content[end + 5:].lstrip("\n")

    def replace(link: re.Match) -> str:
        title, _, section = link.group(1).partition("#")
        label = link.group(2) or (f"{title} · {section}" if section else title)
        if title not in urls_by_title:
            raise PresetError("飞书内部链接缺少已创建的目标。", "readback_failed")
        return f"[{label}]({urls_by_title[title]})"

    return WIKILINK.sub(replace, content)
