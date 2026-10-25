"""CSV loading for the participant-facing dataset."""
from __future__ import annotations

import csv
import os

from .common import DATASET_DIR


def read_csv(name: str, dataset_dir: str = DATASET_DIR) -> list[dict]:
    with open(os.path.join(dataset_dir, name), encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class Dataset:
    def __init__(self, dataset_dir: str = DATASET_DIR):
        self.dataset_dir = dataset_dir
        self.identities = read_csv("user_identities.csv", dataset_dir)
        self.audit_log = read_csv("user_audit_log.csv", dataset_dir)
        self.activities = read_csv("system_activity.csv", dataset_dir)
        self.rules = read_csv("deletion_rules.csv", dataset_dir)
        self.manifest = read_csv("evidence_files.csv", dataset_dir)

    def requests(self, name: str = "deletion_requests.csv") -> list[dict]:
        return read_csv(name, self.dataset_dir)
