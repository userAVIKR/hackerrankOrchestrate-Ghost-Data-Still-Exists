"""End-to-end audit pipeline: load -> extract evidence -> attribute -> assess -> plan."""
from __future__ import annotations

import time

from .evidence import EvidenceStore
from .identity import IdentityIndex
from .intent import classify_request
from .loader import Dataset
from .planner import plan, serialize
from .reasoner import CaseAssessment, Reasoner


class AuditAgent:
    def __init__(self, dataset_dir=None, verbose=True, llm=False, usage=None, intent_model=True):
        t0 = time.time()
        self.usage = usage
        self.llm = None
        self.intent_model = None
        if intent_model:
            try:
                from .hf_intent import HFIntentModel

                self.intent_model = HFIntentModel(usage)
            except ImportError as exc:  # torch/transformers not installed: rules only
                print(f"[agent] local intent model unavailable ({exc}); using deterministic rules only")
        if llm:
            try:
                from .llm import IntentLLM

                self.llm = IntentLLM(usage)
            except Exception as exc:  # missing SDK or credentials: rules only
                print(f"[agent] LLM disabled ({type(exc).__name__}); using deterministic intent rules")
        self.ds = Dataset(dataset_dir) if dataset_dir else Dataset()
        self.identities = IdentityIndex(self.ds.identities, self.ds.audit_log)
        self.evidence = EvidenceStore(self.ds.dataset_dir).load(self.ds.activities)
        self.reasoner = Reasoner(self.ds, self.evidence, self.identities)
        # Every participant-visible request takes part in workflow attribution, because
        # requests from both files share the same deletion-worker activity stream.
        self.all_requests = self.ds.requests("sample_deletion_requests.csv") + self.ds.requests()
        self._prepare(self.all_requests)
        self.workflow = self.reasoner.attribute_workflow([r for r in self.all_requests if r["_authorized"]])
        if usage is not None:
            usage.local_ocr_images = sum(1 for m in self.evidence.manifest.values() if m["file_path"].endswith(".png"))
            usage.local_pdf_documents = sum(1 for m in self.evidence.manifest.values() if m["file_path"].endswith(".pdf"))
        if verbose:
            print(f"[agent] {len(self.evidence.observations)} evidence observations from "
                  f"{len(self.evidence.manifest)} files; ready in {time.time() - t0:.1f}s")

    def _prepare(self, requests):
        model_votes = [None] * len(requests)
        if self.intent_model is not None:
            try:
                model_votes = self.intent_model.classify_many([r["request_text"] for r in requests])
            except Exception as exc:  # model download / runtime failure: rules only
                print(f"[agent] local intent model failed ({type(exc).__name__}: {exc}); using rules only")
                self.intent_model = None
        for r, vote in zip(requests, model_votes):
            intent = classify_request(r["request_text"])
            if vote is not None:
                # The rules are authoritative when confident; the LLM decides when they are not.
                intent["model_p_yes"] = vote["p_yes"]
                intent["model_agrees"] = vote["authorizes_deletion"] == intent["authorizes_deletion"]
                if intent["confidence"] < 0.9:
                    intent["authorizes_deletion"] = vote["authorizes_deletion"]
                    intent["decided_by"] = "llm"
                else:
                    intent["decided_by"] = "rules"
            if self.llm is not None and intent["confidence"] < 0.9:
                verdict = self.llm.classify(r["request_text"])
                if verdict is not None:
                    intent = dict(intent, authorizes_deletion=bool(verdict.get("authorizes_deletion")),
                                  llm_reason=verdict.get("reason"), confidence=0.9)
            r["_intent"] = intent
            r["_authorized"] = intent["authorizes_deletion"]
            r["user_id"] = self.identities.resolve_email(r["email"])

    def audit(self, request: dict, workflow_of: str | None = None) -> tuple[str, list[dict], CaseAssessment]:
        if "_authorized" not in request:
            self._prepare([request])
        if not request["_authorized"] or request["user_id"] is None:
            a = CaseAssessment(request["case_id"], request.get("user_id"), False, [],
                               ["request does not authorise deletion"])
            return "NOT_APPLICABLE", [], a
        assessment = self.reasoner.assess(request, self.workflow.get(workflow_of or request["case_id"], []))
        status, steps = plan(assessment)
        return status, steps, assessment

    def run(self, requests: list[dict]) -> list[dict]:
        rows = []
        for r in sorted(requests, key=lambda r: r["case_id"]):
            status, steps, _ = self.audit(r)
            rows.append({"case_id": r["case_id"], "deletion_status": status, "steps_json": serialize(steps)})
        return rows
