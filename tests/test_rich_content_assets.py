from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills"))

from shared.contracts import BINDING_SCHEMA, ROOT_KEYS, Binding
from shared.fake_adapter import FakeAdapter, FakeFaults
from shared.stage5_intake import IntakeRequest, Stage5Intake
from shared.stage7_method import ContentMethodRequest, MethodRequest, PeerContentRequest, Stage7Method
from shared.templates import TEMPLATE_VERSION


TASK_ID = "01a01e29-a6ba-73a2-82e6-4ad1caa0f33b"


class RichContentAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = Binding(
            BINDING_SCHEMA, "CLT-synthetic-04", "合成验收主体", "合成验收库",
            "company", "obsidian", "/private/tmp/zsk-rich-content-fixture",
            {key: f"root:{key}" for key in ROOT_KEYS}, TEMPLATE_VERSION,
        )
        self.adapter = FakeAdapter()
        self.adapter.resolve_binding(self.binding)
        self.adapter.create_skeleton(self.binding)
        result = Stage5Intake(self.adapter).execute(IntakeRequest(
            TASK_ID, self.binding, "同行合成样本.md",
            "# 合成参考：选择合适的学习工具\n\n讲述者比较了两种工具。\n".encode(),
            "合成同行样本", "reference_method",
        ))
        self.source = result.record
        self.request = PeerContentRequest(
            task_id=TASK_ID, binding=self.binding, source=self.source,
            title="从工具选择到使用习惯", topic="工具选择", source_section="正文第 1—7 段",
            original_title_summary="原始标题：选择合适的学习工具。来源对比工具，但最终讨论的是使用习惯。",
            topic_value="面向选择困难的读者，把功能差异转化为使用场景，帮助他们判断是否真的需要替换。",
            opening="来源从一个不断换工具却没有开始学习的场景切入，提出使用目的比功能清单重要。",
            progression="先还原困惑，再比较使用条件，最后解释保持习惯的重要性。\n\n每一步都回答上一步留下的问题。",
            details_and_function=("来源提到连续 3 天没有打开新工具。数字用于表达使用落差，不是效果证明，也不是当前客户经历。\n\n"
                                  "场景细节帮助读者把抽象功能代入日常动作，保留它的叙事作用。" * 4),
            closing="回到开始学习的目标，建议读者先记录实际使用需要。",
            adaptation="可借鉴选择问题与使用场景的关系；客户是否有同样经历须由其自身材料支持。",
            content_purposes=("比较选择", "解释顾虑"), keywords=("工具", "习惯"),
        )
        self.stage = Stage7Method(self.adapter)

    def test_full_peer_preserves_content_and_source_scope_without_promoting_facts(self) -> None:
        response = self.stage.execute_peer(self.request)
        self.assertEqual(response.status, "registered")
        self.assertIn(self.request.details_and_function, response.asset.body)
        self.assertIn(self.request.progression, response.asset.body)
        self.assertEqual(response.asset.source_role, "reference_method")
        self.assertEqual(response.asset.metadata["claim_scope"], "source_only")
        self.assertEqual(response.asset.metadata["source_readable_sha256"], self.source.readable_sha256)
        self.assertNotIn("write_knowledge_asset", self.adapter.calls)
        self.assertNotIn("write_profile", self.adapter.calls)
        self.assertIn("read_back", self.adapter.calls)
        self.assertEqual(response.evidence["downstream_asset_call_count"], 1)

    def test_replay_reuses_and_changed_details_create_a_new_asset(self) -> None:
        first = self.stage.execute(self.request)
        count = self.adapter.object_count
        replay = self.stage.execute(self.request)
        self.assertEqual(replay.status, "reused")
        self.assertEqual(self.adapter.object_count, count)
        revised = self.stage.execute(replace(self.request, details_and_function="补充了来源中细节的分析。"))
        self.assertEqual(revised.status, "registered")
        self.assertNotEqual(first.asset.asset_id, revised.asset.asset_id)
        self.assertEqual(self.adapter.object_count, count + 1)
        old = self.adapter._objects[f"asset:method:{first.asset.asset_id}"]["asset_data"]["body"]
        self.assertEqual(old, first.asset.body)

    def test_missing_a_section_is_rejected_before_any_write(self) -> None:
        count = self.adapter.object_count
        with self.assertRaises(ValueError):
            replace(self.request, progression=" ")
        self.assertEqual(count, self.adapter.object_count)

    def test_request_cannot_cross_clients(self) -> None:
        with self.assertRaises(ValueError):
            replace(self.request, binding=replace(self.binding, client_id="CLT-another-client"))

    def test_disallowed_sources_remain_blocked(self) -> None:
        for field, value, code in (
            ("permission_status", "denied", "permission_denied"),
            ("privacy_status", "blocked", "privacy_blocked"),
            ("status", "indexed_only", "ownership_unknown"),
            ("source_role", "profile_material", "routing_ambiguous"),
        ):
            with self.subTest(field=field):
                count = self.adapter.object_count
                response = self.stage.execute(replace(self.request, source=replace(self.source, **{field: value})))
                self.assertEqual(response.code, code)
                self.assertIsNone(response.asset)
                self.assertEqual(count, self.adapter.object_count)

    def test_readback_failure_retains_written_asset_but_does_not_claim_success(self) -> None:
        count = self.adapter.object_count
        self.adapter.faults = FakeFaults(readback_failure=True)
        response = self.stage.execute(self.request)
        self.assertEqual((response.status, response.code), ("exception", "readback_failed"))
        self.assertIsNone(response.asset)
        self.assertEqual(self.adapter.object_count, count + 1)

    def test_backend_permission_failure_is_zero_write(self) -> None:
        count = self.adapter.object_count
        self.adapter.faults = FakeFaults(permission_denied=True)
        response = self.stage.execute(self.request)
        self.assertEqual(response.code, "permission_denied")
        self.assertEqual(count, self.adapter.object_count)

    def test_unknown_source_record_cannot_bypass_registered_source_gate(self) -> None:
        source = replace(self.source, source_id="SRC-" + "a" * 24)
        count = self.adapter.object_count
        response = self.stage.execute(replace(self.request, source=source))
        self.assertEqual(response.status, "exception")
        self.assertEqual(self.adapter.object_count, count)

    def test_selection_guide_keeps_full_structure_and_workflow_restriction(self) -> None:
        text = "先理解受众是在比较、解释还是表达经历，再判断材料是否支持这个讲法。\n" * 8
        request = ContentMethodRequest(
            TASK_ID, self.binding, self.source, "如何选择讲法", "表达目标", "方法论章节",
            "用于只有想法且不确定怎样展开的任务。", text,
            "增强机制可辅助开头，不必替代主线；本来源尚未验证的部分要保留其原有说明。",
            method_kind="selection_guide", applicable_workflows=("content-koubo-slim",),
        )
        response = self.stage.execute(request)
        self.assertEqual(response.asset.metadata["asset_type"], "content_method_asset")
        self.assertEqual(response.asset.metadata["method_kind"], "selection_guide")
        self.assertEqual(response.asset.metadata["applicable_workflows"], ("content-koubo-slim",))
        self.assertIn(text.strip(), response.asset.body)
        self.assertNotIn("content-gzh-slim", response.asset.body)

    def test_rich_scope_is_preserved_and_changes_identity(self) -> None:
        first = self.stage.execute(self.request)
        scoped = replace(self.request, audience_scope="internal_sales_training", usage_scope="method_reference_only",
                         maturity="experimental_reference", source_verification="unverified")
        response = self.stage.execute(scoped)
        self.assertNotEqual(first.asset.asset_id, response.asset.asset_id)
        self.assertEqual(response.asset.metadata["audience_scope"], "internal_sales_training")
        for field in ("usage_scope", "maturity", "source_verification"):
            self.assertEqual(response.asset.metadata[field], getattr(scoped, field))
        self.assertEqual(response.asset.metadata["status"], "active")
        self.assertIn("内部销售培训", response.asset.body)
        self.assertIn("experimental_reference", response.asset.body)
        for field, value in (("audience_scope", "consumer"), ("usage_scope", "counterexample_only"),
                             ("maturity", "established"), ("source_verification", "verified")):
            revised = self.stage.execute(replace(scoped, **{field: value}))
            self.assertNotEqual(response.asset.asset_id, revised.asset.asset_id)

    def test_explicitly_forbidden_rich_cards_are_not_active(self) -> None:
        method = ContentMethodRequest(TASK_ID, self.binding, self.source, "方法", "主题", "结构章节",
                                      "适用条件", "完整结构说明", "使用边界", audience_scope="consumer")
        for request in (self.request, method):
            for usage in ("do_not_use", "blocked", "forbidden"):
                with self.subTest(kind=type(request).__name__, usage=usage):
                    result = self.stage.execute(replace(request, usage_scope=usage))
                    self.assertEqual(result.status, "registered")
                    self.assertEqual(result.asset.metadata["status"], "blocked")
                    self.assertIn("不得作为当前创作可用资产", result.asset.body)

    def test_invalid_scope_is_rejected_without_write(self) -> None:
        count = self.adapter.object_count
        for changes in ({"audience_scope": "unknown"}, {"usage_scope": "blocked\nstatus: active"},
                        {"maturity": ""}, {"source_verification": "unverified\nverified"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.request, **changes)
        self.assertEqual(self.adapter.object_count, count)

    def test_legacy_mechanism_path_still_rejects_source_case_and_long_fields(self) -> None:
        args = (TASK_ID, self.binding, self.source, "选择表达", "内容选择", "先说问题", "再讲条件", "短句对比", "回到行动")
        with self.assertRaises(ValueError):
            MethodRequest(*args, "引用案例")
        with self.assertRaises(ValueError):
            MethodRequest(*args, "方法" * 100)
        response = self.stage.execute(MethodRequest(*args, "按使用条件解释选择"))
        self.assertEqual(response.status, "registered")
        self.assertNotIn("claim_scope:", response.asset.body)


if __name__ == "__main__":
    unittest.main()
