"""Deletion-state reconstruction.

For every authorised case this module answers three questions:

1. Which resources belong to the deletion subject?
   - resources whose evidence carries one of the subject's current or historical
     identifiers (email, phone, email hash, stable user_id), observed during the
     request lifecycle;
   - resources linked by the data catalog (LINK_RESOURCE_IDENTITY activities);
   - resources the deletion workflow itself touched for this request. Workflow
     activity carries no subject identifier, so it is attributed with a small
     constraint solver: the activity must fall inside the request's workflow
     window around `reported_at`, the account deletion result must agree with the
     workflow's `reported_status` claim, a resource linked by evidence to another
     person is excluded, and a case receives at most one resource of each kind.
2. What state was each resource in at `audit_at`? Only evidence available at or
   before `audit_at` counts; workflow results are claims and are used only when no
   evidence observed the resource.
3. Which policy rule applies, and is the state compliant?
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .common import Observation, default_category, parse_ts, system_of
from .evidence import EvidenceStore
from .identity import IdentityIndex

WINDOW_BEFORE = timedelta(minutes=95)
WINDOW_AFTER = timedelta(minutes=145)

WORKFLOW_EVENTS = {
    "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
    "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS", "RETAIN_RECORD",
}
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}

# deletion_rules.csv data category -> required action
CATEGORY_ACTION_DEFAULT = {
    "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
    "CACHE_ENTRY": "DELETE", "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE",
    "ANALYTICS_PROFILE": "ANONYMIZE", "PRODUCT_TELEMETRY": "ANONYMIZE", "USER_EXPORT": "DELETE",
    "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT", "SECURITY_AUDIT_LOG": "RETAIN",
    "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "BLOCK", "ACCOUNT_TOMBSTONE": "REDACT",
    "DATA_FLOW": "DISABLE",
}
EVENT_ACTION = {
    "DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE",
    "DISABLE_DATA_FLOW": "DISABLE", "CHECK_DATA_FLOW": "DISABLE", "BLOCK_RESTORE": "BLOCK",
    "CHECK_RESTORE_ACCESS": "BLOCK", "RETAIN_RECORD": "RETAIN",
}


def resource_kind(resource_id: str) -> str:
    return re.sub(r"_\d+$", "", resource_id)


@dataclass
class ResourceAssessment:
    resource_id: str
    system: str
    category: str
    action: str  # DELETE | REDACT | ANONYMIZE | RETAIN | DISABLE | BLOCK
    state: str  # PRESENT | ABSENT | ACTIVE | INACTIVE
    pii: bool | None
    evidence_id: str
    basis: str  # evidence | workflow_claim | default
    reasons: list[str] = field(default_factory=list)

    @property
    def compliant(self) -> bool:
        if self.action == "DELETE":
            return self.state == "ABSENT"
        if self.action in ("REDACT", "ANONYMIZE"):
            return self.state == "ABSENT" or not self.pii
        if self.action == "RETAIN":
            return True
        if self.action in ("DISABLE", "BLOCK"):
            return self.state == "INACTIVE"
        return True


@dataclass
class CaseAssessment:
    case_id: str
    user_id: str | None
    authorized: bool
    resources: list[ResourceAssessment]
    notes: list[str] = field(default_factory=list)


class Reasoner:
    def __init__(self, dataset, evidence: EvidenceStore, identities: IdentityIndex):
        self.ds = dataset
        self.ev = evidence
        self.idx = identities
        self.activities = dataset.activities
        for a in self.activities:
            a["_t"] = parse_ts(a["event_time"])
        self.obs_by_resource: dict[str, list[Observation]] = defaultdict(list)
        for o in evidence.observations:
            self.obs_by_resource[o.resource_id].append(o)
        # Static resource -> person links from evidence identifiers and the data catalog.
        self.owners: dict[str, set[str]] = defaultdict(set)
        for o in evidence.observations:
            self.owners[o.resource_id] |= identities.owners_of(o.ids)
        self.catalog_links: list[dict] = []
        for a in self.activities:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                users = set(re.findall(r"user_\d+", a["details"].split(" to ", 1)[-1]))
                emails = re.findall(r"[\w.+\-]+@[\w.\-]+\.\w+", a["details"])
                for e in emails:
                    users |= identities.owners_of({"email:" + e.lower()})
                self.owners[a["resource_id"]] |= users
                self.catalog_links.append({"activity": a, "users": users})
        self.workflow_by_resource: dict[str, list[dict]] = defaultdict(list)
        for a in self.activities:
            if a["source_system"] == "deletion_worker" and a["event_type"] in WORKFLOW_EVENTS:
                self.workflow_by_resource[a["resource_id"]].append(a)

    # ------------------------------------------------------------------ workflow attribution
    @staticmethod
    def _phase_offset(a: dict) -> int:
        """Seconds offset of an event class within one request's workflow run.

        One request's workflow run emits its events on a shared clock: measured from
        the account deletion, "policy action completed" events land ~10 s later
        (mod 30 s) and "final result" / status-check events ~21 s later. This
        regularity is learned from activities whose resources the evidence links to
        a known person, and is used only to attribute activities with no identifiers.
        """
        details = a["details"]
        if a["resource_id"].startswith("account_") and a["event_type"] == "DELETE_RECORD":
            return 0
        if "Policy action completed" in details:
            return 9.9
        return 21.4

    @staticmethod
    def _phase(a: dict) -> float:
        return (a["_t"].timestamp() - Reasoner._phase_offset(a)) % 30

    @staticmethod
    def _phase_close(p: float, q: float, tol: float = 1.4) -> bool:
        d = abs(p - q) % 30
        return min(d, 30 - d) <= tol

    def attribute_workflow(self, cases: list[dict]) -> dict[str, list[dict]]:
        """Assign deletion-worker activities to cases. Returns case_id -> activities."""
        for c in cases:
            c["_rep"] = parse_ts(c["reported_at"])
            c["_num"] = int(re.sub(r"\D", "", c["case_id"]) or 0)
        assigned: dict[str, list[dict]] = defaultdict(list)
        kinds_taken: dict[str, set[str]] = defaultdict(set)
        phases: dict[str, list[float]] = defaultdict(list)
        pending: list[tuple[dict, list[dict]]] = []

        def take(c, a):
            assigned[c["case_id"]].append(a)
            kinds_taken[c["case_id"]].add(resource_kind(a["resource_id"]))
            phases[c["case_id"]].append(self._phase(a))

        for a in self.activities:
            if a["source_system"] != "deletion_worker" or a["event_type"] not in WORKFLOW_EVENTS:
                continue
            t = a["_t"]
            cands = [c for c in cases if c["_rep"] - WINDOW_BEFORE <= t <= c["_rep"] + WINDOW_AFTER]
            rid = a["resource_id"]
            if rid.startswith("account_") and "submitted primary profile" in a["details"]:
                # The workflow status claim is derived from the primary-profile deletion result.
                cands = [c for c in cands if STATUS_RESULT.get(c["reported_status"]) == a["reported_result"]]
            elif "reported the requested operation" in a["details"] or "stopped after" in a["details"]                     or "completed some internal work" in a["details"]:
                # Direct worker reports are emitted at the moment the request status is reported.
                close = [c for c in cands if abs((t - c["_rep"]).total_seconds()) <= 15 * 60]
                cands = close or cands
            owners = self.owners.get(rid, set())
            if owners:
                for c in cands:
                    if c["user_id"] in owners:
                        take(c, a)
                continue
            if cands:
                pending.append((a, cands))

        def viable(a, cands):
            kind = resource_kind(a["resource_id"])
            p = self._phase(a)
            live = [c for c in cands if kind not in kinds_taken[c["case_id"]]]
            if len(cands) == 1:
                return live, []  # uniquely placed by time alone; no clock check needed
            matched = [c for c in live if phases[c["case_id"]] and
                       self._phase_close(p, sorted(phases[c["case_id"]])[len(phases[c["case_id"]]) // 2])]
            unknown = [c for c in live if not phases[c["case_id"]]]
            return matched, unknown

        # Constraint propagation: kind exclusivity + workflow-clock phase consistency.
        changed = True
        while changed and pending:
            changed = False
            rest = []
            for a, cands in pending:
                matched, unknown = viable(a, cands)
                if len(matched) == 1:
                    take(matched[0], a)
                    changed = True
                elif not matched and len(unknown) == 1:
                    take(unknown[0], a)
                    changed = True
                elif matched or unknown:
                    rest.append((a, cands))
            pending = rest
        # Remaining ties: solve each connected group of tied activities jointly. Every
        # assignment that respects kind exclusivity is scored by how many same-case event
        # pairs agree on the workflow clock; ties fall back to chronological / case order.
        self.tie_log = []
        self.unresolved_ties = len(pending)
        groups = self._tie_groups(pending)
        for group in groups:
            best = self._solve_group(group, kinds_taken, phases)
            for a, c in best:
                if c is not None:
                    self.tie_log.append((a["activity_id"], a["resource_id"], c["case_id"]))
                    take(c, a)
        return assigned

    def _tie_groups(self, pending):
        """Group tied activities that share candidate cases (union-find over case ids)."""
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, cands in pending:
            ids = [c["case_id"] for c in cands]
            for other in ids[1:]:
                parent[find(ids[0])] = find(other)
            find(ids[0])
        groups: dict[str, list] = defaultdict(list)
        for a, cands in pending:
            groups[find(cands[0]["case_id"])].append((a, cands))
        return [sorted(g, key=lambda x: (x[0]["_t"], x[0]["activity_id"])) for g in groups.values()]

    def _solve_group(self, group, kinds_taken, phases, limit=200000):
        import itertools

        options = []
        for a, cands in group:
            kind = resource_kind(a["resource_id"])
            opts = sorted((c for c in cands if kind not in kinds_taken[c["case_id"]]), key=lambda c: c["_num"])
            options.append(opts + [None])
        total = 1
        for o in options:
            total *= len(o)
        if total > limit:  # too large to enumerate: greedy chronological fallback
            out, used = [], defaultdict(set)
            for (a, _), opts in zip(group, options):
                kind = resource_kind(a["resource_id"])
                pick = next((c for c in opts if c is not None and kind not in used[c["case_id"]]), None)
                if pick is not None:
                    used[pick["case_id"]].add(kind)
                out.append((a, pick))
            return out
        best, best_key = None, None
        acts = [a for a, _ in group]
        aphase = [self._phase(a) for a in acts]
        for combo in itertools.product(*options):
            used = set()
            ok = True
            for a, c in zip(acts, combo):
                if c is None:
                    continue
                key = (c["case_id"], resource_kind(a["resource_id"]))
                if key in used:
                    ok = False
                    break
                used.add(key)
            if not ok:
                continue
            agree = 0
            per_case = defaultdict(list)
            for p, c in zip(aphase, combo):
                if c is not None:
                    per_case[c["case_id"]].append(p)
            for cid, ps in per_case.items():
                anchors = phases[cid] + ps
                for i, p in enumerate(ps):
                    agree += sum(1 for q in anchors if q is not p and self._phase_close(p, q)) > 0
                    agree -= sum(1 for q in anchors if q is not p and not self._phase_close(p, q)) > 0
            assigned_n = sum(c is not None for c in combo)
            order = tuple(c["_num"] if c is not None else 10 ** 9 for c in combo)
            key = (agree, assigned_n, tuple(-x for x in order))
            if best_key is None or key > best_key:
                best, best_key = combo, key
        return list(zip(acts, best))

    # ------------------------------------------------------------------ per-case reconstruction
    def assess(self, case: dict, workflow: list[dict]) -> CaseAssessment:
        uid = case["user_id"]
        req = parse_ts(case["requested_at"])
        audit = parse_ts(case["audit_at"])
        tokens = self.idx.tokens.get(uid, set())
        notes: list[str] = []

        relevant: dict[str, str] = {}  # resource -> how it was linked
        for a in workflow:
            relevant.setdefault(a["resource_id"], f"workflow:{a['activity_id']}")
        for rid, obs in self.obs_by_resource.items():
            # Identity link: any evidence that observed the record carrying one of the subject's
            # identifiers, up to the audit cutoff (a record can be linked through an old phone
            # number captured before the request).
            linked = [o for o in obs if o.ids & tokens and o.observed_at <= audit]
            if not linked:
                continue
            # Lifecycle: the record must have been observed (linked or not) during this request,
            # otherwise it belongs to an earlier request of the same person.
            during = [o for o in obs if req <= o.observed_at <= audit]
            if not during:
                continue
            if any(o.available_at <= audit for o in during):
                relevant.setdefault(rid, f"evidence:{linked[-1].evidence_id}")
            elif default_category(rid) in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
                # A policy-retained record identified only by a later capture still has to
                # be verified as present after remediation.
                relevant.setdefault(rid, f"evidence(later):{linked[-1].evidence_id}")
        # Screenshots list many records; when the data catalog names the subject's record of a
        # kind, other records of that kind seen only on a screenshot are context, not subject data.
        catalog_kinds = {resource_kind(l["activity"]["resource_id"]) for l in self.catalog_links if uid in l["users"]}
        for rid in list(relevant):
            if relevant[rid].startswith("evidence") and resource_kind(rid) in catalog_kinds:
                srcs = {o.store for o in self.obs_by_resource.get(rid, ()) if o.ids & tokens}
                if srcs == {"media"} and not any(rid == l["activity"]["resource_id"] for l in self.catalog_links):
                    del relevant[rid]
        for link in self.catalog_links:
            a = link["activity"]
            if uid in link["users"] and req <= a["_t"] <= audit:
                relevant.setdefault(a["resource_id"], f"catalog:{a['activity_id']}")

        if uid and not any(r.startswith("account_") for r in relevant):
            # No workflow activity or evidence named the primary profile. Account resource
            # IDs mirror the stable user_id where the evidence shows both (e.g. a replica row
            # account_005 / user_05), so derive the profile ID from the user reference.
            relevant[self.account_from_user(uid)] = "derived:user_id"

        # Evidence files that observed this case's resources (used for fallback citations).
        case_files: dict[str, datetime] = {}
        for rid in relevant:
            for o in self.obs_by_resource.get(rid, ()):
                if o.available_at <= audit and o.observed_at >= req:
                    case_files[o.evidence_id] = o.available_at

        out: list[ResourceAssessment] = []
        for rid, how in sorted(relevant.items()):
            out.append(self._assess_resource(rid, how, case, workflow, req, audit, case_files))
        return CaseAssessment(case["case_id"], uid, True, out, notes)

    @staticmethod
    def account_from_user(uid: str) -> str:
        return "account_%03d" % int(uid.split("_")[1])

    def _assess_resource(self, rid, how, case, workflow, req, audit, case_files) -> ResourceAssessment:
        system = system_of(rid)
        acts = [a for a in workflow if a["resource_id"] == rid]
        obs = [o for o in self.obs_by_resource.get(rid, ())
               if o.available_at <= audit and o.observed_at >= req]

        # --- category / required action
        category = None
        history = [o for o in self.obs_by_resource.get(rid, ()) if o.available_at <= audit]
        for o in sorted(history, key=lambda o: o.observed_at, reverse=True):
            if o.category:
                category = o.category
                break
        if category is None:
            category = self._media_category(rid, history) or default_category(rid)
        action = CATEGORY_ACTION_DEFAULT.get(category, "DELETE")
        explicit_policy = [a for a in acts if a["event_type"] in ("DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD")]
        if explicit_policy:
            # The workflow's chosen treatment reflects how policy classified this copy
            # (e.g. a contact record inside the analytics store that must be deleted).
            action = EVENT_ACTION[explicit_policy[-1]["event_type"]]
        if category == "SECURITY_AUDIT_LOG":
            until = self.ev.retention.get(rid)
            action = "RETAIN" if until is None or until >= audit else "DELETE"
        if rid.startswith(("data_flow_",)):
            action = "DISABLE"
        if rid.startswith(("backup_",)):
            action = "BLOCK"
        if rid.startswith(("legal_hold_",)):
            action = "RETAIN"

        # --- state at audit_at
        reasons = [how]
        if obs:
            latest_t = max(o.observed_at for o in obs)
            last = [o for o in obs if o.observed_at == latest_t]
            # Several stores in one capture (primary + replicas): any live copy counts.
            state, pii = self._merge(last)
            cite = sorted(last, key=lambda o: (o.available_at, o.evidence_id))[-1].evidence_id
            basis = "evidence"
            reasons.append(f"latest evidence {cite} at {latest_t.isoformat()}")
        else:
            state, pii, cite, basis = self._from_workflow(rid, acts, action)
            if not cite:
                cite = self._fallback_citation(system, audit, req, case_files)
            reasons.append(f"no evidence observation; {basis}")
        if state == "PRESENT" and pii is None:
            pii = action not in ("REDACT", "ANONYMIZE") or self._pii_from_claims(acts)
        return ResourceAssessment(rid, system, category, action, state, pii, cite, basis, reasons)

    @staticmethod
    def _merge(obs: list[Observation]) -> tuple[str, bool | None]:
        states = {o.state for o in obs}
        if "ACTIVE" in states:
            return "ACTIVE", None
        if "INACTIVE" in states:
            return "INACTIVE", None
        if "PRESENT" in states:
            piis = [o.pii for o in obs if o.state == "PRESENT"]
            if any(p is True for p in piis):
                return "PRESENT", True
            if any(p is False for p in piis):
                return "PRESENT", False
            return "PRESENT", None
        return "ABSENT", None

    @staticmethod
    def _pii_from_claims(acts) -> bool:
        ok = [a for a in acts if a["event_type"] in ("REDACT_RECORD", "ANONYMIZE_RECORD")]
        return not (ok and ok[-1]["reported_result"] == "SUCCESS")

    def _from_workflow(self, rid, acts, action):
        """No evidence observed the resource: fall back to what the workflow claims."""
        if rid.startswith("account_") or not acts:
            return "ABSENT", None, "", "default:not observed"
        last = acts[-1]
        ok = last["reported_result"] == "SUCCESS"
        et = last["event_type"]
        if et == "CHECK_DATA_FLOW":
            return ("ACTIVE" if "remains enabled" in last["details"] else "INACTIVE"), None, last["activity_id"], "workflow_check"
        if et == "CHECK_RESTORE_ACCESS":
            return ("ACTIVE" if "can still be restored" in last["details"] else "INACTIVE"), None, last["activity_id"], "workflow_check"
        if action in ("DISABLE", "BLOCK"):
            return ("INACTIVE" if ok else "ACTIVE"), None, "" if ok else last["activity_id"], "workflow_claim"
        if action == "RETAIN":
            return "PRESENT", None, "", "workflow_claim"
        if action == "DELETE":
            return ("ABSENT" if ok else "PRESENT"), (None if ok else True), "" if ok else last["activity_id"], "workflow_claim"
        return "PRESENT", (not ok), "" if ok else last["activity_id"], "workflow_claim"

    def _fallback_citation(self, system, audit, req, case_files) -> str:
        """Cite the capture that shows the system without this record."""
        best = None
        for fid, at in case_files.items():
            if self._covers(fid, system) and (best is None or at > best[0]):
                best = (at, fid)
        if best:
            return best[1]
        for o in self.ev.observations:
            # Only a capture of the system's own data store can show a record is missing;
            # cross-service lookup ledgers and logs only describe the records they list.
            if o.system == system and o.available_at <= audit and o.store != "log"                     and o.source not in ("ledger", "log_ledger", "handover"):
                if best is None or o.available_at > best[0]:
                    best = (o.available_at, o.evidence_id)
        return best[1] if best else ""

    def _covers(self, fid, system) -> bool:
        if system in self.ev.coverage.get(fid, {}):
            return True
        return any(o.evidence_id == fid and o.system == system for o in self.ev.observations)

    def _media_category(self, rid, obs) -> str | None:
        for o in obs:
            if o.store != "media":
                continue
            text = self.ev.file_texts.get(o.evidence_id, "").lower()
            if rid.startswith(("support_", "object_")):
                if any(w in text for w in ("attachment", "uploader", ".pdf", "file ")):
                    if "conversation" not in text:
                        return "SUPPORT_ATTACHMENT"
                return "SUPPORT_MESSAGE"
        return None
