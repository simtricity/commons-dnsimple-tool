"""TinyDB storage layer for DNSimple CLI."""

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from tinydb import TinyDB, Query

from dnsimple_cli.models import (
    DNSRecord,
    Domain,
    DomainTransfer,
    SyncMetadata,
    extract_subdomains_with_email,
)
from dnsimple_cli.services import (
    detect_apps,
    detect_email_mx,
    detect_email_smtp,
    detect_services,
)


def get_state_dir() -> Path:
    """Directory holding credentials, the local database and reports.

    ``$DNSIMPLE_STATE_DIR`` if set, otherwise ``~/.simt/dnsimple``. Independent of the
    current directory and of where the package is installed.
    """
    override = os.environ.get("DNSIMPLE_STATE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".simt" / "dnsimple"


def get_db_path() -> Path:
    """Get the path to the TinyDB database file (``<state>/data/db.json``)."""
    data_dir = get_state_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "db.json"


class Storage:
    """TinyDB storage for domains and metadata."""

    def __init__(self, db_path: Path | None = None):
        """Initialize storage with optional custom path."""
        self.db_path = db_path or get_db_path()
        self.db = TinyDB(self.db_path)
        self.domains_table = self.db.table("domains")
        self.metadata_table = self.db.table("metadata")
        self.transfers_table = self.db.table("transfers")

    def close(self):
        """Close the database."""
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # =========================================================================
    # Domain Operations
    # =========================================================================

    def save_domains(self, domains: list[Domain]) -> None:
        """Save domains to the database, replacing existing data."""
        # Clear existing domains
        self.domains_table.truncate()

        # Insert new domains
        for domain in domains:
            self.domains_table.insert(domain.model_dump(mode="json"))

    def load_domains(self, include_subdomains: bool = True) -> list[Domain]:
        """Load all domains from the database."""
        DomainQuery = Query()

        if include_subdomains:
            docs = self.domains_table.all()
        else:
            # Only primary domains (parent_domain is None)
            docs = self.domains_table.search(DomainQuery.parent_domain == None)  # noqa: E711

        return [Domain.model_validate(doc) for doc in docs]

    def get_domain(self, name: str) -> Domain | None:
        """Get a single domain by name."""
        DomainQuery = Query()
        doc = self.domains_table.get(DomainQuery.name == name)
        if doc:
            return Domain.model_validate(doc)
        return None

    def get_primary_domains(self) -> list[Domain]:
        """Get only primary domains (not subdomains)."""
        return self.load_domains(include_subdomains=False)

    def get_subdomains(self, parent_domain: str) -> list[Domain]:
        """Get subdomains of a specific parent domain."""
        DomainQuery = Query()
        docs = self.domains_table.search(DomainQuery.parent_domain == parent_domain)
        return [Domain.model_validate(doc) for doc in docs]

    def save_domain(self, domain: Domain, subdomains: list[Domain] | None = None) -> None:
        """Upsert a single domain and its subdomains without truncating the table.

        Replaces the domain entry if it exists, inserts if new.
        Also replaces any subdomains for this zone.
        """
        DomainQuery = Query()

        # Upsert primary domain
        existing = self.domains_table.get(DomainQuery.name == domain.name)
        data = domain.model_dump(mode="json")
        if existing:
            self.domains_table.update(data, DomainQuery.name == domain.name)
        else:
            self.domains_table.insert(data)

        # Replace subdomains for this zone
        if subdomains is not None:
            # Remove old subdomains
            self.domains_table.remove(DomainQuery.parent_domain == domain.name)
            # Insert new ones
            for sub in subdomains:
                self.domains_table.insert(sub.model_dump(mode="json"))

    # =========================================================================
    # Metadata Operations
    # =========================================================================

    def save_metadata(self, metadata: SyncMetadata) -> None:
        """Save sync metadata."""
        self.metadata_table.truncate()
        self.metadata_table.insert(metadata.model_dump(mode="json"))

    def get_metadata(self) -> SyncMetadata | None:
        """Get the latest sync metadata."""
        docs = self.metadata_table.all()
        if docs:
            return SyncMetadata.model_validate(docs[0])
        return None

    # =========================================================================
    # Transfer Operations
    # =========================================================================

    def save_transfer(self, transfer: DomainTransfer) -> None:
        """Save or update a transfer record."""
        TransferQuery = Query()
        existing = self.transfers_table.get(TransferQuery.id == transfer.id)
        if existing:
            self.transfers_table.update(
                transfer.model_dump(mode="json"),
                TransferQuery.id == transfer.id,
            )
        else:
            self.transfers_table.insert(transfer.model_dump(mode="json"))

    def get_all_transfers(self) -> list[DomainTransfer]:
        """Get all transfer records."""
        docs = self.transfers_table.all()
        return [DomainTransfer.model_validate(doc) for doc in docs]

    def get_transfer_by_domain(self, domain_name: str) -> DomainTransfer | None:
        """Get the most recent transfer for a domain."""
        TransferQuery = Query()
        docs = self.transfers_table.search(TransferQuery.domain_name == domain_name)
        if docs:
            transfers = [DomainTransfer.model_validate(d) for d in docs]
            return sorted(transfers, key=lambda t: t.created_at or datetime.min)[-1]
        return None


def process_api_data(
    api_domains: list[dict[str, Any]],
    api_zones: dict[str, dict[str, Any]],
    api_records: dict[str, list[dict[str, Any]]],
    account_id: str,
) -> tuple[list[Domain], SyncMetadata]:
    """
    Process raw API data into Domain objects with detected services/apps/email.

    Args:
        api_domains: List of domain dicts from DNSimple API
        api_zones: Dict mapping domain name to zone dict
        api_records: Dict mapping domain name to list of record dicts
        account_id: DNSimple account ID

    Returns:
        Tuple of (list of Domain objects, SyncMetadata)
    """
    now = datetime.now()
    domains: list[Domain] = []
    subdomain_count = 0

    for api_domain in api_domains:
        zone_name = api_domain["name"]
        api_zone = api_zones.get(zone_name, {})
        raw_records = api_records.get(zone_name, [])

        # Convert to DNSRecord objects
        records = [DNSRecord.from_api(r) for r in raw_records]

        # Create primary domain
        primary = _create_domain(
            name=zone_name,
            parent_domain=None,
            state=api_domain.get("state", "unknown"),
            records=records,
            domain_created_at=api_domain.get("created_at"),
            domain_updated_at=api_domain.get("updated_at"),
            zone_updated_at=api_zone.get("updated_at"),
            synced_at=now,
        )
        domains.append(primary)

        # Find and create subdomains with their own email config
        subdomain_names = extract_subdomains_with_email(records)
        for sub_name in subdomain_names:
            # Filter records for this subdomain
            sub_records = _filter_records_for_subdomain(records, sub_name)

            subdomain = _create_domain(
                name=f"{sub_name}.{zone_name}",
                parent_domain=zone_name,
                state="subdomain",
                records=sub_records,
                domain_created_at=None,
                domain_updated_at=None,
                zone_updated_at=api_zone.get("updated_at"),
                synced_at=now,
            )
            domains.append(subdomain)
            subdomain_count += 1

    metadata = SyncMetadata(
        synced_at=now,
        account_id=account_id,
        domain_count=len(api_domains),
        subdomain_count=subdomain_count,
    )

    return domains, metadata


def _create_domain(
    name: str,
    parent_domain: str | None,
    state: str,
    records: list[DNSRecord],
    domain_created_at: str | None,
    domain_updated_at: str | None,
    zone_updated_at: str | None,
    synced_at: datetime,
) -> Domain:
    """Create a Domain object with all detected configurations."""
    # For subdomains, we need to pass the subdomain name to detection functions
    subdomain_name = name.split(".")[0] if parent_domain else None

    return Domain(
        name=name,
        parent_domain=parent_domain,
        state=state,
        records=records,
        services=detect_services(records) if not parent_domain else [],
        apps=detect_apps(records),
        email_mx=detect_email_mx(records, subdomain_name),
        email_smtp=detect_email_smtp(records, subdomain_name),
        domain_created_at=_parse_ts(domain_created_at),
        domain_updated_at=_parse_ts(domain_updated_at),
        zone_updated_at=_parse_ts(zone_updated_at),
        synced_at=synced_at,
    )


def _parse_ts(value: str | None) -> datetime | None:
    """DNSimple timestamps are ISO 8601 with a trailing Z; None stays None."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _filter_records_for_subdomain(
    records: list[DNSRecord],
    subdomain: str,
) -> list[DNSRecord]:
    """
    Filter records that belong to a specific subdomain.

    This includes:
    - Records where name exactly matches the subdomain
    - Records where name starts with subdomain. (e.g., "_dmarc.subdomain")
    """
    filtered = []
    sub_lower = subdomain.lower()

    for record in records:
        name = (record.name or "").lower()

        # Exact match
        if name == sub_lower:
            filtered.append(record)
            continue

        # Records ending with .subdomain (e.g., resend._domainkey.send)
        if name.endswith(f".{sub_lower}"):
            filtered.append(record)
            continue

    return filtered
