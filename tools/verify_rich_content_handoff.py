#!/usr/bin/env python3
"""Synthetic Stage5→rich Stage7→both independent Content CLIs; no human Gates."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills"))

from shared.content_source_contract import build_base_manifest, write_obsidian_base_contract
from shared.contracts import BINDING_SCHEMA, ROOT_KEYS, Binding
from shared.obsidian_adapter import ObsidianAdapter
from shared.stage5_intake import IntakeRequest, Stage5Intake
from shared.stage7_method import ContentMethodRequest, PeerContentRequest, Stage7Method
from shared.templates import TEMPLATE_VERSION

TASK_ID = "01a01e29-a6ba-73a2-82e6-4ad1caa0f33b"
PEER_TEXT = {
    "original_title_summary": "合成标题：工具如何成为日常帮助。内容从工具选择转向使用条件。",
    "topic_value": "面对选项过多的读者，解释使用场景比功能数量更能支持判断。",
    "opening": "开头提出有了新工具却没有开始行动的疑惑，承诺解释这种落差。",
    "progression": "先还原顾虑，再比较条件，最后说明如何观察真实使用体验。",
    "details_and_function": "具体场景用于帮助读者代入：选完工具后仍不知道下一步做什么。\n\n" * 50,
    "closing": "回到日常需要，让读者带着明确问题尝试一次。",
    "adaptation": "同行拆解末尾：可借鉴问题与场景之间的关系，具体客户经历需要独立材料支持。",
}
STRUCTURE_END = "结构末尾：收束时回到开头承诺，明确适用条件与下一步。"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_cli(script: Path, arguments: list[str], cwd: Path) -> dict:
    # Every mutable/runtime location is explicitly under the temporary directory.
    completed = subprocess.run(
        [sys.executable, "-B", str(script), *arguments], cwd=cwd,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=False,
    )
    require(completed.returncode == 0, f"CLI failed ({script.name}): {completed.stdout} {completed.stderr}")
    result = json.loads(completed.stdout)
    require(isinstance(result, dict), "CLI must return one JSON object")
    return result


def check_metadata(metadata: dict, audience: str = "consumer") -> None:
    expected = {"audience_scope": audience, "claim_scope": "source_only",
                "maturity": "historical_reference", "source_verification": "unverified",
                "usage_scope": "method_reference_only"}
    for key, value in expected.items():
        require(metadata.get(key) == value, f"source metadata lost/changed: {key}")


def check_content(peer: str, method: str) -> None:
    for field, text in PEER_TEXT.items():
        require(text.strip() in peer, f"peer section was lost or truncated: {field}")
    require(STRUCTURE_END in method, "structure ending was lost")


def verify(koubo_root: Path, gzh_root: Path) -> dict:
    koubo = koubo_root / "Skills/content-koubo-slim/scripts/content_koubo_slim.py"
    gzh = gzh_root / "scripts/content-gzh-slim"
    require(koubo.is_file() and gzh.is_file(), "explicit Content candidate roots are incomplete")
    temporary_parent = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="zsk-rich-handoff-", dir=temporary_parent) as directory:
        root = Path(directory)
        vault = root / "synthetic-vault"
        vault.mkdir(mode=0o700)
        binding = Binding(BINDING_SCHEMA, "CLT-RICH-SYNTHETIC", "合成验收主体", "合成内容库",
                          "company", "obsidian", str(vault),
                          {key: f"root:{key}" for key in ROOT_KEYS}, TEMPLATE_VERSION)
        adapter = ObsidianAdapter()
        require(adapter.resolve_binding(binding).status == "ok", "ZSK binding failed")
        require(adapter.create_skeleton(binding).status == "ok", "ZSK skeleton failed")
        source_text = "# 合成参考素材\n\n" + "\n\n".join(PEER_TEXT.values()) + "\n\n" + STRUCTURE_END
        intake = Stage5Intake(adapter).execute(IntakeRequest(
            TASK_ID, binding, "合成参考.md", source_text.encode(), "合成参考",
            source_role="reference_method", original_retention_approved=True,
        ))
        require(intake.status == "registered" and intake.record is not None, "real Stage5 source registration failed")
        common = {"task_id": TASK_ID, "binding": binding, "source": intake.record,
                  "topic": "工具选择", "source_section": "合成原文完整章节",
                  "content_purposes": ("解释使用价值",), "keywords": ("使用条件",),
                  "audience_scope": "consumer", "usage_scope": "method_reference_only",
                  "maturity": "historical_reference", "source_verification": "unverified"}
        peer_request = PeerContentRequest(**common, title="完整同行拆解", **PEER_TEXT)
        method_request = ContentMethodRequest(**common, title="条件比较结构", use_when="用于比较使用条件与实际需要。",
                                              structure="先说场景，再解释条件，接着给出判断顺序。\n\n" * 40,
                                              adaptation=STRUCTURE_END)
        stage = Stage7Method(adapter)
        results = [stage.execute(peer_request), stage.execute(method_request),
                   stage.execute(replace(peer_request, title="内部培训同行", audience_scope="internal_sales_training")),
                   stage.execute(replace(method_request, title="禁止使用的方法", usage_scope="do_not_use"))]
        for result in results:
            require(result.status == "registered" and result.asset is not None, f"rich Stage7 failed: {result.code}")
            require(result.evidence["events"][-1]["action"] == "read_back", "Stage7 did not verify persisted asset")
        manifest = build_base_manifest(client_id=binding.client_id, knowledge_base_name=binding.knowledge_base_name,
                                       backend="obsidian", locator=str(vault))
        write_obsidian_base_contract(vault, manifest, {"contract_version": "content-source-v1",
                                     "knowledge_base_id": manifest["knowledge_base_id"], "profiles": [], "revision": 1})
        registry = root / "host/knowledge-base-registry.json"
        base = ["configure", "--vault", str(vault), "--registry", str(registry), "--client-id", binding.client_id]
        preview = run_cli(koubo, base, root)
        require(not registry.exists(), "Koubo preview wrote Registry")
        run_cli(koubo, [*base, "--confirmation", preview["confirmation"]], root)
        base = ["configure", "--knowledge-base", str(vault), "--registry", str(registry), "--default-no-ip"]
        before = registry.read_bytes()
        preview = run_cli(gzh, base, root)
        require(registry.read_bytes() == before, "GZH preview changed Registry")
        run_cli(gzh, [*base, "--confirmation", preview["confirmation"]], root)
        registered = json.loads(registry.read_text())
        require(len(registered["bindings"]) == 1, "configuration did not share one binding")
        shared_binding = next(iter(registered["bindings"].values()))
        require(set(shared_binding["supported_workflows"]) == {"content-koubo-slim", "content-gzh-slim"}, "shared binding workflow mismatch")

        discovery = run_cli(koubo, ["discover-methods", "--registry", str(registry), "--audience-scope", "consumer"], root)
        selected = {item["asset_id"]: item for item in discovery["items"]}
        require(set(selected) == {results[0].asset.asset_id, results[1].asset.asset_id}, "consumer discovery included internal or blocked assets")
        material_plan = []
        full_text = []
        for result in results[:2]:
            item = selected[result.asset.asset_id]
            read = run_cli(koubo, ["discover-methods", "--registry", str(registry), "--audience-scope", "consumer",
                           "--read-path", item["relative_path"], "--expected-sha256", item["page_sha256"]], root)
            check_metadata(read["source_metadata"])
            full_text.append(read["content"])
            material_plan.append({"relative_path": item["relative_path"], "page_sha256": item["page_sha256"],
                                  "reason": "依据具体想法，借鉴同行问题、场景和条件比较的组织方式"})
        check_content(*full_text)
        koubo_plan = root / "koubo-selection.json"
        write_json(koubo_plan, material_plan)
        koubo_runs = root / "koubo-runs"
        started = run_cli(koubo, ["start", "--registry", str(registry), "--runs-root", str(koubo_runs),
                          "--speaker-mode", "neutral", "--audience-scope", "consumer", "--topic-original", "得到工具以后怎样判断有没有帮助",
                          "--method-selection", str(koubo_plan)], root)
        require(started.get("run_created_now") is True, "Koubo no-reference Run not created")
        inputs = list(koubo_runs.rglob("analyzer_input_v1.json"))
        require(len(inputs) == 1, "Koubo created unexpected analyzer inputs")
        analyzer = json.loads(inputs[0].read_text())
        require(analyzer["references"] == [], "Koubo invented external references")
        candidates = {item["asset_role"]: item for item in analyzer["method_candidates"]}
        require(len(analyzer["method_candidates"]) == 2 and len(candidates) == 2, "Koubo lost a role or exceeded material budget")
        check_content(candidates["peer_content_asset"]["excerpt"], candidates["oral_method_asset"]["excerpt"])

        task_path = root / "gzh-task.json"
        write_json(task_path, {"knowledge_base": manifest["knowledge_base_id"], "ip": "none",
                              "user_thoughts": "我想解释得到工具以后，如何判断它是否真正提供帮助。", "references": []})
        discovery = run_cli(gzh, ["discover-sources", "--input", str(task_path), "--registry", str(registry)], root)
        by_id = {item["metadata"].get("asset_id"): item for item in discovery["inventory"]["04"]}
        gzh_plan = root / "gzh-selection.json"
        write_json(gzh_plan, {"knowledge_base_id": manifest["knowledge_base_id"], "business_refs": [],
                             "peer_refs": [by_id[results[0].asset.asset_id]["object_ref"]],
                             "method_refs": [by_id[results[1].asset.asset_id]["object_ref"]],
                             "rationale": "以同行场景解释问题，再按条件比较组织长文；保留适用范围"})
        preview = run_cli(gzh, ["preview-sources", "--input", str(task_path), "--registry", str(registry), "--retrieval-plan", str(gzh_plan)], root)
        catalog = preview["catalog"]["knowledge_bases"][0]
        peer, method = catalog["peer_content_assets"], catalog["content_method_assets"]
        require(len(peer) == len(method) == 1, "GZH role budget/material selection changed")
        check_content(peer[0]["excerpt"], method[0]["excerpt"])
        for item in (peer[0], method[0]):
            check_metadata(item["source_metadata"])
        write_json(gzh_plan, preview["retrieval_plan"])
        gzh_runs = root / "gzh-runs"
        started = run_cli(gzh, ["start", "--input", str(task_path), "--registry", str(registry), "--retrieval-plan", str(gzh_plan), "--store", str(gzh_runs)], root)
        require(started.get("outcome") == "created" and started.get("status") == "created", "GZH did not stop at unapproved Run start")
        saved_catalog = json.loads((gzh_runs / "runs" / started["run_id"] / "source_catalog.json").read_text())
        require(saved_catalog == preview["catalog"], "GZH frozen catalog differs from preview")
        require(not list(root.rglob("approved_direction.json")) and not list(root.rglob("direction_v1.json")), "acceptance crossed a human direction Gate")
        return {"status": "passed", "synthetic_source_registered": True, "real_obsidian_adapter_readback": True,
                "rich_stage7_assets": 4, "shared_binding": True, "external_references": 0,
                "koubo_materials": {"peer": 1, "method": 1}, "gzh_materials": {"peer": 1, "method": 1},
                "seven_peer_sections_preserved": True, "structure_ending_preserved": True,
                "source_scope_and_metadata_preserved": True, "koubo_consumer_scope_enforced": True,
                "human_gates_crossed": 0, "customer_data_accessed": False, "published": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content-koubo-slim-root", required=True, type=Path)
    parser.add_argument("--content-gzh-slim-root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.content_koubo_slim_root.resolve(), args.content_gzh_slim_root.resolve()), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
