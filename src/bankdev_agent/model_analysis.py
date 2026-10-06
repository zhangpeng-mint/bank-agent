"""Model-assisted proposals around the deterministic evidence workflow."""
from __future__ import annotations

from dataclasses import asdict

from bankdev_agent.errors import BankDevError
from bankdev_agent.index_query import IndexQuery
from bankdev_agent.knowledge_query import KnowledgeQuery
from bankdev_agent.model_gateway import ModelGateway, digest
from bankdev_agent.requirement_analysis import Claim, ClaimVerifier, RequirementAnalyzer, RequirementFrame


class AssistedRequirementAnalyzer:
    def __init__(self, analyzer: RequirementAnalyzer, gateway: ModelGateway, context: str = "requirement-only"):
        if context not in {"requirement-only", "verified-facts"}:
            raise ValueError("unknown model context policy")
        self.analyzer, self.gateway, self.context = analyzer, gateway, context

    def analyze(self, frame: RequirementFrame, max_depth: int = 8, max_nodes: int = 200) -> dict:
        # The frame is authoritative user input. Model proposals never fill user decisions,
        # acceptance criteria or non-goals silently, even if a quotation is present.
        intake = self.gateway.call("intake", {"requirement": asdict(frame)})
        report = self.analyzer.analyze(frame, max_depth, max_nodes)
        assistance = {"context_policy": self.context, "intake_proposals": intake or {},
                      "proposal_notice": "模型候选，未写入用户 RequirementFrame；引用原文不等同于语义已确认。",
                      "query_results": [], "selected_fact_ids": [], "fact_catalog": {},
                      "suggestion_notice": "仅验证结构与引用有效性，未证明推断/建议的语义正确；须人工复核。",
                      "events": self.gateway.events}
        report["model_assistance"] = assistance
        if intake:
            for term in dict.fromkeys(intake["queries"]):
                # Suggestions only reach bounded read-only query APIs, never shell/tools.
                result = {"query": term, "source": "model_proposal", "scope": "configured_index_snapshots",
                          "adopted_as_requirement": False}
                try:
                    knowledge = KnowledgeQuery(self.analyzer.config, self.analyzer.database).search(term, limit=3)
                    interfaces = IndexQuery(self.analyzer.config, self.analyzer.database).search_interface(term, limit=3)
                    result.update(knowledge=knowledge, interfaces=interfaces)
                except BankDevError:
                    result["status"] = "unresolved_in_configured_scope"
                assistance["query_results"].append(result)
        facts = []
        if self.context == "verified-facts" and report["publishable"] and report["verification"]["ok"]:
            for claim in report["claims"]:
                if claim["type"] != "fact":
                    continue
                claim_id = "claim:" + digest(claim["binding"])[:24]
                facts.append({"claim_id": claim_id, "text": claim["text"], "category": claim["category"],
                              "evidence_ids": claim["evidence_ids"]})
                assistance["fact_catalog"][claim_id] = claim["binding"]
                if len(facts) == 40:
                    break
        assistance["context_fact_count"] = len(facts)
        assistance["context_facts_truncated"] = (self.context == "verified-facts"
            and sum(c["type"] == "fact" for c in report["claims"]) > len(facts))
        draft = None
        if intake and report["publishable"]:
            draft = self.gateway.call("draft", {"requirement": asdict(frame), "facts": facts,
                "analysis_status": report["status"],
                "coverage_notice": "静态、限定 snapshot 与路径的分析；未命中不等于不存在。"})
        if draft:
            assistance["selected_fact_ids"] = draft["selected_fact_ids"]
            for item in draft["suggestions"]:
                category = {"inference": "inferences", "question": "open_questions", "recommendation": "recommendations"}[item["type"]]
                claim = asdict(Claim(item["text"], item["type"], category, item["evidence_ids"],
                                    confidence_reason=item["reason"]))
                claim.update(source="model", requires_review=True, countercondition=item["countercondition"])
                report["claims"].append(claim)
                if item["type"] == "question":
                    report["open_questions"].append(item["text"])
        assistance["status"] = "accepted" if intake and draft else "fallback"
        report["mode"] = "model_assisted" if intake and draft else "template_with_model_fallback"
        # The model can add advice, never promote a partial/blocked result to complete.
        report["verification"] = ClaimVerifier(self.analyzer.config, self.analyzer.database).verify(
            report["claims"], report["evidence_ledger"])
        if not report["verification"]["ok"]:
            report.update(status="blocked", ok=False, publishable=False)
            report["coverage"]["blockers"].append("post_model_claim_verification_failed")
        return report


def smoke_test(gateway: ModelGateway) -> dict:
    """Only hard-coded synthetic data; no repository reads or evidence sent."""
    frame = RequirementFrame.parse("目标：查询演示商品。非目标：不修改数据。验收：返回商品名称。")
    result = gateway.call("intake", {"requirement": asdict(frame)})
    draft = None
    if result:
        draft = gateway.call("draft", {"requirement": asdict(frame), "facts": [{
            "claim_id": "claim:synthetic-product", "text": "合成示例接口：GET /demo/products 返回商品名称。",
            "category": "synthetic_fact", "evidence_ids": ["evidence:synthetic-product"]}],
            "analysis_status": "synthetic_fixture"})
    return {"ok": result is not None and draft is not None, "data_class": "synthetic", "private_repository_content_sent": False,
            "validated_output": result, "validated_draft": draft, "events": gateway.events}
