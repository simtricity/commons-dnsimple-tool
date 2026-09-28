"""App hosting setup templates and pre-flight checks.

Defines DNS record templates for app hosting providers (Deno Deploy)
and provides pre-flight checks for conflicts with existing records.
"""

from __future__ import annotations

from typing import Any


def get_deno_records(domain: str, acme_token: str) -> list[dict[str, Any]]:
    """Get the DNS records needed for Deno Deploy www hosting.

    Standard pattern:
    - CNAME www → alias.deno.net (app traffic)
    - CNAME _acme-challenge.www → {token}._acme.deno.net. (TLS cert)
    - URL @ → https://www.{domain} (naked domain redirect)

    Args:
        domain: Domain name (e.g. example.com)
        acme_token: ACME challenge token from Deno Deploy config page

    Returns:
        List of record dicts ready for creation
    """
    return [
        {
            "type": "CNAME",
            "name": "www",
            "content": "alias.deno.net",
            "priority": None,
            "ttl": 3600,
        },
        {
            "type": "CNAME",
            "name": "_acme-challenge.www",
            "content": f"{acme_token}._acme.deno.net.",
            "priority": None,
            "ttl": 3600,
        },
        {
            "type": "URL",
            "name": "",
            "content": f"https://www.{domain}",
            "priority": None,
            "ttl": 3600,
        },
    ]


def get_deno_apex_records(acme_token: str) -> list[dict[str, Any]]:
    """Get the DNS records needed for Deno Deploy on the apex domain (ALIAS pattern).

    Apex pattern (no www):
    - ALIAS @ → alias.deno.net (app traffic, resolves at request time)
    - CNAME _acme-challenge → {token}._acme.deno.net. (TLS cert)

    Args:
        acme_token: ACME challenge token from Deno Deploy config page

    Returns:
        List of record dicts ready for creation
    """
    return [
        {
            "type": "ALIAS",
            "name": "",
            "content": "alias.deno.net",
            "priority": None,
            "ttl": 3600,
        },
        {
            "type": "CNAME",
            "name": "_acme-challenge",
            "content": f"{acme_token}._acme.deno.net.",
            "priority": None,
            "ttl": 3600,
        },
    ]


def get_deno_www_only_records(acme_token: str) -> list[dict[str, Any]]:
    """Get the DNS records for adding www to Deno Deploy WITHOUT a naked URL redirect.

    Used alongside the apex setup when the bare apex itself is on Deno Deploy
    and www should also point at Deno (with the redirect handled in app code).

    - CNAME www → alias.deno.net
    - CNAME _acme-challenge.www → {token}._acme.deno.net.

    Args:
        acme_token: ACME challenge token from Deno Deploy for the www host
            (different from the apex token — Deno issues one per domain).

    Returns:
        List of record dicts ready for creation.
    """
    return [
        {
            "type": "CNAME",
            "name": "www",
            "content": "alias.deno.net",
            "priority": None,
            "ttl": 3600,
        },
        {
            "type": "CNAME",
            "name": "_acme-challenge.www",
            "content": f"{acme_token}._acme.deno.net.",
            "priority": None,
            "ttl": 3600,
        },
    ]


def check_existing_apex(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Check for existing apex-related DNS records that might conflict.

    Returns:
        Dict with:
            has_apex: bool - whether @ has ALIAS/A/AAAA/URL/CNAME
            apex_record: dict | None
            apex_type: str | None
            has_acme: bool - whether _acme-challenge exists
            acme_record: dict | None
    """
    result: dict[str, Any] = {
        "has_apex": False,
        "apex_record": None,
        "apex_type": None,
        "has_acme": False,
        "acme_record": None,
    }

    for rec in records:
        rec_name = rec.get("name", "")
        rec_type = rec.get("type", "")

        if rec_name == "" and rec_type in ("ALIAS", "A", "AAAA", "URL", "CNAME"):
            result["has_apex"] = True
            result["apex_record"] = rec
            result["apex_type"] = rec_type

        if rec_name == "_acme-challenge" and rec_type == "CNAME":
            result["has_acme"] = True
            result["acme_record"] = rec

    return result


def check_existing_web(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Check for existing web-related DNS records that might conflict.

    Args:
        records: List of DNS record dicts (from API)

    Returns:
        Dict with:
            has_www: bool - whether www record exists
            www_record: dict | None - existing www record
            www_type: str | None - type of existing www record (CNAME, ALIAS, A)
            has_naked_redirect: bool - whether @ has URL/ALIAS/A record
            naked_record: dict | None - existing naked domain record
            has_acme: bool - whether _acme-challenge.www exists
            acme_record: dict | None - existing ACME record
    """
    result: dict[str, Any] = {
        "has_www": False,
        "www_record": None,
        "www_type": None,
        "has_naked_redirect": False,
        "naked_record": None,
        "has_acme": False,
        "acme_record": None,
    }

    for rec in records:
        rec_name = rec.get("name", "")
        rec_type = rec.get("type", "")

        if rec_name == "www" and rec_type in ("CNAME", "ALIAS", "A", "AAAA"):
            result["has_www"] = True
            result["www_record"] = rec
            result["www_type"] = rec_type

        if rec_name == "" and rec_type in ("URL", "ALIAS", "A"):
            result["has_naked_redirect"] = True
            result["naked_record"] = rec

        if rec_name == "_acme-challenge.www" and rec_type == "CNAME":
            result["has_acme"] = True
            result["acme_record"] = rec

    return result
