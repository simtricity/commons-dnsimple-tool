"""Email provider setup templates and pre-flight checks.

Defines DNS record templates for email providers (Google Workspace, ImprovMX)
and provides pre-flight checks for conflicts with existing records.
"""

from __future__ import annotations

import re
from typing import Any


# =============================================================================
# Google Workspace
# =============================================================================

GOOGLE_MX_RECORDS = [
    {"content": "aspmx.l.google.com", "priority": 10},
    {"content": "alt1.aspmx.l.google.com", "priority": 20},
]

GOOGLE_SPF = "v=spf1 include:_spf.google.com ~all"

# =============================================================================
# ImprovMX (email forwarding)
# =============================================================================

IMPROVMX_MX_RECORDS = [
    {"content": "mx1.improvmx.com", "priority": 10},
    {"content": "mx2.improvmx.com", "priority": 20},
]

IMPROVMX_SPF = "v=spf1 include:spf.improvmx.com ~all"

# =============================================================================
# Provider registry
# =============================================================================

PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "label": "Google Workspace",
        "mx_records": GOOGLE_MX_RECORDS,
        "spf": GOOGLE_SPF,
        "spf_include": "_spf.google.com",
        "next_steps": [
            "Add this domain as a user alias domain in Google Admin → Domains",
            "Add Google site verification TXT record (from Google Admin)",
        ],
    },
    "improvmx": {
        "label": "ImprovMX",
        "mx_records": IMPROVMX_MX_RECORDS,
        "spf": IMPROVMX_SPF,
        "spf_include": "spf.improvmx.com",
        "next_steps": [
            "Configure email forwarding rules at https://app.improvmx.com",
        ],
    },
}


def get_provider(name: str) -> dict[str, Any]:
    """Get provider config by name.

    Args:
        name: Provider name (google, improvmx)

    Returns:
        Provider config dict

    Raises:
        ValueError: If provider name is unknown
    """
    if name not in PROVIDERS:
        valid = ", ".join(PROVIDERS.keys())
        raise ValueError(f"Unknown provider '{name}'. Valid providers: {valid}")
    return PROVIDERS[name]


def get_records_to_create(provider_name: str) -> list[dict[str, Any]]:
    """Get the list of DNS records to create for a provider.

    Args:
        provider_name: Provider name (google, improvmx)

    Returns:
        List of record dicts with type, name, content, priority, ttl
    """
    provider = get_provider(provider_name)
    records = []

    for mx in provider["mx_records"]:
        records.append({
            "type": "MX",
            "name": "",
            "content": mx["content"],
            "priority": mx["priority"],
            "ttl": 3600,
        })

    records.append({
        "type": "TXT",
        "name": "",
        "content": provider["spf"],
        "priority": None,
        "ttl": 3600,
    })

    return records


def check_existing_email(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check for existing email-related DNS records that might conflict.

    Args:
        records: List of DNS record dicts (from API or storage)

    Returns:
        Dict with:
            has_mx: bool - whether MX records exist at root
            mx_provider: str | None - detected MX provider name
            mx_records: list - existing MX record dicts
            has_spf: bool - whether SPF record exists at root
            spf_record: dict | None - existing SPF record dict
            spf_content: str | None - SPF record content (unquoted)
    """
    result: dict[str, Any] = {
        "has_mx": False,
        "mx_provider": None,
        "mx_records": [],
        "has_spf": False,
        "spf_record": None,
        "spf_content": None,
    }

    for rec in records:
        rec_name = rec.get("name", "")
        rec_type = rec.get("type", "")

        # Root MX records
        if rec_type == "MX" and rec_name == "":
            result["has_mx"] = True
            result["mx_records"].append(rec)
            content = rec.get("content", "").lower()
            if "google.com" in content:
                result["mx_provider"] = "google"
            elif "improvmx.com" in content:
                result["mx_provider"] = "improvmx"

        # Root SPF record
        if rec_type == "TXT" and rec_name == "":
            content = rec.get("content", "")
            if content.startswith('"') and content.endswith('"'):
                content = content[1:-1]
            if content.lower().startswith("v=spf1"):
                result["has_spf"] = True
                result["spf_record"] = rec
                result["spf_content"] = content

    return result


def merge_spf(existing_spf: str, new_include: str) -> str:
    """Merge a new SPF include into an existing SPF record.

    Adds the new include if not already present. Preserves existing includes
    and the policy mechanism (~all, -all, ?all).

    Args:
        existing_spf: Current SPF record content (e.g. "v=spf1 include:spf.mailjet.com ~all")
        new_include: Include domain to add (e.g. "_spf.google.com")

    Returns:
        Merged SPF string
    """
    # Check if already included
    if new_include.lower() in existing_spf.lower():
        return existing_spf

    # Find the policy at the end (~all, -all, ?all, +all)
    policy_match = re.search(r"\s+([~\-?+]all)\s*$", existing_spf)
    if policy_match:
        policy = policy_match.group(1)
        base = existing_spf[: policy_match.start()]
        return f"{base} include:{new_include} {policy}"

    # No policy found, just append
    return f"{existing_spf} include:{new_include} ~all"
