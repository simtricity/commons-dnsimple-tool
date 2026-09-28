"""Detection logic for services, apps, and email providers."""

import re

from dnsimple_cli.models import (
    App,
    DNSRecord,
    EmailMX,
    EmailSMTP,
    Service,
    get_subdomain_from_record,
    is_special_prefix,
)


# =============================================================================
# Service Detection (Domain Ownership Verification)
# =============================================================================

def detect_services(records: list[DNSRecord]) -> list[Service]:
    """Detect domain ownership verification services from DNS records."""
    services = []

    for record in records:
        # Skip subdomain records (they belong to the subdomain, not parent)
        subdomain = get_subdomain_from_record(record.name, "")
        if subdomain and not is_special_prefix(record.name):
            continue

        service = _detect_single_service(record)
        if service:
            services.append(service)

    return services


def _detect_single_service(record: DNSRecord) -> Service | None:
    """Detect a single service from a DNS record."""
    content = record.content
    # Strip quotes from TXT records (DNSimple returns quoted content)
    if record.type == "TXT" and content.startswith('"') and content.endswith('"'):
        content = content[1:-1]
    name = record.name.lower() if record.name else ""

    # Google Site Verification (TXT)
    if record.type == "TXT" and content.startswith("google-site-verification="):
        return Service(
            provider="google",
            service_type="site_verification",
            record_name=record.name or "@",
            record_type="TXT",
            content_preview=content[:50],
        )

    # Microsoft Verification (TXT)
    if record.type == "TXT" and content.startswith("MS="):
        return Service(
            provider="microsoft",
            service_type="domain_verification",
            record_name=record.name or "@",
            record_type="TXT",
            content_preview=content[:50],
        )

    # Apple Domain Verification (TXT)
    if record.type == "TXT" and content.startswith("apple-domain-verification="):
        return Service(
            provider="apple",
            service_type="domain_verification",
            record_name=record.name or "@",
            record_type="TXT",
            content_preview=content[:50],
        )

    # Twilio Domain Verification (TXT at _twilio)
    if record.type == "TXT" and name.startswith("_twilio"):
        return Service(
            provider="twilio",
            service_type="domain_verification",
            record_name=record.name,
            record_type="TXT",
            content_preview=content[:50],
        )

    # Mailjet Domain Verification (TXT at mailjet._hexcode)
    # Pattern: mailjet._49d7d941 with hex content
    if record.type == "TXT" and re.match(r"mailjet\._[a-f0-9]+", name):
        return Service(
            provider="mailjet",
            service_type="domain_verification",
            record_name=record.name,
            record_type="TXT",
            content_preview=content[:50],
        )

    # Amazon SES Verification (TXT with amazonses:)
    if record.type == "TXT" and content.startswith("amazonses:"):
        return Service(
            provider="amazon_ses",
            service_type="domain_verification",
            record_name=record.name or "@",
            record_type="TXT",
            content_preview=content[:50],
        )

    # Deno ACME Challenge (CNAME to *.deno.net)
    if record.type == "CNAME" and name.startswith("_acme-challenge"):
        if ".deno.net" in content.lower():
            return Service(
                provider="deno",
                service_type="acme_challenge",
                record_name=record.name,
                record_type="CNAME",
                content_preview=content[:50],
            )

    return None


# =============================================================================
# App Detection (Traffic Routing)
# =============================================================================

# App provider patterns: (target_pattern, provider_name)
APP_PATTERNS = [
    (r"alias\.deno\.net$", "deno"),
    (r"\.deno\.dev$", "deno"),
    (r"\.fly\.dev$", "fly"),
    (r"wp\.wpenginepowered\.com$", "wpengine"),
    (r"\.github\.io$", "github"),
    (r"\.wixdns\.net$", "wix"),
    (r"ghs\.googlehosted\.com$", "google_hosted"),
    (r"\.vercel-dns\.com$", "vercel"),
    (r"\.netlify\.app$", "netlify"),
    (r"\.herokudns\.com$", "heroku"),
    (r"\.clerk\.services$", "clerk"),
]


def detect_apps(records: list[DNSRecord]) -> list[App]:
    """Detect traffic routing apps from CNAME and ALIAS records."""
    apps = []

    for record in records:
        # Only CNAME and ALIAS records
        if record.type not in ("CNAME", "ALIAS"):
            continue

        # Skip ACME challenge records (those are services)
        if record.name and record.name.lower().startswith("_acme-challenge"):
            continue

        # Skip DKIM records
        if record.name and "_domainkey" in record.name.lower():
            continue

        app = _detect_single_app(record)
        if app:
            apps.append(app)

    return apps


def _detect_single_app(record: DNSRecord) -> App | None:
    """Detect a single app from a CNAME/ALIAS record."""
    target = record.content.lower()

    for pattern, provider in APP_PATTERNS:
        if re.search(pattern, target):
            return App(
                subdomain=record.name or "@",
                provider=provider,
                target=record.content,
                record_type=record.type,
            )

    return None


# =============================================================================
# Email MX Detection (Inbound Email)
# =============================================================================

# MX provider patterns: (mx_pattern, provider_name)
MX_PATTERNS = [
    (r"\.google\.com$|\.googlemail\.com$", "google"),
    (r"\.improvmx\.com$", "improvmx"),
    (r"\.sendgrid\.net$", "sendgrid"),
    (r"\.amazonses\.com$", "amazon_ses"),
    (r"\.123-reg\.co\.uk$", "123reg"),
    (r"\.outlook\.com$|\.microsoft\.com$", "microsoft"),
]


def detect_email_mx(records: list[DNSRecord], subdomain: str | None = None) -> EmailMX | None:
    """
    Detect inbound email provider from MX records.

    Args:
        records: List of DNS records
        subdomain: If specified, only look at MX records for this subdomain.
                   If None, look at root domain MX records.
    """
    mx_records = []
    target_name = subdomain if subdomain else ""

    for record in records:
        if record.type != "MX":
            continue

        # Match the subdomain (or root)
        record_name = record.name or ""
        if record_name.lower() != target_name.lower():
            continue

        mx_records.append(record.content)

    if not mx_records:
        return None

    # Determine provider from first MX record
    provider = "unknown"
    sample_mx = mx_records[0].lower()

    for pattern, prov_name in MX_PATTERNS:
        if re.search(pattern, sample_mx):
            provider = prov_name
            break

    return EmailMX(provider=provider, mx_records=mx_records)


# =============================================================================
# Email SMTP Detection (Outbound Email)
# =============================================================================

# SPF provider patterns: (include_pattern, provider_name)
SPF_PATTERNS = [
    (r"_spf\.google\.com|aspmx\.googlemail\.com", "google"),
    (r"spf\.mailjet\.com", "mailjet"),
    (r"spf\.improvmx\.com", "improvmx"),
    (r"amazonses\.com", "amazon_ses"),
    (r"sendgrid\.net", "sendgrid"),
    (r"mailgun\.org", "mailgun"),
]

# DKIM record patterns: (name_pattern, provider_name)
DKIM_PATTERNS = [
    (r"^google\._domainkey", "google"),
    (r"^mailjet\._domainkey", "mailjet"),
    (r"^dkimprovmx\d*\._domainkey", "improvmx"),
    (r"^resend\._domainkey", "amazon_ses"),  # Resend uses Amazon SES
    (r"^fnt\._domainkey", "sendgrid"),  # Front uses SendGrid
    (r"^clk\d*\._domainkey", "clerk"),
]


def detect_email_smtp(records: list[DNSRecord], subdomain: str | None = None) -> list[EmailSMTP]:
    """
    Detect outbound email providers from SPF and DKIM records.

    Args:
        records: List of DNS records
        subdomain: If specified, only look at records for this subdomain.
                   If None, look at root domain records.
    """
    providers: dict[str, EmailSMTP] = {}
    target_name = subdomain if subdomain else ""

    # Find SPF records
    for record in records:
        if record.type != "TXT":
            continue

        record_name = record.name or ""

        # For root domain, SPF is at @ or ""
        # For subdomain, SPF is at the subdomain name
        if record_name.lower() != target_name.lower():
            continue

        # Strip quotes from TXT record content
        content = record.content
        if content.startswith('"') and content.endswith('"'):
            content = content[1:-1]

        if not content.startswith("v=spf1"):
            continue

        # Extract providers from SPF includes
        spf_content = content.lower()
        for pattern, provider in SPF_PATTERNS:
            if re.search(pattern, spf_content):
                if provider not in providers:
                    providers[provider] = EmailSMTP(
                        provider=provider,
                        has_spf=True,
                        has_dkim=False,
                    )
                else:
                    providers[provider].has_spf = True

    # Find DKIM records (these are at special prefixes, not subdomain names)
    for record in records:
        record_name = record.name or ""
        name_lower = record_name.lower()

        # DKIM records are either TXT or CNAME
        if record.type not in ("TXT", "CNAME"):
            continue

        # Must contain _domainkey
        if "_domainkey" not in name_lower:
            continue

        # For subdomain DKIM, the pattern is selector._domainkey.subdomain
        # For root domain DKIM, the pattern is selector._domainkey
        if subdomain:
            # For subdomain, DKIM should end with ._domainkey.subdomain or similar
            # This is tricky - most DKIM is for the parent domain
            # Skip for now - subdomains inherit parent DKIM usually
            continue

        # Check for known DKIM patterns
        for pattern, provider in DKIM_PATTERNS:
            if re.match(pattern, name_lower):
                if provider not in providers:
                    providers[provider] = EmailSMTP(
                        provider=provider,
                        has_spf=False,
                        has_dkim=True,
                        dkim_record=record_name,
                    )
                else:
                    providers[provider].has_dkim = True
                    if not providers[provider].dkim_record:
                        providers[provider].dkim_record = record_name
                break

    return list(providers.values())
