"""Redirect setup templates and pre-flight checks.

Defines DNS record templates for domain redirects — used for parked/alias
domains that should redirect all traffic to a primary domain.
"""

from __future__ import annotations

from typing import Any


def get_redirect_records(target_url: str) -> list[dict[str, Any]]:
    """Get the DNS records needed for a full domain redirect.

    Creates:
    - URL @ → target (naked domain redirect)
    - URL * → target (wildcard catches www and all subdomains)

    Args:
        target_url: Full URL to redirect to (e.g. https://www.example.com)

    Returns:
        List of record dicts ready for creation
    """
    return [
        {
            "type": "URL",
            "name": "",
            "content": target_url,
            "priority": None,
            "ttl": 3600,
        },
        {
            "type": "URL",
            "name": "*",
            "content": target_url,
            "priority": None,
            "ttl": 3600,
        },
    ]


def check_existing_redirects(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Check for existing records that might conflict with redirect setup.

    Args:
        records: List of DNS record dicts (from API)

    Returns:
        Dict with:
            has_naked: bool - whether @ has URL/ALIAS/A record
            naked_record: dict | None
            has_wildcard: bool - whether * record exists
            wildcard_record: dict | None
            has_www: bool - whether explicit www record exists
            www_record: dict | None
    """
    result: dict[str, Any] = {
        "has_naked": False,
        "naked_record": None,
        "has_wildcard": False,
        "wildcard_record": None,
        "has_www": False,
        "www_record": None,
    }

    for rec in records:
        rec_name = rec.get("name", "")
        rec_type = rec.get("type", "")

        if rec_name == "" and rec_type in ("URL", "ALIAS", "A", "AAAA", "CNAME"):
            result["has_naked"] = True
            result["naked_record"] = rec

        if rec_name == "*":
            result["has_wildcard"] = True
            result["wildcard_record"] = rec

        if rec_name == "www" and rec_type in ("URL", "CNAME", "ALIAS", "A", "AAAA"):
            result["has_www"] = True
            result["www_record"] = rec

    return result
