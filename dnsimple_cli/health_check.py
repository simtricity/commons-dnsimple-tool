"""DNS health checks using synced local data.

Validates key email and web configuration for a domain, modeled on
MxToolbox emailhealth checks but using our local DNS records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from dnsimple_cli.models import DNSRecord


@dataclass
class HealthResult:
    """Single health check result."""

    check: str
    status: str  # "pass", "warn", "fail"
    message: str
    recommendation: str | None = None


@dataclass
class HealthReport:
    """Complete health report for a domain."""

    domain: str
    results: list[HealthResult] = field(default_factory=list)
    #: "receive" (root MX), "send-only" (no root MX but sends: DKIM, or a sender
    #: subdomain with its own SPF or MX), or "none".
    mail_mode: str = "receive"

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.status == "pass")

    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.results if r.status == "warn")

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")


def _get_txt_content(record: DNSRecord) -> str:
    """Get TXT record content with quotes stripped."""
    content = record.content
    if content.startswith('"') and content.endswith('"'):
        content = content[1:-1]
    return content


def sender_subdomains(records: list[DNSRecord]) -> list[str]:
    """Subdomains that carry their own SPF or MX, e.g. `send` for Resend's envelope sender."""
    names = set()
    for r in records:
        if not r.name or r.name.startswith("_"):
            continue
        if r.type == "MX" or (r.type == "TXT" and _get_txt_content(r).startswith("v=spf1")):
            names.add(r.name)
    return sorted(names)


def mail_mode(records: list[DNSRecord]) -> str:
    """How the domain uses mail: "receive", "send-only" or "none"."""
    if any(r.type == "MX" and r.name == "" for r in records):
        return "receive"
    has_dkim = any("_domainkey" in r.name.lower() for r in records)
    if has_dkim or sender_subdomains(records):
        return "send-only"
    return "none"


def check_mx(records: list[DNSRecord], mode: str = "receive") -> HealthResult:
    """Check that MX records exist at the root. A send-only domain needs none."""
    mx = [r for r in records if r.type == "MX" and r.name == ""]
    if not mx and mode == "send-only":
        return HealthResult(
            "MX Records", "pass",
            "No root MX: send-only domain, not expected to receive mail",
        )
    if not mx:
        return HealthResult("MX Records", "fail", "No MX records found — domain cannot receive email")
    providers = ", ".join(r.content for r in mx)
    return HealthResult("MX Records", "pass", f"{len(mx)} MX record(s): {providers}")


def check_spf(records: list[DNSRecord], mode: str = "receive") -> HealthResult:
    """Check SPF record exists and has a reasonable policy.

    A domain that receives no mail at its root needs no sending SPF there: senders such as
    Resend use `send.<domain>` as the envelope sender, which has its own SPF. The root then
    only needs `v=spf1 -all` so nothing can send as it.
    """
    spf_records = []
    for r in records:
        if r.type == "TXT" and r.name == "":
            content = _get_txt_content(r)
            if content.startswith("v=spf1"):
                spf_records.append(content)

    if not spf_records and mode in ("send-only", "none"):
        subs = sender_subdomains(records)
        if len(subs) == 1:
            where = f"sending uses the '{subs[0]}' subdomain, which has its own SPF"
        elif subs:
            where = f"sending uses the {', '.join(repr(x) for x in subs)} subdomains, which have their own SPF"
        else:
            where = "the root sends no mail"
        return HealthResult(
            "SPF Record", "warn",
            f"No root SPF; {where}. Not a failure for a domain with no root MX.",
            recommendation='Add TXT @ "v=spf1 -all" so nothing can send as the root domain',
        )

    if not spf_records:
        return HealthResult("SPF Record", "fail", "No SPF record found — outbound email not authenticated")

    if len(spf_records) > 1:
        return HealthResult("SPF Record", "warn", f"Multiple SPF records found ({len(spf_records)}) — only one is allowed per RFC 7208")

    spf = spf_records[0]

    # Check policy qualifier
    if spf.endswith("-all"):
        policy_msg = "strict reject (-all)"
    elif spf.endswith("~all"):
        policy_msg = "soft fail (~all)"
    elif spf.endswith("?all"):
        return HealthResult("SPF Record", "warn", f"SPF policy is neutral (?all) — should be ~all or -all. Record: {spf}")
    elif spf.endswith("+all"):
        return HealthResult("SPF Record", "fail", f"SPF policy allows all senders (+all) — this is insecure. Record: {spf}")
    else:
        policy_msg = "present"

    return HealthResult("SPF Record", "pass", f"SPF {policy_msg}: {spf}")


def check_dmarc(records: list[DNSRecord]) -> HealthResult:
    """Check DMARC record exists with a policy."""
    dmarc_records = []
    for r in records:
        if r.type == "TXT" and r.name.lower() == "_dmarc":
            content = _get_txt_content(r)
            if content.startswith("v=DMARC1"):
                dmarc_records.append(content)

    if not dmarc_records:
        return HealthResult("DMARC Record", "fail", "No DMARC record found — domain vulnerable to email spoofing")

    dmarc = dmarc_records[0]

    # Extract policy
    policy_match = re.search(r"p=(none|quarantine|reject)", dmarc)
    if not policy_match:
        return HealthResult("DMARC Record", "warn", f"DMARC record found but no valid policy: {dmarc}")

    policy = policy_match.group(1)
    if policy == "none":
        return HealthResult("DMARC Record", "warn", f"DMARC policy is 'none' (monitoring only) — consider upgrading to quarantine or reject. Record: {dmarc}")

    return HealthResult("DMARC Record", "pass", f"DMARC policy={policy}: {dmarc}")


def check_dkim(records: list[DNSRecord]) -> HealthResult:
    """Check for DKIM records."""
    dkim_records = []
    for r in records:
        if r.type in ("TXT", "CNAME") and "_domainkey" in r.name.lower():
            dkim_records.append(r)

    if not dkim_records:
        return HealthResult("DKIM Records", "warn", "No DKIM records found — email authentication incomplete without DKIM")

    selectors = [r.name for r in dkim_records]
    return HealthResult("DKIM Records", "pass", f"{len(dkim_records)} DKIM record(s): {', '.join(selectors)}")


def check_www(records: list[DNSRecord]) -> HealthResult:
    """Check that www has a CNAME or ALIAS record."""
    www_records = [
        r for r in records
        if r.name.lower() == "www" and r.type in ("CNAME", "ALIAS", "A", "AAAA")
    ]

    if not www_records:
        return HealthResult("WWW Record", "warn", "No www record found — domain has no web presence at www")

    rec = www_records[0]
    return HealthResult("WWW Record", "pass", f"www {rec.type} → {rec.content}")


def check_naked_redirect(records: list[DNSRecord]) -> HealthResult:
    """Check that the naked/apex domain has a redirect or hosting record."""
    naked_records = [
        r for r in records
        if r.name == "" and r.type in ("URL", "ALIAS", "A", "AAAA")
    ]

    if not naked_records:
        return HealthResult("Naked Domain", "warn", "No apex record (URL redirect, ALIAS, or A) — naked domain won't resolve")

    rec = naked_records[0]
    if rec.type == "URL":
        return HealthResult("Naked Domain", "pass", f"Apex redirects to {rec.content}")
    return HealthResult("Naked Domain", "pass", f"Apex {rec.type} → {rec.content}")


def check_ns(records: list[DNSRecord]) -> HealthResult:
    """Check NS records point to DNSimple."""
    ns_records = [r for r in records if r.type == "NS" and r.name == ""]
    if not ns_records:
        return HealthResult("NS Records", "warn", "No NS records found")

    dnsimple_ns = [r for r in ns_records if "dnsimple" in r.content.lower()]
    if not dnsimple_ns:
        ns_list = ", ".join(r.content for r in ns_records)
        return HealthResult("NS Records", "warn", f"NS records don't point to DNSimple: {ns_list}")

    return HealthResult("NS Records", "pass", f"{len(ns_records)} NS records pointing to DNSimple")


def run_health_check(domain_name: str, records: list[DNSRecord]) -> HealthReport:
    """Run all health checks on a domain's records.

    Args:
        domain_name: The domain being checked
        records: All DNS records for this domain/zone

    Returns:
        HealthReport with all check results
    """
    mode = mail_mode(records)
    report = HealthReport(domain=domain_name, mail_mode=mode)

    report.results.append(check_ns(records))
    report.results.append(check_mx(records, mode))
    report.results.append(check_spf(records, mode))
    report.results.append(check_dkim(records))
    report.results.append(check_dmarc(records))
    report.results.append(check_www(records))
    report.results.append(check_naked_redirect(records))

    return report
