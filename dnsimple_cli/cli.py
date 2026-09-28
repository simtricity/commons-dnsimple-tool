"""CLI commands for DNSimple domain management."""

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from dnsimple_cli.api import DNSimpleClient, DNSimpleAPIError
from dnsimple_cli.models import DomainTransfer
from dnsimple_cli.storage import Storage, get_state_dir, process_api_data

# Load credentials from the state dir (see get_state_dir). Variables already set in the
# environment win, so `doppler run -- dnsimple …` works unchanged.
ENV_FILE = get_state_dir() / "dnsimple.env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    # fallback: legacy ./.env in the current directory, for setups that predate the state dir
    load_dotenv(Path.cwd() / ".env")

app = typer.Typer(help="DNSimple Domain Manager CLI")
console = Console()


def get_client() -> DNSimpleClient:
    """Create and return a DNSimple client from environment variables."""
    account_id = os.getenv("DNSIMPLE_ACCOUNT_ID")
    access_token = os.getenv("DNSIMPLE_ACCESS_TOKEN")

    if not account_id or not access_token:
        console.print(
            "[red]Error:[/red] Missing DNSIMPLE_ACCOUNT_ID or DNSIMPLE_ACCESS_TOKEN "
            f"(looked in the environment, {ENV_FILE}, then ./.env)"
        )
        raise typer.Exit(1)

    if access_token == "your_access_token_here":
        console.print(
            f"[red]Error:[/red] Please set your actual DNSIMPLE_ACCESS_TOKEN in {ENV_FILE}"
        )
        raise typer.Exit(1)

    return DNSimpleClient(account_id, access_token)


def check_whois_transfer_lock(domain_name: str) -> tuple[bool, list[str]]:
    """Check WHOIS for transfer lock status.

    Returns:
        Tuple of (is_locked, list of domain statuses found).
        Returns (False, []) if WHOIS lookup fails.
    """
    try:
        result = subprocess.run(
            ["whois", domain_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        statuses = []
        for line in result.stdout.splitlines():
            if line.strip().lower().startswith("domain status:"):
                status = line.split(":", 1)[1].strip().split()[0]
                statuses.append(status)

        is_locked = any("transferprohibited" in s.lower() for s in statuses)
        return is_locked, statuses
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, []


def _sync_zone(client: DNSimpleClient, storage: Storage, zone_name: str) -> None:
    """Re-sync a single zone after record changes.

    Fetches fresh records from the API, re-runs detection, and upserts
    the domain + subdomains in storage without touching other domains.
    """
    from dnsimple_cli.models import DNSRecord, extract_subdomains_with_email

    console.print(f"[dim]Syncing {zone_name}...[/dim]", end=" ")

    # Fetch fresh data for this zone
    api_records = client.list_records(zone_name)
    api_zone = client.get_zone(zone_name)

    # Get existing domain info (for state, created_at, etc.)
    existing = storage.get_domain(zone_name)
    state = existing.state if existing else "registered"
    domain_created_at = existing.domain_created_at if existing else None
    domain_updated_at = existing.domain_updated_at if existing else None

    # Convert to DNSRecord objects
    records = [DNSRecord.from_api(r) for r in api_records]

    # Re-run detection via process helper
    from dnsimple_cli.storage import _create_domain

    now = datetime.now()
    primary = _create_domain(
        name=zone_name,
        parent_domain=None,
        state=state,
        records=records,
        domain_created_at=domain_created_at.isoformat() if domain_created_at else None,
        domain_updated_at=domain_updated_at.isoformat() if domain_updated_at else None,
        zone_updated_at=api_zone.get("updated_at"),
        synced_at=now,
    )

    # Build subdomains
    from dnsimple_cli.storage import _filter_records_for_subdomain

    subdomain_names = extract_subdomains_with_email(records)
    subdomains = []
    for sub_name in subdomain_names:
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
        subdomains.append(subdomain)

    storage.save_domain(primary, subdomains)
    console.print("[green]done[/green]")


def require_sync() -> Storage:
    """Get storage and ensure data exists."""
    storage = Storage()
    metadata = storage.get_metadata()
    if not metadata:
        console.print("[yellow]No sync data found. Run 'dnsimple sync' first.[/yellow]")
        storage.close()
        raise typer.Exit(1)
    return storage


# =============================================================================
# Commands
# =============================================================================


@app.command()
def whoami():
    """Verify API credentials and show account information."""
    try:
        with get_client() as client:
            info = client.whoami()
            console.print("[green]Authentication successful![/green]")

            if "account" in info:
                account = info["account"]
                console.print(f"Account ID: {account.get('id')}")
                console.print(f"Email: {account.get('email')}")
                console.print(f"Plan: {account.get('plan_identifier')}")
            elif "user" in info:
                user = info["user"]
                console.print(f"User ID: {user.get('id')}")
                console.print(f"Email: {user.get('email')}")
    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command()
def sync(
    force: Annotated[
        bool, typer.Option("--force", help="Force full sync of all zones")
    ] = False,
):
    """Sync domains and DNS records from DNSimple.

    Uses incremental sync by default - only fetches records for zones that
    have changed since the last sync. Use --force to force a complete refresh.
    """
    try:
        with get_client() as client:
            storage = Storage()
            existing_domains = {d.name: d for d in storage.load_domains(include_subdomains=False)}
            has_existing_data = len(existing_domains) > 0

            # Determine if we need full sync
            if force or not has_existing_data:
                if force:
                    console.print("[bold]Full sync requested...[/bold]")
                else:
                    console.print("[bold]No existing data, performing full sync...[/bold]")
                _full_sync(client, storage)
            else:
                _incremental_sync(client, storage, existing_domains)

            storage.close()

    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


def _full_sync(client: DNSimpleClient, storage: Storage) -> None:
    """Perform a full sync of all domains and records."""
    console.print("[bold]Fetching domains from DNSimple...[/bold]")

    # Fetch all domains
    api_domains = client.list_domains()
    console.print(f"Found {len(api_domains)} domains")

    # Fetch zones and records for each domain
    api_zones: dict[str, dict] = {}
    api_records: dict[str, list] = {}
    for domain in api_domains:
        domain_name = domain["name"]
        console.print(f"  Fetching zone & records for {domain_name}...")
        api_zones[domain_name] = client.get_zone(domain_name)
        api_records[domain_name] = client.list_records(domain_name)

    # Process and save
    console.print("\n[bold]Processing records...[/bold]")
    domains, metadata = process_api_data(
        api_domains, api_zones, api_records, client.account_id
    )

    storage.save_domains(domains)
    storage.save_metadata(metadata)

    console.print(
        f"\n[green]Full sync: {metadata.domain_count} domains "
        f"({metadata.subdomain_count} subdomains)[/green]"
    )


def _incremental_sync(
    client: DNSimpleClient,
    storage: Storage,
    existing_domains: dict,
) -> None:
    """Perform incremental sync - only fetch records for changed zones."""
    from dnsimple_cli.models import Domain

    console.print("[bold]Checking for changes...[/bold]")

    # Fetch zones list (lightweight - just metadata)
    api_zones_list = client.list_zones()
    api_domains = client.list_domains()

    # Build lookup of current zone timestamps
    current_zones = {z["name"]: z for z in api_zones_list}
    api_domains_by_name = {d["name"]: d for d in api_domains}

    # Compare with stored timestamps to find changed zones
    changed_zones: list[str] = []
    unchanged_zones: list[str] = []

    for zone_name, zone_data in current_zones.items():
        stored_domain = existing_domains.get(zone_name)
        if stored_domain is None:
            # New zone
            changed_zones.append(zone_name)
            console.print(f"  [cyan]NEW:[/cyan] {zone_name}")
        elif stored_domain.zone_updated_at is None:
            # No stored timestamp, need to fetch
            changed_zones.append(zone_name)
            console.print(f"  [yellow]NO TIMESTAMP:[/yellow] {zone_name}")
        else:
            # Compare timestamps
            stored_ts = stored_domain.zone_updated_at.isoformat().replace("+00:00", "Z")
            current_ts = zone_data.get("updated_at", "")

            # Normalize for comparison (remove microseconds if present)
            if stored_ts.rstrip("Z") != current_ts.rstrip("Z"):
                changed_zones.append(zone_name)
                console.print(f"  [yellow]CHANGED:[/yellow] {zone_name}")
            else:
                unchanged_zones.append(zone_name)

    # Check for deleted zones
    current_zone_names = set(current_zones.keys())
    stored_zone_names = set(existing_domains.keys())
    deleted_zones = stored_zone_names - current_zone_names
    for zone_name in deleted_zones:
        console.print(f"  [red]DELETED:[/red] {zone_name}")

    if not changed_zones and not deleted_zones:
        console.print("\n[green]No changes detected. Everything up to date.[/green]")
        return

    console.print(f"\n[bold]Fetching {len(changed_zones)} changed zone(s)...[/bold]")

    # Fetch records only for changed zones
    api_zones: dict[str, dict] = {}
    api_records: dict[str, list] = {}
    for zone_name in changed_zones:
        console.print(f"  Fetching records for {zone_name}...")
        api_zones[zone_name] = current_zones[zone_name]
        api_records[zone_name] = client.list_records(zone_name)

    # Process changed zones
    changed_api_domains = [api_domains_by_name[z] for z in changed_zones]
    new_domains, _ = process_api_data(
        changed_api_domains, api_zones, api_records, client.account_id
    )

    # Merge: keep unchanged domains, replace changed ones
    final_domains: list[Domain] = []
    new_domains_by_name = {d.name: d for d in new_domains}

    # Add unchanged primary domains and their subdomains
    for zone_name in unchanged_zones:
        # Add primary domain
        final_domains.append(existing_domains[zone_name])
        # Add its subdomains
        for sub in storage.get_subdomains(zone_name):
            final_domains.append(sub)

    # Add new/changed domains (includes subdomains from process_api_data)
    final_domains.extend(new_domains)

    # Create metadata
    from datetime import datetime
    from dnsimple_cli.models import SyncMetadata

    primary_count = len([d for d in final_domains if d.parent_domain is None])
    subdomain_count = len([d for d in final_domains if d.parent_domain is not None])

    metadata = SyncMetadata(
        synced_at=datetime.now(),
        account_id=client.account_id,
        domain_count=primary_count,
        subdomain_count=subdomain_count,
    )

    storage.save_domains(final_domains)
    storage.save_metadata(metadata)

    console.print(
        f"\n[green]Incremental sync: {len(changed_zones)} zone(s) updated, "
        f"{len(unchanged_zones)} unchanged[/green]"
    )


@app.command()
def domains(
    all_domains: Annotated[
        bool, typer.Option("--all", help="Include subdomains")
    ] = False,
):
    """List all synced domains."""
    storage = require_sync()

    try:
        domain_list = storage.load_domains(include_subdomains=all_domains)
        metadata = storage.get_metadata()

        table = Table(title="Domains")
        table.add_column("Domain", style="cyan")
        table.add_column("Parent", style="dim")
        table.add_column("Records", justify="right")
        table.add_column("Services", justify="right")
        table.add_column("Apps", justify="right")
        table.add_column("MX")
        table.add_column("SMTP", justify="right")

        for domain in sorted(domain_list, key=lambda d: (d.parent_domain or "", d.name)):
            table.add_row(
                domain.name,
                domain.parent_domain or "",
                str(len(domain.records)),
                str(len(domain.services)),
                str(len(domain.apps)),
                domain.email_mx.provider if domain.email_mx else "-",
                str(len(domain.email_smtp)),
            )

        console.print(table)
        if metadata:
            console.print(f"\nSynced at: {metadata.synced_at.isoformat()}")
            if not all_domains:
                console.print("[dim]Use --all to include subdomains[/dim]")
    finally:
        storage.close()


@app.command()
def domain(domain_name: str):
    """Show details for a specific domain."""
    storage = require_sync()

    try:
        dom = storage.get_domain(domain_name)
        if not dom:
            console.print(f"[red]Domain '{domain_name}' not found.[/red]")
            raise typer.Exit(1)

        console.print(f"\n[bold cyan]{dom.name}[/bold cyan]")
        if dom.parent_domain:
            console.print(f"Parent: {dom.parent_domain}")
        console.print(f"State: {dom.state}")
        console.print(f"Records: {len(dom.records)}")

        # Services
        if dom.services:
            console.print("\n[bold]Services (Domain Verification):[/bold]")
            for svc in dom.services:
                console.print(f"  • {svc.provider}: {svc.service_type}")

        # Apps
        if dom.apps:
            console.print("\n[bold]Apps (Traffic Routing):[/bold]")
            for app in dom.apps:
                console.print(f"  • {app.subdomain} → {app.provider} ({app.target})")

        # Email MX
        if dom.email_mx:
            console.print(f"\n[bold]Email MX (Receiving):[/bold] {dom.email_mx.provider}")
            for mx in dom.email_mx.mx_records:
                console.print(f"  • {mx}")

        # Email SMTP
        if dom.email_smtp:
            console.print("\n[bold]Email SMTP (Sending):[/bold]")
            for smtp in dom.email_smtp:
                indicators = []
                if smtp.has_spf:
                    indicators.append("SPF")
                if smtp.has_dkim:
                    indicators.append("DKIM")
                console.print(f"  • {smtp.provider} ({', '.join(indicators)})")

        # Subdomains
        subdomains = storage.get_subdomains(domain_name)
        if subdomains:
            console.print(f"\n[bold]Subdomains:[/bold] {len(subdomains)}")
            for sub in subdomains:
                console.print(f"  • {sub.name}")

        console.print()

    finally:
        storage.close()


@app.command()
def services():
    """List all services (domain verifications) across domains."""
    storage = require_sync()

    try:
        domain_list = storage.load_domains(include_subdomains=False)

        table = Table(title="Services (Domain Ownership Verification)")
        table.add_column("Domain", style="cyan")
        table.add_column("Provider")
        table.add_column("Type")
        table.add_column("Record", style="dim")

        for domain in sorted(domain_list, key=lambda d: d.name):
            for svc in domain.services:
                table.add_row(
                    domain.name,
                    svc.provider,
                    svc.service_type,
                    svc.record_name,
                )

        console.print(table)

    finally:
        storage.close()


@app.command()
def apps():
    """List all apps (traffic routing) across domains."""
    storage = require_sync()

    try:
        domain_list = storage.load_domains(include_subdomains=True)

        table = Table(title="Apps (Traffic Routing)")
        table.add_column("Domain", style="cyan")
        table.add_column("Subdomain")
        table.add_column("Provider")
        table.add_column("Target", style="dim")

        for domain in sorted(domain_list, key=lambda d: d.name):
            for app in domain.apps:
                table.add_row(
                    domain.name,
                    app.subdomain,
                    app.provider,
                    app.target,
                )

        console.print(table)

    finally:
        storage.close()


@app.command()
def email(
    mx_only: Annotated[
        bool, typer.Option("--mx", help="Show only MX (receiving) providers")
    ] = False,
    smtp_only: Annotated[
        bool, typer.Option("--smtp", help="Show only SMTP (sending) providers")
    ] = False,
):
    """List email configuration across domains."""
    storage = require_sync()

    try:
        domain_list = storage.load_domains(include_subdomains=True)

        if not mx_only and not smtp_only:
            # Show both
            mx_only = True
            smtp_only = True

        if mx_only:
            table = Table(title="Email MX (Receiving)")
            table.add_column("Domain", style="cyan")
            table.add_column("Provider")
            table.add_column("MX Records", style="dim")

            for domain in sorted(domain_list, key=lambda d: d.name):
                if domain.email_mx:
                    mx_list = ", ".join(domain.email_mx.mx_records[:2])
                    if len(domain.email_mx.mx_records) > 2:
                        mx_list += f" (+{len(domain.email_mx.mx_records) - 2})"
                    table.add_row(
                        domain.name,
                        domain.email_mx.provider,
                        mx_list,
                    )

            console.print(table)
            console.print()

        if smtp_only:
            table = Table(title="Email SMTP (Sending)")
            table.add_column("Domain", style="cyan")
            table.add_column("Provider")
            table.add_column("SPF", justify="center")
            table.add_column("DKIM", justify="center")
            table.add_column("DKIM Record", style="dim")

            for domain in sorted(domain_list, key=lambda d: d.name):
                for smtp in domain.email_smtp:
                    table.add_row(
                        domain.name,
                        smtp.provider,
                        "✓" if smtp.has_spf else "",
                        "✓" if smtp.has_dkim else "",
                        smtp.dkim_record or "",
                    )

            console.print(table)

    finally:
        storage.close()


@app.command()
def records(domain_name: str):
    """Show all DNS records for a domain."""
    storage = require_sync()

    try:
        dom = storage.get_domain(domain_name)
        if not dom:
            console.print(f"[red]Domain '{domain_name}' not found.[/red]")
            raise typer.Exit(1)

        table = Table(title=f"Records for {domain_name}")
        table.add_column("Name", style="cyan")
        table.add_column("Type")
        table.add_column("Content")
        table.add_column("TTL", justify="right")

        for record in sorted(dom.records, key=lambda r: (r.type, r.name)):
            name = record.name or "@"
            content = record.content
            # Truncate long content
            if len(content) > 60:
                content = content[:57] + "..."
            table.add_row(
                name,
                record.type,
                content,
                str(record.ttl),
            )

        console.print(table)

    finally:
        storage.close()


@app.command()
def report():
    """Generate HTML audit report with recommendations.

    Requires a report-recommendations.json file generated by Claude.
    This file contains domain groupings and AI-generated recommendations.
    """
    from dnsimple_cli.report import ReportError, generate_report, get_recommendations_path

    # Check for sync data first
    storage = require_sync()
    storage.close()

    try:
        output_path = generate_report()
        console.print(f"\n[green]Report generated:[/green] {output_path}")
    except ReportError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


# =============================================================================
# Transfer Commands
# =============================================================================


@app.command()
def contacts():
    """List contacts (registrants) in your DNSimple account."""
    try:
        with get_client() as client:
            contact_list = client.list_contacts()

            if not contact_list:
                console.print(
                    "[yellow]No contacts found. Create one at dnsimple.com first.[/yellow]"
                )
                raise typer.Exit(1)

            table = Table(title="Contacts (Registrants)")
            table.add_column("ID", style="bold cyan", justify="right")
            table.add_column("Name")
            table.add_column("Organisation")
            table.add_column("Email")
            table.add_column("Country")

            for c in contact_list:
                table.add_row(
                    str(c["id"]),
                    f"{c['first_name']} {c['last_name']}",
                    c.get("organization_name", ""),
                    c["email"],
                    c.get("country", ""),
                )

            console.print(table)
            console.print(
                "\n[dim]Use the contact ID as --registrant-id when transferring domains.[/dim]"
            )

    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("transfer-check")
def transfer_check(
    domain_name: Annotated[
        str, typer.Argument(help="Domain to check (e.g. example.net)")
    ],
):
    """Check if a domain can be transferred and show pricing."""
    try:
        with get_client() as client:
            tld = domain_name.split(".")[-1]

            console.print(
                f"\n[bold]Transfer pre-flight check for {domain_name}[/bold]\n"
            )

            # Check TLD supports transfer
            console.print(f"Checking .{tld} TLD support...", end=" ")
            tld_info = client.get_tld(tld)
            if tld_info.get("transfer_enabled"):
                console.print(f"[green]OK[/green] - .{tld} supports transfers")
            else:
                console.print(
                    f"[red]FAILED[/red] - .{tld} does not support transfers"
                )
                raise typer.Exit(1)

            # Get transfer pricing
            console.print("Checking transfer price...", end=" ")
            try:
                prices = client.get_domain_prices(domain_name, action="transfer")
                price = prices.get("transfer_price", prices.get("price", "unknown"))
                premium = prices.get("premium", False)
                price_display = f"[green]${price}/year[/green]"
                if premium:
                    price_display += " [yellow](PREMIUM)[/yellow]"
                console.print(price_display)
            except DNSimpleAPIError as e:
                if e.status_code == 400:
                    console.print(
                        "[yellow]Price unavailable (domain may not be registered elsewhere)[/yellow]"
                    )
                else:
                    raise

            # WHOIS transfer lock check
            console.print("Checking WHOIS transfer lock...", end=" ")
            is_locked, statuses = check_whois_transfer_lock(domain_name)
            if is_locked:
                console.print(
                    "[red]LOCKED[/red] - domain has clientTransferProhibited status"
                )
                console.print(
                    "[yellow]You must unlock this domain at your current registrar before transferring.[/yellow]"
                )
            elif statuses:
                console.print("[green]OK[/green] - no transfer lock detected")
            else:
                console.print("[yellow]SKIPPED[/yellow] - could not query WHOIS")

            # Preparation checklist
            console.print("\n[bold yellow]Preparation Checklist (at current registrar):[/bold yellow]")
            console.print("  1. Log in and navigate to Domain Settings")
            console.print("  2. Turn OFF Domain Lock (under Security)")
            console.print("  3. Get the Auth/EPP Code (under Transfer)")
            console.print("  4. Disable WHOIS privacy if enabled")
            console.print("  5. Ensure the domain is more than 60 days old")
            console.print("  6. Ensure the domain does not expire within 15 days")
            console.print(
                f'\n[dim]When ready: dnsimple transfer {domain_name} --auth-code "YOUR_CODE"[/dim]'
            )

    except DNSimpleAPIError as e:
        console.print(f"\n[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command()
def transfer(
    domain_name: Annotated[
        str, typer.Argument(help="Domain to transfer (e.g. example.net)")
    ],
    auth_code: Annotated[
        str, typer.Option("--auth-code", help="Auth/EPP code from current registrar")
    ],
    registrant_id: Annotated[
        int | None,
        typer.Option(
            "--registrant-id",
            help="Contact ID for registrant (run 'contacts' to see IDs)",
        ),
    ] = None,
    auto_renew: Annotated[
        bool,
        typer.Option("--auto-renew/--no-auto-renew", help="Enable auto-renewal"),
    ] = True,
    whois_privacy: Annotated[
        bool,
        typer.Option(
            "--whois-privacy/--no-whois-privacy", help="Enable WHOIS privacy"
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
):
    """Transfer a domain from another registrar to DNSimple."""
    try:
        with get_client() as client:
            # Auto-select registrant if only one contact exists
            if registrant_id is None:
                contact_list = client.list_contacts()
                if len(contact_list) == 0:
                    console.print(
                        "[red]No contacts found. Create one at dnsimple.com first.[/red]"
                    )
                    raise typer.Exit(1)
                elif len(contact_list) == 1:
                    registrant_id = contact_list[0]["id"]
                    name = f"{contact_list[0]['first_name']} {contact_list[0]['last_name']}"
                    console.print(f"Using contact: {name} (ID: {registrant_id})")
                else:
                    console.print(
                        "[yellow]Multiple contacts found. Please specify --registrant-id:[/yellow]"
                    )
                    for c in contact_list:
                        console.print(
                            f"  {c['id']}: {c['first_name']} {c['last_name']} ({c['email']})"
                        )
                    raise typer.Exit(1)

            # Check for existing active transfer
            storage = Storage()
            existing = storage.get_transfer_by_domain(domain_name)
            if existing and existing.state not in ("transferred", "cancelled"):
                console.print(
                    f"[yellow]Active transfer already exists for {domain_name} "
                    f"(ID: {existing.id}, state: {existing.state})[/yellow]"
                )
                console.print("Use 'dnsimple transfer-status' to check progress.")
                storage.close()
                raise typer.Exit(1)

            # Get pricing for confirmation
            try:
                prices = client.get_domain_prices(domain_name, action="transfer")
                price = prices.get("transfer_price", prices.get("price", "unknown"))
            except DNSimpleAPIError:
                price = "unknown"

            # Confirmation
            if not yes:
                console.print(f"\n[bold]Transfer Summary:[/bold]")
                console.print(f"  Domain:        {domain_name}")
                console.print(f"  Registrant:    ID {registrant_id}")
                console.print(f"  Auto-renew:    {'Yes' if auto_renew else 'No'}")
                console.print(f"  WHOIS privacy: {'Yes' if whois_privacy else 'No'}")
                console.print(f"  Cost:          ${price}")
                console.print()
                confirmed = typer.confirm("Proceed with transfer?")
                if not confirmed:
                    console.print("[yellow]Transfer cancelled.[/yellow]")
                    storage.close()
                    raise typer.Exit(0)

            # WHOIS pre-flight: check for transfer lock before hitting API
            console.print("\nChecking WHOIS transfer lock...", end=" ")
            is_locked, statuses = check_whois_transfer_lock(domain_name)
            if is_locked:
                console.print(
                    "[red]LOCKED[/red] - domain has clientTransferProhibited status"
                )
                console.print(
                    "[red]Aborting:[/red] Unlock the domain at your current registrar first."
                )
                storage.close()
                raise typer.Exit(1)
            elif statuses:
                console.print("[green]OK[/green]")
            else:
                console.print("[yellow]SKIPPED[/yellow] - could not query WHOIS")

            # Initiate transfer
            console.print(
                f"[bold]Initiating transfer for {domain_name}...[/bold]"
            )
            if registrant_id is None:  # every branch above sets it or exits
                raise typer.Exit(1)
            result = client.transfer_domain(
                domain=domain_name,
                registrant_id=registrant_id,
                auth_code=auth_code,
                auto_renew=auto_renew,
                whois_privacy=whois_privacy,
            )

            # Save to local tracking
            transfer_obj = DomainTransfer.from_api(result, domain_name)
            transfer_obj.initiated_at = datetime.now()
            transfer_obj.last_checked_at = datetime.now()

            storage.save_transfer(transfer_obj)
            storage.close()

            console.print(f"\n[green]Transfer initiated![/green]")
            console.print(f"  Transfer ID: {result['id']}")
            console.print(f"  State:       {result['state']}")
            if result.get("status_description"):
                console.print(f"  Status:      {result['status_description']}")

            console.print("\n[bold yellow]Next steps:[/bold yellow]")
            console.print(
                "  1. Your current registrar will send a confirmation email - approve it"
            )
            console.print("  2. Transfer typically takes 1-7 days")
            console.print("  3. Check progress: dnsimple transfer-status --refresh")

    except DNSimpleAPIError as e:
        console.print(f"\n[red]API Error:[/red] {e.message}")
        if "auth" in e.message.lower() or "code" in e.message.lower():
            console.print(
                "[dim]Hint: Check that the auth/EPP code is correct and the domain is unlocked.[/dim]"
            )
        raise typer.Exit(1)


@app.command("transfer-status")
def transfer_status(
    domain_name: Annotated[
        str | None, typer.Argument(help="Specific domain to check (optional)")
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Refresh status from DNSimple API"),
    ] = False,
):
    """Show status of domain transfers."""
    storage = Storage()

    try:
        if domain_name:
            t = storage.get_transfer_by_domain(domain_name)
            transfers = [t] if t else []
            if not transfers:
                console.print(
                    f"[yellow]No transfer found for {domain_name}.[/yellow]"
                )
                raise typer.Exit(1)
        else:
            transfers = storage.get_all_transfers()
            if not transfers:
                console.print(
                    "[yellow]No transfers tracked. Use 'dnsimple transfer' to start one.[/yellow]"
                )
                raise typer.Exit(0)

        # Optionally refresh from API
        if refresh:
            console.print("[bold]Refreshing transfer status...[/bold]")
            with get_client() as client:
                for t in transfers:
                    if t.state not in ("transferred", "cancelled"):
                        console.print(
                            f"  Refreshing {t.domain_name}...", end=" "
                        )
                        try:
                            result = client.get_transfer(t.domain_name, t.id)
                            t.state = result["state"]
                            t.status_description = result.get(
                                "status_description", ""
                            )
                            raw_updated = result.get("updated_at")
                            if raw_updated:
                                t.updated_at = datetime.fromisoformat(
                                    raw_updated.replace("Z", "+00:00")
                                )
                            t.last_checked_at = datetime.now()
                            storage.save_transfer(t)
                            console.print(f"[green]{t.state}[/green]")
                        except DNSimpleAPIError as e:
                            console.print(f"[red]error: {e.message}[/red]")
            console.print()

        # Display table
        table = Table(title="Domain Transfers")
        table.add_column("Domain", style="cyan")
        table.add_column("Transfer ID", justify="right")
        table.add_column("State")
        table.add_column("Status")
        table.add_column("Initiated")
        table.add_column("Last Checked", style="dim")

        state_styles = {
            "transferring": "[yellow]transferring[/yellow]",
            "transferred": "[green]transferred[/green]",
            "cancelled": "[red]cancelled[/red]",
        }

        for t in sorted(transfers, key=lambda x: x.domain_name):
            state_display = state_styles.get(t.state, t.state)
            initiated = (
                t.initiated_at.strftime("%Y-%m-%d %H:%M")
                if t.initiated_at
                else "-"
            )
            checked = (
                t.last_checked_at.strftime("%Y-%m-%d %H:%M")
                if t.last_checked_at
                else "-"
            )

            table.add_row(
                t.domain_name,
                str(t.id),
                state_display,
                t.status_description or "-",
                initiated,
                checked,
            )

        console.print(table)

        # Hint for active transfers
        active = [
            t for t in transfers if t.state not in ("transferred", "cancelled")
        ]
        if active and not refresh:
            console.print(
                "\n[dim]Use --refresh to update status from DNSimple API.[/dim]"
            )

    finally:
        storage.close()


@app.command("transfer-cancel")
def transfer_cancel(
    domain_name: Annotated[
        str, typer.Argument(help="Domain to cancel transfer for")
    ],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation")
    ] = False,
):
    """Cancel a pending domain transfer."""
    storage = Storage()

    try:
        transfer_obj = storage.get_transfer_by_domain(domain_name)
        if not transfer_obj:
            console.print(f"[red]No transfer found for {domain_name}.[/red]")
            raise typer.Exit(1)

        if transfer_obj.state in ("transferred", "cancelled"):
            console.print(
                f"[yellow]Transfer is already {transfer_obj.state}.[/yellow]"
            )
            raise typer.Exit(0)

        if not yes:
            confirmed = typer.confirm(f"Cancel transfer for {domain_name}?")
            if not confirmed:
                raise typer.Exit(0)

        with get_client() as client:
            result = client.cancel_transfer(domain_name, transfer_obj.id)
            transfer_obj.state = result.get("state", "cancelled")
            transfer_obj.status_description = result.get(
                "status_description", ""
            )
            transfer_obj.last_checked_at = datetime.now()
            storage.save_transfer(transfer_obj)

            console.print(
                f"[green]Transfer for {domain_name} has been cancelled.[/green]"
            )

    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)
    finally:
        storage.close()


# =============================================================================
# Record Editing Commands
# =============================================================================


@app.command("email-setup")
def email_setup(
    domain_name: Annotated[
        str, typer.Argument(help="Domain to configure email for")
    ],
    provider: Annotated[
        str,
        typer.Option(
            "--provider",
            "-p",
            help="Email provider: google (Google Workspace) or improvmx",
        ),
    ] = "google",
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
):
    """Set up email DNS records for a domain.

    Creates MX and SPF records for the specified email provider.
    Google Workspace is the default.
    Use --provider improvmx for forwarding-only mail via ImprovMX.
    """
    from dnsimple_cli.email_setup import (
        check_existing_email,
        get_provider,
        get_records_to_create,
        merge_spf,
    )

    try:
        provider_config = get_provider(provider)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    try:
        with get_client() as client:
            # Fetch current records from API (live, not cached)
            console.print(f"Checking current records for {domain_name}...")
            current_records = client.list_records(domain_name)

            # Check for existing email config
            existing = check_existing_email(current_records)

            if existing["has_mx"]:
                mx_provider = existing["mx_provider"] or "unknown"
                console.print(
                    f"[red]Error:[/red] {domain_name} already has MX records "
                    f"(provider: {mx_provider})"
                )
                console.print(
                    "[dim]Remove existing MX records first if you want to switch providers.[/dim]"
                )
                raise typer.Exit(1)

            # Determine SPF action
            spf_action = "create"  # create, merge, or skip
            spf_content = provider_config["spf"]

            if existing["has_spf"]:
                current_spf = existing["spf_content"]
                if provider_config["spf_include"].lower() in current_spf.lower():
                    console.print(
                        f"[yellow]SPF record already includes {provider_config['spf_include']}[/yellow]"
                    )
                    spf_action = "skip"
                else:
                    spf_content = merge_spf(
                        current_spf, provider_config["spf_include"]
                    )
                    spf_action = "merge"

            # Show what will be created
            records_to_create = get_records_to_create(provider)
            label = provider_config["label"]

            console.print(f"\n[bold]{label} email setup for {domain_name}[/bold]\n")

            table = Table(title="Records to create")
            table.add_column("Type", style="cyan")
            table.add_column("Name")
            table.add_column("Content")
            table.add_column("Priority", justify="right")

            for rec in records_to_create:
                if rec["type"] == "TXT" and spf_action == "skip":
                    continue
                name_display = "@" if rec["name"] == "" else rec["name"]
                pri_display = str(rec["priority"]) if rec["priority"] else "-"
                content_display = rec["content"]
                if rec["type"] == "TXT" and spf_action == "merge":
                    content_display = spf_content
                table.add_row(
                    rec["type"],
                    name_display,
                    content_display,
                    pri_display,
                )

            console.print(table)

            if spf_action == "merge":
                console.print(
                    f"\n[yellow]Existing SPF will be updated:[/yellow]"
                )
                console.print(f"  Old: {existing['spf_content']}")
                console.print(f"  New: {spf_content}")

            # Confirmation
            if not yes:
                console.print()
                confirmed = typer.confirm("Proceed with email setup?")
                if not confirmed:
                    console.print("[yellow]Setup cancelled.[/yellow]")
                    raise typer.Exit(0)

            # Create records
            console.print(f"\n[bold]Creating records...[/bold]")
            created_count = 0

            for rec in records_to_create:
                if rec["type"] == "TXT" and spf_action == "skip":
                    continue

                if rec["type"] == "TXT" and spf_action == "merge":
                    # Update existing SPF record
                    spf_record = existing["spf_record"]
                    record_id = spf_record.get("id")
                    console.print(
                        f"  Updating SPF record (ID: {record_id})...",
                        end=" ",
                    )
                    client.update_record(
                        domain_name, record_id, content=spf_content
                    )
                    console.print("[green]OK[/green]")
                    created_count += 1
                else:
                    name_display = "@" if rec["name"] == "" else rec["name"]
                    console.print(
                        f"  Creating {rec['type']} {name_display} → {rec['content']}...",
                        end=" ",
                    )
                    content = rec["content"]
                    if rec["type"] == "TXT" and spf_action == "create":
                        content = spf_content
                    result = client.create_record(
                        zone=domain_name,
                        record_type=rec["type"],
                        name=rec["name"],
                        content=content,
                        ttl=rec["ttl"],
                        priority=rec["priority"],
                    )
                    console.print(f"[green]OK[/green] (ID: {result['id']})")
                    created_count += 1

            console.print(
                f"\n[green]Email setup complete![/green] "
                f"{created_count} record(s) created/updated."
            )

            # Auto-sync this zone
            storage = Storage()
            _sync_zone(client, storage, domain_name)
            storage.close()

            # Next steps
            if provider_config["next_steps"]:
                console.print("\n[bold yellow]Next steps:[/bold yellow]")
                for i, step in enumerate(provider_config["next_steps"], 1):
                    console.print(f"  {i}. {step}")

    except DNSimpleAPIError as e:
        console.print(f"\n[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("redirect-setup")
def redirect_setup(
    domain_names: Annotated[
        list[str], typer.Argument(help="Domain(s) to set up redirects for")
    ],
    target: Annotated[
        str,
        typer.Option(
            "--target",
            "-t",
            help="Target URL to redirect to (e.g. https://www.example.com)",
        ),
    ],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
):
    """Set up domain redirects (naked + wildcard) to a target URL.

    Used for parked/alias domains that should redirect all traffic
    to a primary domain. Creates @ and * URL records.
    """
    from dnsimple_cli.redirect_setup import check_existing_redirects, get_redirect_records

    # Validate target looks like a URL
    if not target.startswith("https://"):
        console.print("[red]Error:[/red] Target must start with https://")
        raise typer.Exit(1)

    records_template = get_redirect_records(target)

    try:
        with get_client() as client:
            # Pre-flight: check all domains first
            domain_plans: list[dict] = []

            for domain_name in domain_names:
                console.print(f"Checking {domain_name}...")
                current_records = client.list_records(domain_name)
                existing = check_existing_redirects(current_records)

                to_create = []
                skipped = []

                for rec in records_template:
                    name = rec["name"]
                    if name == "" and existing["has_naked"]:
                        skipped.append(f"@ (existing {existing['naked_record']['type']})")
                        continue
                    if name == "*" and existing["has_wildcard"]:
                        skipped.append(f"* (existing {existing['wildcard_record']['type']})")
                        continue
                    to_create.append(rec)

                if existing["has_www"]:
                    www_rec = existing["www_record"]
                    console.print(
                        f"  [yellow]Note: {domain_name} has explicit www record "
                        f"({www_rec['type']} → {www_rec.get('content', '')}). "
                        f"Wildcard won't override it.[/yellow]"
                    )

                domain_plans.append({
                    "domain": domain_name,
                    "to_create": to_create,
                    "skipped": skipped,
                })

            # Show plan
            console.print(f"\n[bold]Redirect setup → {target}[/bold]\n")

            table = Table(title="Records to create")
            table.add_column("Domain", style="cyan")
            table.add_column("Type")
            table.add_column("Name")
            table.add_column("Target")

            total_count = 0
            for plan in domain_plans:
                for rec in plan["to_create"]:
                    name_display = "@" if rec["name"] == "" else rec["name"]
                    table.add_row(plan["domain"], rec["type"], name_display, rec["content"])
                    total_count += 1
                for skip_msg in plan["skipped"]:
                    table.add_row(plan["domain"], "[dim]skip[/dim]", f"[dim]{skip_msg}[/dim]", "")

            console.print(table)

            if total_count == 0:
                console.print("\n[yellow]Nothing to create — all records already exist.[/yellow]")
                raise typer.Exit(0)

            if not yes:
                console.print()
                confirmed = typer.confirm(f"Create {total_count} redirect record(s)?")
                if not confirmed:
                    console.print("[yellow]Setup cancelled.[/yellow]")
                    raise typer.Exit(0)

            # Create records
            console.print(f"\n[bold]Creating records...[/bold]")
            storage = Storage()

            for plan in domain_plans:
                for rec in plan["to_create"]:
                    name_display = "@" if rec["name"] == "" else rec["name"]
                    console.print(
                        f"  {plan['domain']}: {rec['type']} {name_display} → {rec['content']}...",
                        end=" ",
                    )
                    result = client.create_record(
                        zone=plan["domain"],
                        record_type=rec["type"],
                        name=rec["name"],
                        content=rec["content"],
                        ttl=rec["ttl"],
                        priority=rec["priority"],
                    )
                    console.print(f"[green]OK[/green] (ID: {result['id']})")

                # Auto-sync each zone
                _sync_zone(client, storage, plan["domain"])

            storage.close()

            console.print(
                f"\n[green]Redirect setup complete![/green] "
                f"{total_count} record(s) created across {len(domain_names)} domain(s)."
            )

    except DNSimpleAPIError as e:
        console.print(f"\n[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("app-setup")
def app_setup(
    domain_name: Annotated[
        str, typer.Argument(help="Domain to configure Deno Deploy for")
    ],
    acme_token: Annotated[
        str,
        typer.Option(
            "--acme-token",
            help="ACME challenge token from Deno Deploy config (the hex string before ._acme.deno.net)",
        ),
    ],
    apex: Annotated[
        bool,
        typer.Option(
            "--apex",
            help="Use the apex/ALIAS pattern (ALIAS @ → alias.deno.net) instead of the www CNAME pattern",
        ),
    ] = False,
    with_www_acme_token: Annotated[
        str | None,
        typer.Option(
            "--with-www-acme-token",
            help="(With --apex only) Also add CNAME www → alias.deno.net plus its ACME CNAME using this token. "
            "Token comes from Deno Deploy after adding www.<domain> as a second domain. App must do the 301 to apex.",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
):
    """Set up Deno Deploy DNS records for a domain.

    Default: www CNAME + ACME CNAME + naked URL redirect.
    With --apex: ALIAS @ + ACME CNAME (no www, no redirect).
    With --apex --with-www-acme-token: also adds CNAME www + ACME for www
    (handled by Deno's own cert; app must implement the 301 to apex).
    Get ACME tokens from Deno Deploy → Domains → Configure.
    """
    from dnsimple_cli.app_setup import (
        check_existing_apex,
        check_existing_web,
        get_deno_apex_records,
        get_deno_records,
        get_deno_www_only_records,
    )

    if with_www_acme_token and not apex:
        console.print(
            "[red]Error:[/red] --with-www-acme-token requires --apex. "
            "For the www-only flow, run app-setup without --apex."
        )
        raise typer.Exit(1)

    try:
        with get_client() as client:
            console.print(f"Checking current records for {domain_name}...")
            current_records = client.list_records(domain_name)

            if apex:
                existing = check_existing_apex(current_records)

                if existing["has_apex"]:
                    apex_type = existing["apex_type"]
                    apex_content = existing["apex_record"].get("content", "")
                    console.print(
                        f"[red]Error:[/red] {domain_name} already has an apex record "
                        f"({apex_type} → {apex_content})"
                    )
                    console.print(
                        "[dim]Remove existing apex record first if you want to reconfigure.[/dim]"
                    )
                    raise typer.Exit(1)

                records_to_create = get_deno_apex_records(acme_token)
                skip_acme = existing["has_acme"]
                skip_www_acme = False

                if with_www_acme_token:
                    web = check_existing_web(current_records)
                    if web["has_www"]:
                        www_type = web["www_type"]
                        www_content = web["www_record"].get("content", "")
                        console.print(
                            f"[red]Error:[/red] {domain_name} already has a www record "
                            f"({www_type} → {www_content}). Remove it first if you want to reconfigure."
                        )
                        raise typer.Exit(1)
                    skip_www_acme = web["has_acme"]
                    records_to_create = records_to_create + get_deno_www_only_records(
                        with_www_acme_token
                    )

                console.print(
                    f"\n[bold]Deno Deploy apex setup for {domain_name}"
                    f"{' (with www)' if with_www_acme_token else ''}[/bold]\n"
                )

                table = Table(title="Records to create")
                table.add_column("Type", style="cyan")
                table.add_column("Name")
                table.add_column("Content")

                for rec in records_to_create:
                    if rec["name"] == "_acme-challenge" and skip_acme:
                        continue
                    if rec["name"] == "_acme-challenge.www" and skip_www_acme:
                        continue
                    name_display = "@" if rec["name"] == "" else rec["name"]
                    table.add_row(rec["type"], name_display, rec["content"])

                console.print(table)

                if skip_acme:
                    console.print(
                        "[yellow]Skipping apex ACME challenge (already exists)[/yellow]"
                    )
                if skip_www_acme:
                    console.print(
                        "[yellow]Skipping www ACME challenge (already exists)[/yellow]"
                    )

                if not yes:
                    console.print()
                    confirmed = typer.confirm("Proceed with Deno Deploy apex setup?")
                    if not confirmed:
                        console.print("[yellow]Setup cancelled.[/yellow]")
                        raise typer.Exit(0)

                console.print("\n[bold]Creating records...[/bold]")
                created_count = 0

                for rec in records_to_create:
                    if rec["name"] == "_acme-challenge" and skip_acme:
                        continue
                    if rec["name"] == "_acme-challenge.www" and skip_www_acme:
                        continue
                    name_display = "@" if rec["name"] == "" else rec["name"]
                    console.print(
                        f"  Creating {rec['type']} {name_display} → {rec['content']}...",
                        end=" ",
                    )
                    result = client.create_record(
                        zone=domain_name,
                        record_type=rec["type"],
                        name=rec["name"],
                        content=rec["content"],
                        ttl=rec["ttl"],
                        priority=rec["priority"],
                    )
                    console.print(f"[green]OK[/green] (ID: {result['id']})")
                    created_count += 1

                storage = Storage()
                _sync_zone(client, storage, domain_name)
                storage.close()

                console.print(
                    f"\n[green]Deno Deploy apex setup complete![/green] "
                    f"{created_count} record(s) created."
                )
                console.print("\n[bold yellow]Next steps:[/bold yellow]")
                console.print(
                    "  1. In Deno Deploy → Domains, click 'Verify DNS and provision certificate' "
                    f"for {domain_name}"
                    + (f" and www.{domain_name}" if with_www_acme_token else "")
                )
                console.print("  2. Assign the domain to your Deno Deploy project")
                if with_www_acme_token:
                    console.print(
                        f"  3. Add a 301 in your Deno app for the www host:\n"
                        f"     [dim]if (new URL(req.url).hostname === \"www.{domain_name}\") "
                        f'return Response.redirect("https://{domain_name}" + new URL(req.url).pathname, 301);[/dim]'
                    )
                return

            existing = check_existing_web(current_records)

            if existing["has_www"]:
                www_type = existing["www_type"]
                www_content = existing["www_record"].get("content", "")
                console.print(
                    f"[red]Error:[/red] {domain_name} already has a www record "
                    f"({www_type} → {www_content})"
                )
                console.print(
                    "[dim]Remove existing www record first if you want to reconfigure.[/dim]"
                )
                raise typer.Exit(1)

            records_to_create = get_deno_records(domain_name, acme_token)

            # Skip records that already exist
            skip_naked = existing["has_naked_redirect"]
            skip_acme = existing["has_acme"]

            console.print(f"\n[bold]Deno Deploy setup for {domain_name}[/bold]\n")

            table = Table(title="Records to create")
            table.add_column("Type", style="cyan")
            table.add_column("Name")
            table.add_column("Content")

            for rec in records_to_create:
                if rec["type"] == "URL" and skip_naked:
                    continue
                if rec["name"] == "_acme-challenge.www" and skip_acme:
                    continue
                name_display = "@" if rec["name"] == "" else rec["name"]
                table.add_row(rec["type"], name_display, rec["content"])

            console.print(table)

            if skip_naked:
                console.print(
                    f"[yellow]Skipping naked redirect (already exists)[/yellow]"
                )
            if skip_acme:
                console.print(
                    f"[yellow]Skipping ACME challenge (already exists)[/yellow]"
                )

            if not yes:
                console.print()
                confirmed = typer.confirm("Proceed with Deno Deploy setup?")
                if not confirmed:
                    console.print("[yellow]Setup cancelled.[/yellow]")
                    raise typer.Exit(0)

            console.print(f"\n[bold]Creating records...[/bold]")
            created_count = 0

            for rec in records_to_create:
                if rec["type"] == "URL" and skip_naked:
                    continue
                if rec["name"] == "_acme-challenge.www" and skip_acme:
                    continue

                name_display = "@" if rec["name"] == "" else rec["name"]
                console.print(
                    f"  Creating {rec['type']} {name_display} → {rec['content']}...",
                    end=" ",
                )
                result = client.create_record(
                    zone=domain_name,
                    record_type=rec["type"],
                    name=rec["name"],
                    content=rec["content"],
                    ttl=rec["ttl"],
                    priority=rec["priority"],
                )
                console.print(f"[green]OK[/green] (ID: {result['id']})")
                created_count += 1

            # Auto-sync this zone
            storage = Storage()
            _sync_zone(client, storage, domain_name)
            storage.close()

            console.print(
                f"\n[green]Deno Deploy setup complete![/green] "
                f"{created_count} record(s) created."
            )
            console.print("\n[bold yellow]Next steps:[/bold yellow]")
            console.print(
                "  1. In Deno Deploy → Domains, click 'Verify DNS and provision certificate'"
            )
            console.print(
                "  2. Assign the domain to your Deno Deploy project"
            )

    except DNSimpleAPIError as e:
        console.print(f"\n[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("clerk-setup")
def clerk_setup(
    domain_name: Annotated[
        str, typer.Argument(help="Domain to configure Clerk for")
    ],
    instance_id: Annotated[
        str,
        typer.Option(
            "--instance-id",
            "-i",
            help="Clerk instance ID (e.g. ceta5hgdrzyn from the Clerk dashboard DNS config)",
        ),
    ],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
):
    """Set up Clerk authentication DNS records for a domain.

    Creates 5 CNAME records: frontend API, account portal, email,
    and two DKIM records. Get the instance ID from Clerk → Configure → Domains.
    """
    import re

    from dnsimple_cli.clerk_setup import check_existing_clerk, get_clerk_records

    # Validate instance ID format
    if not re.match(r"^[a-z0-9]+$", instance_id):
        console.print(
            "[red]Error:[/red] Instance ID should be alphanumeric "
            f"(got '{instance_id}'). Check the Clerk dashboard."
        )
        raise typer.Exit(1)

    try:
        with get_client() as client:
            console.print(f"Checking current records for {domain_name}...")
            current_records = client.list_records(domain_name)

            existing = check_existing_clerk(current_records)

            # Report conflicts
            if existing["conflicts"]:
                console.print("\n[red]Conflicting records found:[/red]")
                for conflict in existing["conflicts"]:
                    rec = conflict["record"]
                    console.print(
                        f"  {rec.get('type')} {conflict['name']} → "
                        f"{rec.get('content', '')}"
                    )
                console.print(
                    "[dim]Remove conflicting records before running clerk-setup.[/dim]"
                )
                raise typer.Exit(1)

            # Report already-existing Clerk records
            if existing["existing"]:
                console.print(
                    f"[yellow]{len(existing['existing'])} Clerk record(s) already exist — will skip:[/yellow]"
                )
                for e in existing["existing"]:
                    console.print(f"  [dim]{e['name']} → {e['record'].get('content', '')}[/dim]")

            # Generate records and filter to those that need creating
            all_records = get_clerk_records(domain_name, instance_id)
            to_create = [
                r for r in all_records
                if r["name"].lower() not in existing["existing_names"]
            ]

            if not to_create:
                console.print(
                    "\n[green]All 5 Clerk records already exist. Nothing to do.[/green]"
                )
                raise typer.Exit(0)

            # Display plan
            console.print(f"\n[bold]Clerk setup for {domain_name}[/bold]\n")

            table = Table(title="CNAME records to create")
            table.add_column("Name", style="cyan")
            table.add_column("Label")
            table.add_column("Target")

            for rec in to_create:
                table.add_row(rec["name"], rec["label"], rec["content"])

            console.print(table)

            if not yes:
                console.print()
                confirmed = typer.confirm(
                    f"Create {len(to_create)} Clerk CNAME record(s)?"
                )
                if not confirmed:
                    console.print("[yellow]Setup cancelled.[/yellow]")
                    raise typer.Exit(0)

            # Create records
            console.print(f"\n[bold]Creating records...[/bold]")
            created_count = 0

            for rec in to_create:
                console.print(
                    f"  CNAME {rec['name']} → {rec['content']}...",
                    end=" ",
                )
                result = client.create_record(
                    zone=domain_name,
                    record_type="CNAME",
                    name=rec["name"],
                    content=rec["content"],
                    ttl=rec["ttl"],
                )
                console.print(f"[green]OK[/green] (ID: {result['id']})")
                created_count += 1

            # Auto-sync this zone
            storage = Storage()
            _sync_zone(client, storage, domain_name)
            storage.close()

            console.print(
                f"\n[green]Clerk setup complete![/green] "
                f"{created_count} record(s) created."
            )
            console.print("\n[bold yellow]Next steps:[/bold yellow]")
            console.print(
                "  1. In the Clerk dashboard, click 'Verify configuration' to confirm DNS records"
            )
            console.print(
                "  2. Wait for SSL certificates to be issued (shown on the same page)"
            )

    except DNSimpleAPIError as e:
        console.print(f"\n[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("record-create")
def record_create(
    zone: Annotated[str, typer.Argument(help="Domain/zone name")],
    record_type: Annotated[
        str, typer.Argument(help="Record type (MX, TXT, CNAME, A, etc.)")
    ],
    name: Annotated[
        str,
        typer.Argument(
            help='Record name ("" for root, "www", "mail", etc.)'
        ),
    ],
    content: Annotated[str, typer.Argument(help="Record content/value")],
    ttl: Annotated[
        int, typer.Option("--ttl", help="TTL in seconds")
    ] = 3600,
    priority: Annotated[
        int | None, typer.Option("--priority", help="Priority (for MX/SRV)")
    ] = None,
):
    """Create a single DNS record."""
    try:
        with get_client() as client:
            name_display = "@" if name == "" else name
            console.print(
                f"Creating {record_type} record: {name_display} → {content}"
            )

            result = client.create_record(
                zone=zone,
                record_type=record_type,
                name=name,
                content=content,
                ttl=ttl,
                priority=priority,
            )

            console.print(f"[green]Record created![/green] ID: {result['id']}")

            # Auto-sync this zone
            storage = Storage()
            _sync_zone(client, storage, zone)
            storage.close()

    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("record-delete")
def record_delete(
    zone: Annotated[str, typer.Argument(help="Domain/zone name")],
    record_id: Annotated[int, typer.Argument(help="Record ID to delete")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation")
    ] = False,
):
    """Delete a DNS record by ID."""
    try:
        with get_client() as client:
            # Fetch record details for confirmation
            all_records = client.list_records(zone)
            target = None
            for rec in all_records:
                if rec["id"] == record_id:
                    target = rec
                    break

            if not target:
                console.print(
                    f"[red]Record ID {record_id} not found in zone {zone}.[/red]"
                )
                raise typer.Exit(1)

            name_display = "@" if target["name"] == "" else target["name"]
            content = target["content"]
            if len(content) > 60:
                content = content[:57] + "..."

            console.print(f"\n[bold]Record to delete:[/bold]")
            console.print(f"  Zone:    {zone}")
            console.print(f"  ID:      {record_id}")
            console.print(f"  Type:    {target['type']}")
            console.print(f"  Name:    {name_display}")
            console.print(f"  Content: {content}")

            if not yes:
                console.print()
                confirmed = typer.confirm("Delete this record?")
                if not confirmed:
                    console.print("[yellow]Deletion cancelled.[/yellow]")
                    raise typer.Exit(0)

            client.delete_record(zone, record_id)
            console.print(f"\n[green]Record {record_id} deleted.[/green]")

            # Auto-sync this zone
            storage = Storage()
            _sync_zone(client, storage, zone)
            storage.close()

    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("record-update")
def record_update(
    zone: Annotated[str, typer.Argument(help="Domain/zone name")],
    record_id: Annotated[int, typer.Argument(help="Record ID to update")],
    content: Annotated[
        str | None,
        typer.Option("--content", "-c", help="New record content/value"),
    ] = None,
    ttl: Annotated[
        int | None, typer.Option("--ttl", help="New TTL in seconds")
    ] = None,
    priority: Annotated[
        int | None,
        typer.Option("--priority", help="New priority (for MX/SRV)"),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation")
    ] = False,
):
    """Update an existing DNS record (content, TTL, or priority)."""
    if content is None and ttl is None and priority is None:
        console.print(
            "[red]Error:[/red] specify at least one of --content / --ttl / --priority"
        )
        raise typer.Exit(1)

    try:
        with get_client() as client:
            all_records = client.list_records(zone)
            target = next(
                (r for r in all_records if r["id"] == record_id), None
            )
            if not target:
                console.print(
                    f"[red]Record ID {record_id} not found in zone {zone}.[/red]"
                )
                raise typer.Exit(1)

            name_display = "@" if target["name"] == "" else target["name"]
            current_content = target["content"]
            if len(current_content) > 60:
                current_content_display = current_content[:57] + "..."
            else:
                current_content_display = current_content

            console.print("\n[bold]Record to update:[/bold]")
            console.print(f"  Zone:    {zone}")
            console.print(f"  ID:      {record_id}")
            console.print(f"  Type:    {target['type']}")
            console.print(f"  Name:    {name_display}")
            console.print(f"  Content: {current_content_display}")
            console.print(f"  TTL:     {target.get('ttl')}")
            if target.get("priority") is not None:
                console.print(f"  Priority: {target['priority']}")

            console.print("\n[bold]Changes:[/bold]")
            if content is not None:
                console.print(f"  Content: {current_content_display} → {content}")
            if ttl is not None:
                console.print(f"  TTL:     {target.get('ttl')} → {ttl}")
            if priority is not None:
                console.print(
                    f"  Priority: {target.get('priority')} → {priority}"
                )

            if not yes:
                console.print()
                if not typer.confirm("Apply this update?"):
                    console.print("[yellow]Update cancelled.[/yellow]")
                    raise typer.Exit(0)

            client.update_record(
                zone,
                record_id,
                content=content,
                ttl=ttl,
                priority=priority,
            )
            console.print(f"\n[green]Record {record_id} updated.[/green]")

            storage = Storage()
            _sync_zone(client, storage, zone)
            storage.close()

    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


DNSIMPLE_DEFAULT_NS = [
    "ns1.dnsimple-edge.com",
    "ns2.dnsimple-edge.net",
    "ns3.dnsimple-edge.io",
    "ns4.dnsimple-edge.org",
]

# Hostnames recognised as DNSimple-managed (used by delegation-audit to decide
# whether a domain's registry NS is "on DNSimple" or somewhere else).
DNSIMPLE_NS_HOSTS = {
    "ns1.dnsimple-edge.com",
    "ns2.dnsimple-edge.net",
    "ns3.dnsimple-edge.io",
    "ns4.dnsimple-edge.org",
    "ns1.dnsimple.com",
    "ns2.dnsimple.com",
    "ns3.dnsimple.com",
    "ns4.dnsimple.com",
}


@app.command()
def nameservers(
    domain: Annotated[str, typer.Argument(help="Domain name")],
    set_dnsimple: Annotated[
        bool,
        typer.Option(
            "--set-dnsimple",
            help="Set delegation to DNSimple's default 4 edge nameservers",
        ),
    ] = False,
    ns: Annotated[
        list[str] | None,
        typer.Option(
            "--ns",
            help="Custom nameserver (repeat for multiple). Mutually exclusive with --set-dnsimple",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation")
    ] = False,
):
    """Show or change the registry nameserver delegation for a domain.

    With no flags, prints the current delegation. With --set-dnsimple, flips
    to the standard DNSimple edge nameservers. With --ns repeated, sets a
    custom list. Domain must be registered at DNSimple.
    """
    if set_dnsimple and ns:
        console.print(
            "[red]Error:[/red] --set-dnsimple and --ns are mutually exclusive"
        )
        raise typer.Exit(1)

    try:
        with get_client() as client:
            current = client.get_delegation(domain)
            console.print(f"\n[bold]{domain}[/bold] current delegation:")
            for n in current:
                console.print(f"  {n}")

            if not set_dnsimple and not ns:
                return  # show-only mode

            target_ns = DNSIMPLE_DEFAULT_NS if set_dnsimple else list(ns or [])

            if sorted(target_ns) == sorted(current):
                console.print("\n[green]Already set to the requested NS.[/green]")
                return

            console.print("\n[bold]New delegation:[/bold]")
            for n in target_ns:
                console.print(f"  {n}")

            if not yes:
                console.print()
                if not typer.confirm("Apply this delegation change?"):
                    console.print("[yellow]Cancelled.[/yellow]")
                    raise typer.Exit(0)

            updated = client.change_delegation(domain, target_ns)
            console.print("\n[green]Delegation updated.[/green] New NS:")
            for n in updated:
                console.print(f"  {n}")

    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)


@app.command("delegation-audit")
def delegation_audit():
    """Audit registry delegation for every domain in the local DB.

    Reports which domains delegate to DNSimple's nameservers and which still
    delegate elsewhere (e.g. just-transferred domains whose NS hasn't been
    flipped). Useful as the final check after a batch of inbound transfers.
    """
    with Storage() as storage:
        domains = [d.name for d in storage.load_domains(include_subdomains=False)]

    if not domains:
        console.print(
            "[yellow]No domains in local DB.[/yellow] Run `dnsimple sync` first."
        )
        raise typer.Exit(1)

    console.print(f"Auditing {len(domains)} domain(s)...\n")

    on_dnsimple: list[str] = []
    elsewhere: list[tuple[str, list[str]]] = []
    not_registered: list[tuple[str, str]] = []

    try:
        with get_client() as client:
            for d in domains:
                try:
                    ns_list = client.get_delegation(d)
                except DNSimpleAPIError as e:
                    not_registered.append((d, f"HTTP {e.status_code}"))
                    continue
                ns_set = set(ns_list)
                if ns_set and ns_set.issubset(DNSIMPLE_NS_HOSTS):
                    on_dnsimple.append(d)
                else:
                    elsewhere.append((d, sorted(ns_set)))
    except DNSimpleAPIError as e:
        console.print(f"[red]API Error:[/red] {e.message}")
        raise typer.Exit(1)

    table = Table(title="Delegation audit")
    table.add_column("Domain")
    table.add_column("Status")
    table.add_column("Current NS")

    for d in on_dnsimple:
        table.add_row(d, "[green]DNSimple[/green]", "—")
    for d, ns_list in elsewhere:
        table.add_row(
            d, "[yellow]Elsewhere[/yellow]", ", ".join(ns_list) or "(empty)"
        )
    for d, reason in not_registered:
        table.add_row(d, f"[dim]Skipped ({reason})[/dim]", "—")

    console.print(table)
    console.print(
        f"\n{len(on_dnsimple)} on DNSimple, {len(elsewhere)} elsewhere, "
        f"{len(not_registered)} skipped."
    )
    if elsewhere:
        console.print(
            "\n[dim]To flip:[/dim] dnsimple nameservers <domain> --set-dnsimple"
        )


@app.command()
def health(
    domain: Annotated[str, typer.Argument(help="Domain to check")],
):
    """Run DNS health checks on a domain using synced local data."""
    from dnsimple_cli.health_check import run_health_check

    with Storage() as storage:
        dom = storage.get_domain(domain)
        if not dom:
            console.print(
                f"[red]Domain '{domain}' not found.[/red] Run [bold]dnsimple sync[/bold] first."
            )
            raise typer.Exit(1)

        report = run_health_check(dom.name, dom.records)

    # Status styling
    status_style = {
        "pass": "[green]PASS[/green]",
        "warn": "[yellow]WARN[/yellow]",
        "fail": "[red]FAIL[/red]",
    }

    console.print(f"\n[bold]Health Check: {report.domain}[/bold]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", width=6, justify="center")
    table.add_column("Check", width=16)
    table.add_column("Details")

    for result in report.results:
        table.add_row(
            status_style[result.status],
            result.check,
            result.message,
        )

    console.print(table)

    # Summary
    console.print(
        f"\n  {report.pass_count} passed, "
        f"{report.warn_count} warnings, "
        f"{report.fail_count} failures"
    )

    if report.fail_count > 0 or report.warn_count > 0:
        console.print(
            f"\n  [dim]Full check: https://mxtoolbox.com/emailhealth/{domain}/[/dim]"
        )


if __name__ == "__main__":
    app()
