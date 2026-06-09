"""Shared helpers for persisted mapping confidence flags."""

from __future__ import annotations

import json


def load_confidence_factors(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def factors_are_preaired(factors: dict | None) -> bool:
    payload = factors or {}
    return bool(payload.get("preaired") or payload.get("preaired_placeholder"))


def factors_preaired_type(factors: dict | None) -> str:
    payload = factors or {}
    value = str(payload.get("preaired_type") or "").strip().lower()
    if value:
        return value
    if payload.get("preaired_placeholder"):
        return "placeholder"
    if payload.get("preaired_prereleased"):
        return "prereleased"
    if payload.get("preaired"):
        return "placeholder"
    return ""


def factors_are_preaired_placeholder(factors: dict | None) -> bool:
    return factors_preaired_type(factors) == "placeholder"


def mapping_is_preaired(mapping: dict | None) -> bool:
    if not mapping:
        return False
    return factors_are_preaired(load_confidence_factors(mapping.get("confidence_factors")))


def mapping_is_preaired_placeholder(mapping: dict | None) -> bool:
    if not mapping:
        return False
    return factors_are_preaired_placeholder(load_confidence_factors(mapping.get("confidence_factors")))
