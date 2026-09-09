"""Install a frozen preset into a newly created library, never update an existing copy."""
from __future__ import annotations

import json
import html
from pathlib import Path, PurePosixPath
import re

from .content_source_contract import ContentSourceContractError, _cli_json, _feishu_document_payload
from .feishu_cli import CliRunner
from .oral_structure_preset import (
    OralStructurePreset, PresetError, RECEIPT_NAME, _ordinary,
    canonical_json, digest, feishu_markdown,
)
from .templates import ROOT_TITLES


_MD_ESCAPE = re.compile(r"\\([\\`*{}\[\]()#+\-.!_|$~<>])")
_INLINE = re.compile(r"(`+)(.+?)\1|(?<!\\)\[((?:\\.|[^\]\\])*)\]\(([^\s)]+)\)")


def _inline_signature(text: str) -> tuple:
    def plain(value: str) -> str:
        # Normalize for comparison only. Never rewrite fetched Markdown with these values.
        value = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", value)
        return " ".join(html.unescape(_MD_ESCAPE.sub(r"\1", value)).split())
    output = []
    offset = 0
    for item in _INLINE.finditer(text):
        if prefix := plain(text[offset:item.start()]):
            output.append(("text", prefix))
        if item.group(1):
            output.append(("code", item.group(2)))
        else:
            output.append(("link", plain(item.group(3)), _MD_ESCAPE.sub(r"\1", item.group(4))))
        offset = item.end()
    if suffix := plain(text[offset:]):
        output.append(("text", suffix))
    return tuple(output)


def _markdown_signature(content: str) -> tuple:
    """Compare the bundled Markdown subset, retaining ordered text, cells, code and URLs.

    Feishu escapes literals and reformats table padding / bullet markers on export.
    Unknown semantic changes still fail closed. Code (including JSON metadata) is literal.
    """
    output = []
    fence = None
    for raw in content.replace("\r\n", "\n").splitlines():
        line = raw.strip()
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip():
                output.append(("code_end",))
                fence = None
            else:
                output.append(("code_line", raw))
            continue
        if marker:
            fence = marker.group(1)
            output.append(("code_start",))
            continue
        if not line:
            continue
        if line.startswith("|"):
            cells = re.split(r"(?<!\\)\|", line)[1:]
            if cells and not cells[-1].strip():
                cells.pop()
            if cells and all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells):
                output.append(("table_header_separator", len(cells)))
                continue
            output.append(("row", tuple(_inline_signature(cell) for cell in cells)))
            continue
        if match := re.match(r"^(#{1,6})\s+(.*)", line):
            output.append(("heading", len(match.group(1)), _inline_signature(match.group(2))))
        elif match := re.match(r"^(>+)\s?(.*)", line):
            output.append(("quote", len(match.group(1)), _inline_signature(match.group(2))))
        elif re.match(r"^[-*+]\s+", line):
            output.append(("bullet", _inline_signature(line[2:])))
        elif match := re.match(r"^(\d+)[.)]\s+(.*)", line):
            output.append(("ordered", _inline_signature(match.group(2))))
        else:
            output.append(("line", _inline_signature(line)))
    if fence:
        output.append(("unclosed_code",))
    return tuple(output)


def _safe_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise PresetError("预置内容目标目录不安全。", "structure_conflict")
    for part in reversed((path, *path.parents)):
        _ordinary(part, directory=True)


def _write_new(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
    _ordinary(path)
    if path.read_bytes() != payload:
        raise PresetError("预置内容回读不一致。", "readback_failed")


def install_obsidian_preset(vault: Path, preset: OralStructurePreset) -> dict:
    method_root = vault / ROOT_TITLES["04"]
    control_root = vault / ROOT_TITLES["06"]
    seed_root = method_root / "口播结构"
    record_path = control_root / f"{RECEIPT_NAME}.json"
    try:
        _safe_directory(method_root)
        _safe_directory(control_root)
        if seed_root.exists() or seed_root.is_symlink() or record_path.exists() or record_path.is_symlink():
            raise PresetError("口播结构或安装记录已存在，未覆盖或补装。", "structure_conflict")
        seed_root.mkdir()  # Exclusive claim before any document writes.
        for relative in preset.directories:
            if relative != "口播结构":
                (method_root / relative).mkdir()
        objects = {}
        for document in preset.documents:
            path = method_root / document.path
            _safe_directory(path.parent)
            payload = document.content.encode("utf-8")
            _write_new(path, payload)
            objects[document.path] = (path.relative_to(vault).as_posix(), digest(payload))
        # Verify the complete copy again before writing an installation receipt.
        for document in preset.documents:
            path = method_root / document.path
            _safe_directory(path.parent)
            _ordinary(path)
            if digest(path.read_bytes()) != document.sha256:
                raise PresetError("口播结构完整回读失败。", "readback_failed")
        receipt = preset.receipt("obsidian", objects)
        _write_new(record_path, canonical_json(receipt))
        return receipt
    except FileExistsError as exc:
        raise PresetError("口播结构写入发生同名冲突，已停止且未覆盖。", "structure_conflict") from exc
    except OSError as exc:
        raise PresetError("口播结构写入或回读失败，已保留已创建的内容。", "write_failed") from exc


class _FeishuPresetWriter:
    def __init__(self, runner: CliRunner, space_id: str) -> None:
        if not re.fullmatch(r"[0-9]+", space_id):
            raise PresetError("飞书知识空间标识不合法。", "structure_conflict")
        self.runner = runner
        self.space_id = space_id
        self.created_tokens: set[str] = set()
        self.created_objects: set[str] = set()

    def call(self, arguments: tuple[str, ...], *, code: str = "readback_failed", content: str | None = None) -> dict:
        try:
            response = self.runner.run(("lark-cli", "--as", "user", *arguments, "--format", "json"), stdin=content)
            data = _cli_json(response)
        except (OSError, ContentSourceContractError) as exc:
            raise PresetError("飞书口播结构操作未返回可验证结果。", code) from exc
        if (response.returncode != 0 or data.get("ok") is False or data.get("error")
                or data.get("code") not in (None, 0) or data.get("result") not in (None, "success")):
            raise PresetError("飞书口播结构操作失败。", code)
        return data

    def children(self, parent: str = "") -> list[dict]:
        arguments = ("wiki", "nodes", "list", "--space-id", self.space_id)
        if parent:
            arguments += ("--parent-node-token", parent)
        data = self.call((*arguments, "--page-all"))
        items = data.get("items")
        if items is None and data.get("has_more") is False:
            items = []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items) or data.get("has_more"):
            raise PresetError("飞书目录列表不完整。", "readback_failed")
        return items

    def validate_node(self, node: dict, title: str, parent: str) -> dict:
        if (not isinstance(node, dict) or node.get("title") != title
                or node.get("space_id") != self.space_id
                or node.get("parent_node_token", "") != parent
                or node.get("obj_type") != "docx" or node.get("node_type") != "origin"
                or any(not isinstance(node.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", node[key])
                       for key in ("obj_token", "node_token"))):
            raise PresetError("飞书口播结构节点身份、类型或位置不符。", "readback_failed")
        return node

    def create(self, title: str, parent: str) -> dict:
        if any(item.get("title") == title for item in self.children(parent)):
            raise PresetError("飞书口播结构出现同名节点，未覆盖。", "structure_conflict")
        data = self.call((
            "wiki", "nodes", "create", "--params", json.dumps({"space_id": self.space_id}, separators=(",", ":")),
            "--data", json.dumps({"obj_type": "docx", "node_type": "origin", "title": title,
                                  "parent_node_token": parent}, ensure_ascii=False, separators=(",", ":")),
        ), code="write_failed")
        node = self.validate_node(data.get("node"), title, parent)
        if node["node_token"] in self.created_tokens or node["obj_token"] in self.created_objects:
            raise PresetError("飞书创建返回了重复对象，已停止。", "readback_failed")
        self.created_tokens.add(node["node_token"])
        self.created_objects.add(node["obj_token"])
        self.verify_node(node)
        return node

    def verify_node(self, expected: dict) -> None:
        data = self.call(("wiki", "spaces", "get_node", "--params", json.dumps({"token": expected["node_token"]}, separators=(",", ":"))))
        node = self.validate_node(data.get("node"), expected["title"], expected.get("parent_node_token", ""))
        if any(node[key] != expected[key] for key in ("node_token", "obj_token")):
            raise PresetError("飞书口播结构对象回读不一致。", "readback_failed")

    @staticmethod
    def document_payload(node: dict, content: str) -> str:
        # The live Markdown importer otherwise promotes the first H1 to the document title.
        # Keep the numbered Wiki name independent from the method's original body heading.
        return f"<title>{html.escape(node['title'])}</title>\n\n{content}"

    def write(self, node: dict, content: str) -> None:
        self.call(("docs", "+update", "--api-version", "v2", "--doc", node["obj_token"],
                   "--command", "overwrite", "--doc-format", "markdown", "--content", "-"),
                  code="write_failed", content=self.document_payload(node, content))

    def readback(self, node: dict, content: str) -> str:
        self.verify_node(node)
        data = self.call(("docs", "+fetch", "--api-version", "v2", "--doc", node["obj_token"], "--doc-format", "markdown"))
        document = data.get("document")
        actual = document.get("content") if isinstance(document, dict) else None
        body = actual
        if isinstance(actual, str) and (title := re.match(r"^<title>(.*?)</title>\s*", actual, re.DOTALL)):
            if html.unescape(title.group(1)) != node["title"]:
                raise PresetError("飞书口播结构文档标题回读不一致。", "readback_failed")
            body = actual[title.end():]
        # The exporter omits the separate title tag when it equals the first H1.
        # Wiki title identity was verified above; compare the complete original body.
        if not isinstance(body, str) or _markdown_signature(body) != _markdown_signature(content):
            raise PresetError("飞书口播结构正文或链接回读不一致。", "readback_failed")
        return digest(actual.encode("utf-8"))


def install_feishu_preset(runner: CliRunner, space_id: str, preset: OralStructurePreset) -> dict:
    writer = _FeishuPresetWriter(runner, space_id)
    roots = writer.children()
    selected = {}
    for key in ("04", "06"):
        matches = [item for item in roots if item.get("title") == ROOT_TITLES[key]]
        if len(matches) != 1:
            raise PresetError("飞书预置目标根目录缺失或重复。", "structure_conflict")
        selected[key] = writer.validate_node(matches[0], ROOT_TITLES[key], "")
        writer.verify_node(selected[key])
    if any(item.get("title") == RECEIPT_NAME for item in writer.children(selected["06"]["node_token"])):
        raise PresetError("飞书口播结构安装记录已存在，未补装。", "structure_conflict")
    nodes = {}
    for relative in preset.directories:
        parent = PurePosixPath(relative).parent.as_posix()
        parent_token = selected["04"]["node_token"] if parent == "." else nodes[parent]["node_token"]
        nodes[relative] = writer.create(PurePosixPath(relative).name, parent_token)
    for document in preset.documents:
        path = PurePosixPath(document.path)
        nodes[document.path] = writer.create(path.stem, nodes[path.parent.as_posix()]["node_token"])
    urls = {PurePosixPath(doc.path).stem: f"https://feishu.cn/wiki/{nodes[doc.path]['node_token']}" for doc in preset.documents}
    content_by_path = {doc.path: feishu_markdown(doc, urls) for doc in preset.documents}
    for directory in preset.directories:
        # Directory documents are useful navigation pages, not blank placeholders.
        children = [(path, node) for path, node in nodes.items() if PurePosixPath(path).parent.as_posix() == directory]
        content_by_path[directory] = f"# {PurePosixPath(directory).name}\n\n" + "\n".join(
            f"- [{node['title']}](https://feishu.cn/wiki/{node['node_token']})" for _, node in children
        ) + "\n"
    for path, content in content_by_path.items():
        writer.write(nodes[path], content)
    actual_hashes = {path: writer.readback(nodes[path], content) for path, content in content_by_path.items()}
    objects = {doc.path: (f"https://feishu.cn/wiki/{nodes[doc.path]['node_token']}", actual_hashes[doc.path]) for doc in preset.documents}
    receipt = preset.receipt("feishu", objects)
    record = writer.create(RECEIPT_NAME, selected["06"]["node_token"])
    payload = _feishu_document_payload(RECEIPT_NAME, receipt)
    writer.write(record, payload)
    writer.readback(record, payload)
    return receipt
