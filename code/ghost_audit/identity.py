"""Identity resolution: current snapshot + audit history -> identifier token sets."""
from __future__ import annotations

from collections import defaultdict

from .common import norm_name, norm_phone, parse_ts


class IdentityIndex:
    def __init__(self, identities: list[dict], audit_log: list[dict]):
        self.users: dict[str, dict] = {r["user_id"]: r for r in identities}
        self.tokens: dict[str, set[str]] = defaultdict(set)
        self.names: dict[str, set[str]] = defaultdict(set)
        self.history: dict[str, list[dict]] = defaultdict(list)
        for uid, row in self.users.items():
            self.tokens[uid] |= {
                "ref:" + uid,
                "email:" + row["email"].lower(),
                "phone:" + norm_phone(row["phone"]),
                "hash:" + row["email_hash"].lower(),
            }
            self.names[uid].add(norm_name(row["name"]))
        for row in audit_log:
            uid = row["user_id"]
            self.history[uid].append(row)
            for value in (row["old_value"], row["new_value"]):
                if not value:
                    continue
                field = row["field_name"]
                if field == "email":
                    self.tokens[uid].add("email:" + value.lower())
                elif field == "phone":
                    self.tokens[uid].add("phone:" + norm_phone(value))
                elif field == "email_hash":
                    self.tokens[uid].add("hash:" + value.lower())
                elif field in ("name", "full_name"):
                    self.names[uid].add(norm_name(value))
        self.by_token: dict[str, set[str]] = defaultdict(set)
        for uid, toks in self.tokens.items():
            for t in toks:
                self.by_token[t].add(uid)

    def resolve_email(self, email: str) -> str | None:
        owners = self.by_token.get("email:" + (email or "").strip().lower(), set())
        if len(owners) == 1:
            return next(iter(owners))
        if owners:
            # Shared historical address: prefer the user whose *current* email matches.
            for uid in sorted(owners):
                if self.users[uid]["email"].lower() == email.strip().lower():
                    return uid
            return sorted(owners)[0]
        return None

    def owners_of(self, tokens: set[str]) -> set[str]:
        out: set[str] = set()
        for t in tokens:
            out |= self.by_token.get(t, set())
        return out

    def describe(self, uid: str) -> dict:
        row = self.users.get(uid, {})
        return {
            "user_id": uid,
            "current_email": row.get("email"),
            "identifiers": sorted(self.tokens.get(uid, ())),
            "history_events": len(self.history.get(uid, ())),
            "updated_at": parse_ts(row.get("updated_at")),
        }
