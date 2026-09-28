"""Clerk authentication DNS record templates and pre-flight checks.

Clerk requires 5 CNAME records for custom domain setup:
- Frontend API: clerk → frontend-api.clerk.services
- Account Portal: accounts → accounts.clerk.services
- Email: clkmail → mail.{instance_id}.clerk.services
- DKIM 1: clk._domainkey → dkim1.{instance_id}.clerk.services
- DKIM 2: clk2._domainkey → dkim2.{instance_id}.clerk.services
"""

from typing import Any


# Records 1-2 are static (same for all Clerk instances)
# Records 3-5 require the instance-specific identifier
CLERK_RECORDS = [
    {"name": "clerk", "label": "Frontend API", "content": "frontend-api.clerk.services"},
    {"name": "accounts", "label": "Account Portal", "content": "accounts.clerk.services"},
    {"name": "clkmail", "label": "Email", "content_template": "mail.{instance_id}.clerk.services"},
    {"name": "clk._domainkey", "label": "DKIM 1", "content_template": "dkim1.{instance_id}.clerk.services"},
    {"name": "clk2._domainkey", "label": "DKIM 2", "content_template": "dkim2.{instance_id}.clerk.services"},
]


def get_clerk_records(domain: str, instance_id: str) -> list[dict[str, Any]]:
    """Generate the 5 CNAME records needed for Clerk custom domain setup.

    Args:
        domain: The domain being configured (for reference only)
        instance_id: Clerk instance identifier (e.g. ceta5hgdrzyn)

    Returns:
        List of record dicts ready for API creation
    """
    records = []
    for rec in CLERK_RECORDS:
        content = rec.get("content") or rec["content_template"].format(instance_id=instance_id)
        records.append({
            "type": "CNAME",
            "name": rec["name"],
            "content": content,
            "ttl": 3600,
            "priority": None,
            "label": rec["label"],
        })
    return records


def check_existing_clerk(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Check for existing Clerk-related DNS records.

    Args:
        records: Current DNS records from API (raw dicts)

    Returns:
        Dict with per-record existence status:
        - existing: list of dicts with name, record, is_clerk (points to *.clerk.services)
        - conflicts: list of dicts with name, record (exists but NOT pointing to clerk)
    """
    clerk_names = {rec["name"].lower() for rec in CLERK_RECORDS}

    existing = []
    conflicts = []

    for rec in records:
        rec_name = (rec.get("name") or "").lower()
        if rec_name not in clerk_names:
            continue

        if rec.get("type") != "CNAME":
            # Non-CNAME record at a Clerk name — conflict
            conflicts.append({"name": rec_name, "record": rec})
            continue

        content = (rec.get("content") or "").lower()
        if content.endswith(".clerk.services") or content.endswith(".clerk.services."):
            existing.append({"name": rec_name, "record": rec, "is_clerk": True})
        else:
            conflicts.append({"name": rec_name, "record": rec})

    return {
        "existing": existing,
        "conflicts": conflicts,
        "existing_names": {e["name"] for e in existing},
    }
