#!/usr/bin/env python3
"""Verify a complete isolated installation with real DOC/OCR and a fresh process."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

CHILD = r'''
from pathlib import Path
import json, sys
from unittest import mock
from shared.contracts import BINDING_SCHEMA, ROOT_KEYS, Binding, KnowledgeFact
from shared.obsidian_adapter import ObsidianAdapter
from shared.stage5_intake import IntakeRequest, Stage5Intake
from shared.stage6_knowledge import KnowledgeRequest, Stage6Knowledge
from shared.templates import TEMPLATE_VERSION

mode, skills, output = sys.argv[1:]
skills, output = Path(skills), Path(output)
vault = output / '验收知识库'
vault.mkdir(exist_ok=True)
binding = Binding(BINDING_SCHEMA, 'CLT-INTAKE-PROBE', '合成验收客户', '验收知识库',
    'company', 'obsidian', str(vault), {key: f'root:{key}' for key in ROOT_KEYS}, TEMPLATE_VERSION)
adapter = ObsidianAdapter()
assert adapter.resolve_binding(binding).status in {'ok', 'reused'}
assert adapter.create_skeleton(binding).status in {'ok', 'reused'}
task = '01a124d0-f630-72b1-899f-6875e5dfecb5'
assets = skills / 'shared/assets/intake-probe'
requests = tuple(IntakeRequest(task, binding, name, payload, title,
    original_retention_approved=True) for name, payload, title in [
    ('旧Word.doc', (assets/'legacy.doc').read_bytes(), '旧Word'),
    ('参数图片.jpg', (assets/'image.jpg').read_bytes(), 'JPEG参数'),
    ('参数图片.png', (assets/'image.png').read_bytes(), 'PNG参数'),
    ('说明.txt', b'Synthetic plain text input.', '说明')])
intake = Stage5Intake(adapter)
if mode == 'fresh':
    with mock.patch('shared.stage5_intake.convert_legacy_doc', side_effect=AssertionError('must reuse')), \
         mock.patch('shared.stage5_intake.prepare_image', side_effect=AssertionError('must reuse')):
        results = intake.execute_batch(requests)
    assert all(item.status == 'reused' for item in results), [(x.status,x.code) for x in results]
else:
    unsupported = IntakeRequest(task, binding, '音频.mp3', b'synthetic audio', '音频')
    batch = intake.execute_batch((unsupported, *requests))
    assert batch[0].code == 'format_unsupported'
    results = batch[1:]
    assert all(item.status == 'registered' for item in results), [(x.status,x.code) for x in results]
for result in results:
    if result.record.page_text_evidence:
        page = result.record.page_text_evidence[0]
        quote = next(line for line in page.verbatim_text.splitlines() if '10000' in line)
        fact = KnowledgeFact(quote, 1, quote, page.evidence_sha256)
        route = Stage6Knowledge(adapter).execute(KnowledgeRequest(task, binding, result.record,
            result.record.source_title+'参数', '合成产品参数', quote, fact_evidence=(fact,)))
        assert route.status in {'registered','reused'}, (route.status,route.code)
rows = [{'file':x.record.original_name,'status':x.status,'source_id':x.source_id,
         'content_kind':x.record.content_kind,'original_sha256':x.record.original_sha256,
         'page_count':x.record.page_count,'refs':[r.as_dict() for r in x.refs]} for x in results]
print(json.dumps(rows, ensure_ascii=False))
'''


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
            and p.suffix not in {".pyc", ".pyo"}}


def run(command: tuple[str, ...], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=180, check=False)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, help="Optional extracted complete installation package")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    destination = output / "installed-skills"
    source = args.package_root.expanduser().resolve() if args.package_root else ROOT
    env = dict(os.environ, PIP_NO_INDEX="1", PYTHONDONTWRITEBYTECODE="1")
    install_log = run((sys.executable, "-B", str(source / "install.py"), "--dest", str(destination)), cwd=source, env=env)
    before = snapshot(destination)
    prepare_log = run((sys.executable, "-B", str(source / "install.py"), "--dest", str(destination),
                       "--prepare-documents"), cwd=source, env=env)
    assert snapshot(destination) == before, "preparation must not rewrite the installed components"
    doctor_log = run((sys.executable, "-B", str(source / "install.py"), "--dest", str(destination),
                      "--doctor", "--formats", "doc,jpg,png,docx,pdf,pptx,xlsx"), cwd=source, env=env)
    env["PYTHONPATH"] = str(destination)
    first = json.loads(run((sys.executable, "-B", "-c", CHILD, "first", str(destination), str(output)), cwd=output, env=env))
    stored = snapshot(output / "验收知识库")
    fresh = json.loads(run((sys.executable, "-B", "-c", CHILD, "fresh", str(destination), str(output)), cwd=output, env=env))
    assert snapshot(output / "验收知识库") == stored, "fresh-task reuse must not change library files"
    assert [x["source_id"] for x in first] == [x["source_id"] for x in fresh]
    report = {"status": "passed", "download_disabled": True, "installed_bundle_unchanged": True,
              "first_process": first, "fresh_process": fresh, "library_sha256": stored,
              "limits": ["synthetic documents only", "local Obsidian, not live Feishu",
                         "Windows Word COM and customer Doubao require live verification"]}
    (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (output / "installation.log").write_text(install_log + prepare_log + doctor_log, encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(output), "first_registered": len(first),
                      "fresh_reused": len(fresh)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
