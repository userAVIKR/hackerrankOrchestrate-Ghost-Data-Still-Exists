"""Evidence extraction: every participant-facing evidence file -> Observations.

Each parser turns one evidence shape (JSON snapshot bundles, ledgers, service
exports, five log dialects, SQLite dumps, the handover text, PDFs and screenshots)
into a flat list of `Observation`s: "resource R in system S was in state X at time T,
and the record carried identifiers I". Linking an observation to a deletion subject
happens later, in the reasoner, by matching identifier tokens.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from collections import defaultdict

from .common import (
    DATASET_DIR,
    EMAIL_RE,
    RESOURCE_RE,
    USER_RE,
    Observation,
    identifier_tokens,
    is_null,
    norm_name,
    parse_ts,
    system_of,
)
from . import media

LEDGER_STATE = {
    "FOUND": ("PRESENT", None),
    "NOT_FOUND": ("ABSENT", None),
    "IDENTIFIABLE": ("PRESENT", True),
    "ANONYMIZED": ("PRESENT", False),
    "REDACTED": ("PRESENT", False),
    "ACTIVE": ("ACTIVE", None),
    "DISABLED": ("INACTIVE", None),
    "BLOCKED": ("INACTIVE", None),
}

PII_FIELDS = (
    "email", "phone", "email_hash", "full_name", "name", "display_name", "user_id",
    "customer_reference", "requester_email", "requester_name", "requester_phone",
    "owner_email", "owner_phone", "customer_email", "uploaded_by",
)
# Fields whose presence means a *direct identifier* is exposed (customer_reference /
# user_id alone is a stable internal reference, but in profile-style records it still
# makes the record attributable; see `_has_pii`).
DIRECT_FIELDS = (
    "email", "phone", "email_hash", "full_name", "name", "display_name",
    "requester_email", "requester_name", "requester_phone", "owner_email", "owner_phone",
    "customer_email", "uploaded_by",
)


def _bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def _record_ids(record: dict) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    names: set[str] = set()
    for key, value in record.items():
        if isinstance(value, dict):
            sub_ids, sub_names = _record_ids(value)
            ids |= sub_ids
            names |= sub_names
            continue
        if key in ("resource_id", "archive_key", "archive_path", "object_key", "file_name", "integrity_seal"):
            continue
        if isinstance(value, (list, int, float)) or value is None:
            continue
        ids |= identifier_tokens(value)
        if key in ("full_name", "name", "display_name", "requester_name") and not is_null(value):
            names.add(norm_name(value))
    return ids, names


def _has_pii(record: dict, include_reference: bool) -> bool:
    for key in DIRECT_FIELDS:
        if key in record and not is_null(record[key]):
            return True
    if include_reference:
        for key in ("user_id", "customer_reference"):
            if key in record and not is_null(record[key]):
                return True
    text = record.get("message_text") or record.get("message_excerpt")
    if text and not is_null(text) and EMAIL_RE.search(str(text)):
        return True
    doc = record.get("document") or record.get("profile")
    if isinstance(doc, dict):
        return _has_pii(doc, include_reference)
    return False


class EvidenceStore:
    def __init__(self, dataset_dir: str = DATASET_DIR):
        self.dataset_dir = dataset_dir
        self.observations: list[Observation] = []
        self.manifest: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}  # display alias (e.g. invoice no.) -> resource_id
        self.file_texts: dict[str, str] = {}
        self.retention: dict = {}  # audit resource_id -> retention end (rule_12 condition)
        self.coverage: dict = {}  # file_id -> {system: captured_at} for SQL/inventory captures

    # ------------------------------------------------------------------ helpers
    def _add(self, evidence_id, available_at, observed_at, resource_id, state, pii=None,
             category=None, ids=None, store="primary", source="", names=None, system=None):
        if not resource_id or not RESOURCE_RE.fullmatch(resource_id):
            return
        self.observations.append(
            Observation(
                evidence_id=evidence_id,
                available_at=available_at,
                observed_at=observed_at or available_at,
                system=system or system_of(resource_id),
                resource_id=resource_id,
                state=state,
                pii=pii,
                category=category,
                ids=set(ids or ()),
                store=store,
                source=source,
                name_tokens=set(names or ()),
            )
        )

    # ------------------------------------------------------------------ loading
    def load(self, activities: list[dict]):
        for row in activities:
            if row["event_type"] == "LINK_RESOURCE_IDENTITY":
                m = re.search(r"linked (\S+) \(([^)]+)\)|confirmed (\S+) \(([^)]+)\)", row["details"])
                if m:
                    res = m.group(1) or m.group(3)
                    alias = m.group(2) or m.group(4)
                    self.aliases[alias] = res
        with open(os.path.join(self.dataset_dir, "evidence_files.csv"), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self.manifest[row["file_id"]] = row
        for file_id, row in self.manifest.items():
            path = os.path.join(self.dataset_dir, row["file_path"])
            if not os.path.exists(path):
                continue
            at = parse_ts(row["captured_at"])
            ext = os.path.splitext(path)[1].lower()
            try:
                if ext == ".json":
                    self._parse_json(file_id, at, path)
                elif ext == ".log":
                    self._parse_log(file_id, at, path)
                elif ext == ".sql":
                    self._parse_sql(file_id, at, path)
                elif ext == ".txt":
                    self._parse_text_ledger(file_id, at, path)
                elif ext == ".pdf":
                    self._parse_media(file_id, at, media.pdf_text(path), "pdf")
                elif ext in (".png", ".jpg", ".jpeg"):
                    self._parse_media(file_id, at, media.image_text(path), "image")
            except Exception as exc:  # keep going; one bad file must not sink the run
                print(f"[warn] failed to parse {row['file_path']}: {exc}")
        return self

    # ------------------------------------------------------------------ JSON
    def _parse_json(self, fid, at, path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if "snapshots" in data:
            for snap in data["snapshots"]:
                when = parse_ts(snap.get("captured_at")) or at
                if snap.get("data_store"):
                    self.coverage.setdefault(fid, {})[snap["data_store"]] = when
                for rec in snap.get("records", []):
                    self._json_record(fid, at, when, snap.get("data_store"), snap.get("collection"), rec)
        elif "service_exports" in data:
            for system, sections in data["service_exports"].items():
                for section, records in sections.items():
                    for rec in records:
                        when = parse_ts(rec.get("observed_at")) or at
                        self._service_export_record(fid, at, when, system, section, rec)
        elif "records" in data:
            source = data.get("source") or data.get("origin") or ""
            collection = data.get("collection") or ""
            for rec in data["records"]:
                when = parse_ts(rec.get("recorded_at") or rec.get("captured_at")) or at
                self._json_record(fid, at, when, source, collection, rec)

    def _ledger(self, fid, at, when, rec, source):
        state, pii = LEDGER_STATE.get(rec.get("result", ""), ("PRESENT", None))
        subject = rec.get("subject_reference") or rec.get("lookup_value") or ""
        category = rec.get("record_type")
        if pii is None and state == "PRESENT" and category in (
            "USER_PROFILE", "SEARCH_INDEX_ENTRY", "USER_EXPORT", "SUPPORT_ATTACHMENT", "MARKETING_PROFILE"):
            pii = True
        self._add(fid, at, when, rec.get("resource_id"), state, pii, category,
                  identifier_tokens(subject), source=source, system=rec.get("system"))

    def _json_record(self, fid, at, when, store, collection, rec):
        rid = rec.get("resource_id")
        if "record_type" in rec and "result" in rec:
            self._ledger(fid, at, when, rec, "ledger")
            return
        ids, names = _record_ids(rec)
        if rid is None:
            return
        if rid.startswith("account_"):
            self._add(fid, at, when, rid, "PRESENT", _has_pii(rec, True), "USER_PROFILE", ids,
                      source="profile_snapshot", names=names)
        elif rid.startswith("analytics_"):
            self._add(fid, at, when, rid, "PRESENT", _has_pii(rec, True), "ANALYTICS_PROFILE", ids,
                      source="analytics_snapshot", names=names)
        elif rid.startswith("backup_"):
            allowed = _bool(rec.get("restore_allowed"))
            self._add(fid, at, when, rid, "ACTIVE" if allowed else "INACTIVE", None, "BACKUP_ARCHIVE", ids,
                      source="backup_catalog")
        elif rid.startswith("export_"):
            available = _bool(rec.get("download_available"))
            self._add(fid, at, when, rid, "PRESENT" if available is not False else "ABSENT", True,
                      "USER_EXPORT", ids, source="export_jobs")
        elif rid.startswith("data_flow_"):
            enabled = _bool(rec.get("enabled"))
            self._add(fid, at, when, rid, "ACTIVE" if enabled else "INACTIVE", None, "DATA_FLOW", ids,
                      source="connector_config")
        elif rid.startswith("search_"):
            self._add(fid, at, when, rid, "PRESENT", _has_pii(rec, True), "SEARCH_INDEX_ENTRY", ids,
                      source="search_snapshot", names=names)
        elif rid.startswith("audit_record_"):
            self._add(fid, at, when, rid, "PRESENT", None, "SECURITY_AUDIT_LOG", ids, source="audit_snapshot")
        elif rid.startswith("support_"):
            if "file_name" in rec or collection == "ticket_attachments":
                self._add(fid, at, when, rid, "PRESENT", True, "SUPPORT_ATTACHMENT", ids, source="ticket_attachment")
            else:
                self._add(fid, at, when, rid, "PRESENT", _has_pii(rec, False), "SUPPORT_MESSAGE", ids,
                          source="support_conversation", names=names)
        elif rid.startswith("legal_hold_"):
            self._add(fid, at, when, rid, "PRESENT", None, "LEGAL_HOLD_RECORD", ids, source="legal_register")
        elif rid.startswith("tombstone_"):
            self._add(fid, at, when, rid, "PRESENT", _has_pii(rec, False), "ACCOUNT_TOMBSTONE", ids,
                      source="tombstone", names=names)
        else:
            self._add(fid, at, when, rid, "PRESENT", _has_pii(rec, True), None, ids, source=collection or "record",
                      names=names)

    def _service_export_record(self, fid, at, when, system, section, rec):
        rid = rec.get("resource_id")
        if section == "profile_lookups":
            profile = rec.get("profile")
            if rec.get("status_code") == 404 or profile is None:
                self._add(fid, at, when, rid, "ABSENT", None, "USER_PROFILE", set(), source="profile_lookup")
            else:
                ids, names = _record_ids(profile)
                self._add(fid, at, when, rid, "PRESENT", _has_pii(profile, True), "USER_PROFILE", ids,
                          source="profile_lookup", names=names)
        elif section == "index_queries":
            docs = (rec.get("hits") or {}).get("documents") or []
            query_ids = identifier_tokens(rec.get("query"))
            if not docs:
                self._add(fid, at, when, rid, "ABSENT", None, "SEARCH_INDEX_ENTRY", query_ids, source="index_query")
            for doc in docs:
                ids, names = _record_ids(doc)
                self._add(fid, at, when, doc.get("resource_id") or rid, "PRESENT", _has_pii(doc, True),
                          "SEARCH_INDEX_ENTRY", ids, source="index_query", names=names)
        elif section == "restore_entries":
            ids, _ = _record_ids(rec)
            state = "ACTIVE" if _bool(rec.get("restore_allowed")) else "INACTIVE"
            self._add(fid, at, when, rid, state, None, "BACKUP_ARCHIVE", ids, source="restore_entry")
        elif section == "connectors":
            ids, _ = _record_ids(rec)
            state = "ACTIVE" if _bool(rec.get("enabled")) else "INACTIVE"
            self._add(fid, at, when, rid, state, None, "DATA_FLOW", ids, source="connector")
        elif section == "audit_records":
            ids, _ = _record_ids(rec)
            self._add(fid, at, when, rid, "PRESENT", None, "SECURITY_AUDIT_LOG", ids, source="audit_record",
                      )
            self._retention(rid, rec.get("retention_until"))
        else:
            self._json_record(fid, at, when, system, section, rec)

    def _retention(self, rid, until):
        ts = parse_ts(until)
        if rid and ts:
            self.retention.setdefault(rid, ts)

    # ------------------------------------------------------------------ SQL
    def _parse_sql(self, fid, at, path):
        with open(path, encoding="utf-8") as fh:
            script = fh.read()
        m = re.search(r"Captured at:\s*(\S+)", script)
        snap_at = parse_ts(m.group(1)) if m else at
        con = sqlite3.connect(":memory:")
        con.executescript(script)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        scope = {}
        if "snapshot_scope" in tables:
            scope = {r[0]: parse_ts(r[1]) for r in con.execute("SELECT origin_system, captured_at FROM snapshot_scope")}
        for table in tables:
            if table == "snapshot_scope":
                continue
            cur = con.execute(f'SELECT * FROM "{table}"')
            cols = [d[0] for d in cur.description]
            store = "primary"
            for marker in ("failover_replica", "read_replica", "reporting_replica", "replica_ap_south",
                           "replica_eu_west", "replica"):
                if table.endswith(marker) or f"_{marker}" in table:
                    store = marker
                    break
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                when = parse_ts(rec.get("updated_at")) or snap_at
                if "record_json" in rec:
                    inner = json.loads(rec["record_json"])
                    self._sql_record(fid, at, snap_at, store, inner, rec.get("collection_name"))
                else:
                    self._sql_record(fid, at, snap_at, store, rec, table)
        # Inventory captures list the systems they cover; a linked resource of a covered
        # system that is missing from the capture is treated as absent by the reasoner.
        if scope:
            self.coverage.setdefault(fid, {}).update({sysname: ts for sysname, ts in scope.items()})
        else:
            systems = set()
            for o in self.observations:
                if o.evidence_id == fid:
                    systems.add(o.system)
            self.coverage.setdefault(fid, {}).update({s: snap_at for s in systems})
        con.close()

    def _sql_record(self, fid, at, when, store, rec, table):
        rid = rec.get("resource_id")
        if not rid:
            return
        if rid.startswith("backup_") and "restore_allowed" in rec:
            ids, _ = _record_ids(rec)
            self._add(fid, at, when, rid, "ACTIVE" if _bool(rec["restore_allowed"]) else "INACTIVE", None,
                      "BACKUP_ARCHIVE", ids, store=store, source="sql")
        elif rid.startswith("data_flow_") and "enabled" in rec:
            ids, _ = _record_ids(rec)
            self._add(fid, at, when, rid, "ACTIVE" if _bool(rec["enabled"]) else "INACTIVE", None,
                      "DATA_FLOW", ids, store=store, source="sql")
        elif rid.startswith("audit_record_"):
            ids, _ = _record_ids(rec)
            self._add(fid, at, when, rid, "PRESENT", None, "SECURITY_AUDIT_LOG", ids, store=store, source="sql")
            self._retention(rid, rec.get("retention_until"))
        elif rid.startswith("support_") and ("message_excerpt" in rec or "message_text" in rec or "requester_email" in rec):
            ids, names = _record_ids(rec)
            self._add(fid, at, when, rid, "PRESENT", _has_pii(rec, False), "SUPPORT_MESSAGE", ids,
                      store=store, source="sql", names=names)
        else:
            # Generic record: reuse the JSON record classifier, then tag the store.
            before = len(self.observations)
            self._json_record(fid, at, when, store, table, rec)
            for o in self.observations[before:]:
                o.store = store
                o.source = "sql"

    # ------------------------------------------------------------------ text ledger
    def _parse_text_ledger(self, fid, at, path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = [p.strip() for p in line.strip().lstrip("-").split("|")]
                if len(parts) == 6 and RESOURCE_RE.fullmatch(parts[2]):
                    rec = {"recorded_at": parts[0], "system": parts[1], "resource_id": parts[2],
                           "record_type": parts[3], "result": parts[4], "subject_reference": parts[5]}
                    self._ledger(fid, at, parse_ts(parts[0]) or at, rec, "handover")

    # ------------------------------------------------------------------ logs
    KV_RE = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')
    TRACE_RE = re.compile(r"\b((?:tr|rb)-[\w-]+)\b")
    TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
    KEY_FIELDS = ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref",
                  "correlation_key", "matched_value")

    def _log_fields(self, line: str) -> dict:
        fields: dict = {}
        brace = line.find("{")
        if brace != -1 and line.rstrip().endswith("}"):
            try:
                fields.update(json.loads(line[brace:]))
            except json.JSONDecodeError:
                pass
        for key, quoted, bare in self.KV_RE.findall(line):
            value = quoted if quoted or bare == "" else bare
            value = value.replace('\\"', '"')
            fields.setdefault(key, value)
            if key == "message" and "=" in value:
                for k2, q2, b2 in self.KV_RE.findall(value.replace('\\"', '"')):
                    fields.setdefault(k2, q2 if q2 or b2 == "" else b2)
        # bracket / pipe / syslog dialects carry the event name positionally
        m = re.search(r"\]\s+(\w+):", line) or re.search(r"\|\s*(\w+)\s*\|\s*\w+=", line)
        if m and "event" not in fields:
            fields["event"] = m.group(1)
        m = self.TS_RE.match(line)
        if m:
            fields["_ts"] = m.group(1)
        m = self.TRACE_RE.search(line)
        if m:
            fields["_trace"] = m.group(1)
        if "trace_id" in fields:
            fields["_trace"] = fields["trace_id"]
        return fields

    def _parse_log(self, fid, at, path):
        with open(path, encoding="utf-8") as fh:
            lines = [self._log_fields(l) for l in fh if l.strip()]
        trace_keys: dict[str, set[str]] = defaultdict(set)
        for f in lines:
            tr = f.get("_trace")
            if not tr:
                continue
            for key in self.KEY_FIELDS:
                if key in f:
                    trace_keys[tr] |= identifier_tokens(f[key])
        for f in lines:
            rid = f.get("resource_id")
            event = f.get("event", "")
            when = parse_ts(f.get("_ts")) or at
            if event == "record_lookup":
                self._ledger(fid, at, when, f, "log_ledger")
                continue
            if not rid or not RESOURCE_RE.fullmatch(rid):
                continue
            ids = set(trace_keys.get(f.get("_trace"), set()))
            for key in self.KEY_FIELDS + ("email", "customer_reference", "customer_email", "owner_email",
                                          "requester_email", "requester_phone", "phone", "email_hash"):
                if key in f:
                    ids |= identifier_tokens(f[key])
            service = f.get("service") or f.get("logger") or f.get("function") or ""
            state, pii, cat = None, None, None
            if event == "profile_lookup_completed":
                if service == "profile_service":
                    continue  # profile_service lookups target a different account namespace
                ok = str(f.get("http_status")) == "200" or str(f.get("rows")) not in ("0", "None")
                state, pii, cat = ("PRESENT", True, "USER_PROFILE") if ok else ("ABSENT", None, "USER_PROFILE")
            elif event == "index_query_completed":
                hit = f.get("result", "hit") == "hit"
                state, pii, cat = ("PRESENT", True, "SEARCH_INDEX_ENTRY") if hit else ("ABSENT", None, "SEARCH_INDEX_ENTRY")
            elif event in ("message_pii_scan",):
                state, pii, cat = "PRESENT", True, "SUPPORT_MESSAGE"
            elif event in ("attachment_pii_scan",):
                state, pii, cat = "PRESENT", True, "SUPPORT_ATTACHMENT"
            elif event == "support_ticket_read_completed":
                pii = any(not is_null(f.get(k)) for k in ("requester_name", "requester_email", "requester_phone"))
                state, cat = "PRESENT", "SUPPORT_MESSAGE"
            elif event == "message_read":
                pii = not is_null(f.get("message_text"))
                state, cat = "PRESENT", "SUPPORT_MESSAGE"
            elif event in ("contact_query_completed",):
                state, pii, cat = ("PRESENT" if str(f.get("rows", "1")) != "0" else "ABSENT"), True, "SUPPORT_MESSAGE"
            elif event == "deletion_marker_check":
                state, pii, cat = ("PRESENT" if f.get("marker_found") == "false" else "ABSENT"), True, None
            elif event in ("audit_record_read_completed",):
                state, cat = "PRESENT", "SECURITY_AUDIT_LOG"
            elif event in ("analytics_profile_read", "analytics_profile_read_completed"):
                direct = f.get("direct_identifiers")
                if direct is not None:
                    pii = str(direct) != "0"
                else:
                    pii = any(not is_null(f.get(k)) for k in ("email", "phone", "email_hash", "customer_reference"))
                state, cat = "PRESENT", "ANALYTICS_PROFILE"
            elif event == "analytics_identity_scan":
                state, pii, cat = "PRESENT", True, "ANALYTICS_PROFILE"
            elif event == "contact_lookup_completed":
                state, pii, cat = ("PRESENT" if str(f.get("rows", "1")) != "0" else "ABSENT"), True, None
            elif event in ("connector_config_read_completed", "connector_config_loaded", "connector_heartbeat"):
                state, cat = ("ACTIVE" if _bool(f.get("enabled")) else "INACTIVE"), "DATA_FLOW"
            elif event == "restore_policy_read_completed":
                state, cat = ("ACTIVE" if _bool(f.get("restore_allowed")) else "INACTIVE"), "BACKUP_ARCHIVE"
            elif event == "restore_candidate_evaluated":
                restorable = _bool(f.get("eligible")) and not _bool(f.get("deletion_block"))
                state, cat = ("ACTIVE" if restorable else "INACTIVE"), "BACKUP_ARCHIVE"
            elif event == "restore_authorization_completed":
                state, cat = ("INACTIVE" if f.get("decision") == "deny" else "ACTIVE"), "BACKUP_ARCHIVE"
            elif event in ("export_download_check_completed", "export_metadata_read_completed"):
                state, pii, cat = ("PRESENT" if _bool(f.get("available")) else "ABSENT"), True, "USER_EXPORT"
            else:
                continue
            self._add(fid, at, when, rid, state, pii, cat, ids, store="log", source=f"log:{event}")

    # ------------------------------------------------------------------ PDFs and screenshots
    def _parse_media(self, fid, at, text, kind):
        self.file_texts[fid] = text
        flat = re.sub(r"\s+", " ", text)
        compact = re.sub(r"\s+", "", text)
        found: dict[str, str] = {}
        for rid in set(RESOURCE_RE.findall(flat)) | set(RESOURCE_RE.findall(compact)):
            found[rid] = rid
        for alias, rid in self.aliases.items():
            if alias in flat or alias.replace(" ", "") in compact:
                found.setdefault(rid, alias)
        page_ids = identifier_tokens(flat) | identifier_tokens(compact)
        has_contact = any(t.startswith(("email:", "phone:")) for t in page_ids)
        lower = flat.lower()
        for rid, alias in found.items():
            lines = [l for l in text.splitlines() if alias in l or alias in l.replace(" ", "")]
            line_ids = set()
            for l in lines:
                line_ids |= identifier_tokens(l) | identifier_tokens(l.replace(" ", ""))
            # A document (PDF) concerns one subject, so every identifier on it links its
            # resources. A console screenshot lists many rows: a row links only through its own
            # identifiers, except the record the data catalog names for this page.
            if kind == "pdf" or alias != rid:
                ids = line_ids | page_ids
            else:
                ids = line_ids
            state, pii = "PRESENT", has_contact
            if rid.startswith("data_flow_"):
                row = " ".join(lines).lower()
                state = "INACTIVE" if any(w in row for w in ("disabled", "paused", "stopped")) else "ACTIVE"
            elif rid.startswith("backup_"):
                row = " ".join(lines).lower()
                blocked = any(w in row for w in ("blocked", "restore disabled", "legal-only"))
                state = "INACTIVE" if blocked else "ACTIVE"
            elif rid.startswith("export_"):
                row = " ".join(lines).lower()
                state = "ABSENT" if any(w in row for w in ("expired", "deleted", "purged")) else "PRESENT"
            elif rid.startswith("audit_record_"):
                pii = None
            elif rid.startswith("tombstone_"):
                pii = has_contact
            if kind == "pdf" and USER_RE.search(alias or ""):
                pass
            self._add(fid, at, at, rid, state, pii, None, ids, store="media", source=kind)
        if "retain until" in lower:
            for rid in found:
                if rid.startswith("audit_record_"):
                    m = re.search(r"retain until (\d{1,2} \w{3} \d{4})", flat, re.I)
                    if m:
                        from datetime import datetime, timezone

                        try:
                            ts = datetime.strptime(m.group(1), "%d %b %Y").replace(tzinfo=timezone.utc)
                            self.retention.setdefault(rid, ts)
                        except ValueError:
                            pass
