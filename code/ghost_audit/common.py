"""Shared helpers: paths, identifier normalisation, resource/system mapping."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATASET_DIR = os.path.join(REPO_ROOT, "dataset")
CACHE_DIR = os.path.join(REPO_ROOT, "code", ".cache")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"legal_hold|marketing|object|cache|email_delivery|telemetry)_\d+\b"
)
PHONE_RE = re.compile(r"\+\d[\d\s().\-]{6,}\d")

# Resource prefix -> owning system. Longest prefixes first.
PREFIX_SYSTEM = [
    ("audit_record_", "security_service"),
    ("data_flow_", "integration_service"),
    ("legal_hold_", "legal_service"),
    ("email_delivery_", "email_delivery_service"),
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("analytics_", "analytics_platform"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("backup_", "backup_service"),
    ("tombstone_", "identity_service"),
    ("marketing_", "marketing_platform"),
    ("object_", "object_storage"),
    ("cache_", "cache_service"),
    ("telemetry_", "telemetry_service"),
]

# Default data category of a resource, by prefix (refined by evidence when available).
PREFIX_CATEGORY = {
    "account_": "USER_PROFILE",
    "search_": "SEARCH_INDEX_ENTRY",
    "analytics_": "ANALYTICS_PROFILE",
    "support_": "SUPPORT_MESSAGE",
    "export_": "USER_EXPORT",
    "data_flow_": "DATA_FLOW",
    "backup_": "BACKUP_ARCHIVE",
    "audit_record_": "SECURITY_AUDIT_LOG",
    "tombstone_": "ACCOUNT_TOMBSTONE",
    "legal_hold_": "LEGAL_HOLD_RECORD",
    "marketing_": "MARKETING_PROFILE",
    "object_": "SUPPORT_ATTACHMENT",
    "cache_": "CACHE_ENTRY",
    "email_delivery_": "EMAIL_DELIVERY_LOG",
    "telemetry_": "PRODUCT_TELEMETRY",
}


def system_of(resource_id: str) -> str:
    for prefix, system in PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return "unknown_service"


def default_category(resource_id: str) -> str:
    for prefix, _ in PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return PREFIX_CATEGORY[prefix]
    return "UNKNOWN"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def norm_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


NULLS = {None, "", "null", "none", "[redacted]", "redacted"}


def is_null(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in NULLS:
        return True
    return False


def identifier_tokens(value) -> set[str]:
    """Turn a raw field value into normalised identifier tokens used for linking."""
    out: set[str] = set()
    if is_null(value):
        return out
    text = str(value)
    for email in EMAIL_RE.findall(text):
        out.add("email:" + email.lower())
    for user in USER_RE.findall(text):
        out.add("ref:" + user)
    for digest in HASH_RE.findall(text.lower()):
        out.add("hash:" + digest)
    for phone in PHONE_RE.findall(text):
        digits = norm_phone(phone)
        if len(digits) >= 8:
            out.add("phone:" + digits)
    return out


@dataclass
class Observation:
    """One statement, from one evidence item, about one resource at one moment."""

    evidence_id: str
    available_at: datetime  # when the evidence item became available (manifest / activity time)
    observed_at: datetime  # when the described state held
    system: str
    resource_id: str
    state: str  # PRESENT | ABSENT | ACTIVE | INACTIVE
    pii: bool | None = None  # direct identifiers still exposed?
    category: str | None = None
    ids: set[str] = field(default_factory=set)  # identifier tokens linking to a person
    store: str = "primary"  # primary | replica name | log
    source: str = ""  # short description of the evidence shape
    name_tokens: set[str] = field(default_factory=set)
