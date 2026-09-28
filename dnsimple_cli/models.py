"""Data models for DNSimple CLI."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DNSRecord(BaseModel):
    """Raw DNS record from the DNSimple API."""

    id: int
    zone_id: str
    name: str  # "" for root, "www", "mail", etc.
    type: str  # TXT, CNAME, MX, A, AAAA, NS, SOA, etc.
    content: str
    ttl: int
    priority: int | None = None
    regions: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "DNSRecord":
        """Create from API response data."""
        return cls(
            id=data["id"],
            zone_id=data["zone_id"],
            name=data.get("name", ""),
            type=data["type"],
            content=data["content"],
            ttl=data["ttl"],
            priority=data.get("priority"),
            regions=data.get("regions"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class Service(BaseModel):
    """Domain ownership verification service."""

    provider: str  # google, microsoft, apple, twilio, mailjet, amazon_ses, deno
    service_type: str  # site_verification, domain_verification, acme_challenge
    record_name: str  # The DNS record name that provides verification
    record_type: str  # TXT or CNAME
    content_preview: str  # First 50 chars of content for display


class App(BaseModel):
    """Traffic routing to a hosting provider."""

    subdomain: str  # www, app, staging, etc. ("@" for root)
    provider: str  # deno, fly, wpengine, github, wix, google_hosted
    target: str  # The CNAME/ALIAS target
    record_type: str  # CNAME or ALIAS


class EmailMX(BaseModel):
    """Inbound email provider (MX records)."""

    provider: str  # google, improvmx, sendgrid, amazon_ses, 123reg
    mx_records: list[str]  # List of MX targets


class EmailSMTP(BaseModel):
    """Outbound email provider (identified via SPF/DKIM)."""

    provider: str  # google, mailjet, improvmx, amazon_ses, sendgrid, mailgun
    has_spf: bool  # Found in SPF record
    has_dkim: bool  # Has DKIM record
    dkim_record: str | None = None  # DKIM record name if found


class Domain(BaseModel):
    """Domain with its services, apps, and email configuration."""

    name: str  # e.g., "example.org" or "send.example.org"
    parent_domain: str | None = None  # None for primary domains, parent name for subdomains
    state: str = "unknown"  # from DNSimple API (registered, etc.)

    # Raw records from API
    records: list[DNSRecord] = Field(default_factory=list)

    # Detected configurations
    services: list[Service] = Field(default_factory=list)
    apps: list[App] = Field(default_factory=list)
    email_mx: EmailMX | None = None
    email_smtp: list[EmailSMTP] = Field(default_factory=list)

    # Timestamps from DNSimple API (for change detection)
    domain_created_at: datetime | None = None  # Domain registration created
    domain_updated_at: datetime | None = None  # Domain settings updated
    zone_updated_at: datetime | None = None  # Zone updated (may track record changes?)

    # Metadata
    synced_at: datetime | None = None

    @property
    def is_subdomain(self) -> bool:
        """Check if this is a subdomain."""
        return self.parent_domain is not None

    @property
    def fqdn(self) -> str:
        """Get the fully qualified domain name."""
        return self.name


class SyncMetadata(BaseModel):
    """Metadata about the last sync."""

    synced_at: datetime
    account_id: str
    domain_count: int
    subdomain_count: int


class DomainTransfer(BaseModel):
    """Domain transfer tracking record."""

    id: int
    domain_id: int
    domain_name: str  # Added locally (not in API response)
    registrant_id: int
    state: str  # transferring, transferred, cancelled
    status_description: str = ""
    auto_renew: bool = False
    whois_privacy: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Local tracking fields
    initiated_at: datetime | None = None
    last_checked_at: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any], domain_name: str) -> "DomainTransfer":
        """Create from API response data."""
        return cls(
            id=data["id"],
            domain_id=data["domain_id"],
            domain_name=domain_name,
            registrant_id=data["registrant_id"],
            state=data["state"],
            status_description=data.get("status_description", ""),
            auto_renew=data.get("auto_renew", False),
            whois_privacy=data.get("whois_privacy", False),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


# Helper functions for subdomain detection

# These prefixes are NOT real subdomains - they're special DNS record prefixes
SPECIAL_PREFIXES = [
    "_domainkey",
    "_dmarc",
    "_acme-challenge",
    "_twilio",
    "_amazonses",
    "mailjet.",  # mailjet._domainkey, mailjet._xyz
    "google.",  # google._domainkey
    "dkimprovmx",  # dkimprovmx1._domainkey
    "resend.",  # resend._domainkey
    "fnt.",  # fnt._domainkey (Front)
    "selector",  # DKIM selectors
]


def is_special_prefix(record_name: str) -> bool:
    """Check if a record name is a special prefix (not a real subdomain)."""
    if not record_name:
        return False

    name_lower = record_name.lower()

    # Check for special prefixes
    for prefix in SPECIAL_PREFIXES:
        if prefix in name_lower:
            return True

    # Records starting with _ are almost always special
    if name_lower.startswith("_"):
        return True

    return False


def get_subdomain_from_record(record_name: str, zone: str) -> str | None:
    """
    Extract the true subdomain from a record name, if any.

    Returns None if the record affects the primary domain (root or special prefix).
    Returns the subdomain name if it's a true subdomain.

    Examples:
        "" or "@" → None (root domain)
        "_dmarc" → None (special prefix, affects root)
        "mailjet._domainkey" → None (DKIM for root)
        "_acme-challenge.www" → "www" (ACME for www subdomain)
        "send" → "send" (true subdomain)
        "front-mail" → "front-mail" (true subdomain)
        "www" → "www" (true subdomain)
    """
    if not record_name or record_name == "@":
        return None

    # Handle _acme-challenge.subdomain pattern
    if record_name.startswith("_acme-challenge."):
        subdomain = record_name[len("_acme-challenge.") :]
        if subdomain and not is_special_prefix(subdomain):
            return subdomain
        return None

    # If it's a special prefix, it affects the parent domain
    if is_special_prefix(record_name):
        return None

    # Otherwise it's a true subdomain
    return record_name


def extract_subdomains_with_email(records: list[DNSRecord]) -> set[str]:
    """
    Find subdomains that have their own email configuration (MX or SPF records).

    These are true subdomains that should be modeled as separate Domain entries.
    """
    subdomains = set()

    for record in records:
        # Skip root records
        if not record.name or record.name == "@":
            continue

        # Skip special prefixes
        if is_special_prefix(record.name):
            continue

        # Check if this subdomain has MX records
        if record.type == "MX":
            subdomains.add(record.name)

        # Check if this subdomain has its own SPF record
        if record.type == "TXT" and record.content.startswith("v=spf1"):
            subdomains.add(record.name)

    return subdomains
