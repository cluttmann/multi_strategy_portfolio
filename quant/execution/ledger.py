"""Firestore state for the quant system — all collections `qnt-` prefixed.

Coexists with the ETF bot's Firestore in the same project; nothing here
touches the bot's collections.
"""

import datetime as dt

from google.cloud import firestore

from quant.config import GCP_PROJECT

_db = None


def db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=GCP_PROJECT)
    return _db


def get_sleeve(sleeve: str) -> dict:
    doc = db().collection("qnt-ledger").document(sleeve).get()
    return doc.to_dict() or {}


def set_sleeve(sleeve: str, data: dict):
    data["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    db().collection("qnt-ledger").document(sleeve).set(data)


def risk_state() -> dict:
    doc = db().collection("qnt-risk").document("state").get()
    return doc.to_dict() or {}


def update_risk_state(**kwargs):
    kwargs["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    db().collection("qnt-risk").document("state").set(kwargs, merge=True)
