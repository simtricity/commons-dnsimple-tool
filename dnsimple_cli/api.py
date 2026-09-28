"""DNSimple API client."""

import time
from typing import Any

import httpx


class DNSimpleAPIError(Exception):
    """Exception raised for DNSimple API errors."""

    def __init__(self, message: str, status_code: int | None = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class DNSimpleClient:
    """Client for the DNSimple API."""

    BASE_URL = "https://api.dnsimple.com/v2"

    def __init__(self, account_id: str, access_token: str):
        """Initialize the client.

        Args:
            account_id: DNSimple account ID
            access_token: DNSimple API access token
        """
        self.account_id = account_id
        self.access_token = access_token
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | list[Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Make an API request with retry logic.

        Args:
            method: HTTP method
            path: API path
            params: Query parameters
            json_data: JSON body for POST/PUT requests
            max_retries: Maximum number of retries for rate limiting

        Returns:
            JSON response data

        Raises:
            DNSimpleAPIError: If the request fails
        """
        for attempt in range(max_retries):
            try:
                response = self._client.request(
                    method, path, params=params, json=json_data
                )

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    if attempt < max_retries - 1:
                        time.sleep(retry_after)
                        continue
                    raise DNSimpleAPIError(
                        f"Rate limited after {max_retries} retries",
                        status_code=429,
                    )

                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                return response.json()

            except httpx.HTTPStatusError as e:
                raise DNSimpleAPIError(
                    f"HTTP error: {e.response.status_code} - {e.response.text}",
                    status_code=e.response.status_code,
                ) from e
            except httpx.RequestError as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                raise DNSimpleAPIError(f"Request error: {e}") from e

        raise DNSimpleAPIError("Max retries exceeded")

    def whoami(self) -> dict[str, Any]:
        """Verify credentials and get account information.

        Returns:
            Account/user information

        Raises:
            DNSimpleAPIError: If authentication fails
        """
        data = self._request("GET", "/whoami")
        return data.get("data", {})

    def list_domains(self) -> list[dict[str, Any]]:
        """List all domains in the account with pagination.

        Returns:
            List of domain objects
        """
        domains = []
        page = 1
        per_page = 100

        while True:
            data = self._request(
                "GET",
                f"/{self.account_id}/domains",
                params={"page": page, "per_page": per_page},
            )

            page_domains = data.get("data", [])
            domains.extend(page_domains)

            # Check pagination
            pagination = data.get("pagination", {})
            total_pages = pagination.get("total_pages", 1)

            if page >= total_pages:
                break
            page += 1

        return domains

    def list_records(self, zone: str) -> list[dict[str, Any]]:
        """List all DNS records for a zone with pagination.

        Args:
            zone: Domain name (zone)

        Returns:
            List of DNS record objects
        """
        records = []
        page = 1
        per_page = 100

        while True:
            data = self._request(
                "GET",
                f"/{self.account_id}/zones/{zone}/records",
                params={"page": page, "per_page": per_page},
            )

            page_records = data.get("data", [])
            records.extend(page_records)

            # Check pagination
            pagination = data.get("pagination", {})
            total_pages = pagination.get("total_pages", 1)

            if page >= total_pages:
                break
            page += 1

        return records

    def get_zone(self, zone: str) -> dict[str, Any]:
        """Get zone information.

        Args:
            zone: Domain name (zone)

        Returns:
            Zone object with updated_at timestamp
        """
        data = self._request("GET", f"/{self.account_id}/zones/{zone}")
        return data.get("data", {})

    def list_zones(self) -> list[dict[str, Any]]:
        """List all zones with their updated_at timestamps.

        This is a lightweight call for incremental sync - returns zone metadata
        without records, allowing change detection via updated_at comparison.

        Returns:
            List of zone objects with updated_at timestamps
        """
        zones = []
        page = 1
        per_page = 100

        while True:
            data = self._request(
                "GET",
                f"/{self.account_id}/zones",
                params={"page": page, "per_page": per_page},
            )

            page_zones = data.get("data", [])
            zones.extend(page_zones)

            pagination = data.get("pagination", {})
            total_pages = pagination.get("total_pages", 1)

            if page >= total_pages:
                break
            page += 1

        return zones

    # =========================================================================
    # DNS Records (CRUD)
    # =========================================================================

    def create_record(
        self,
        zone: str,
        record_type: str,
        name: str,
        content: str,
        ttl: int = 3600,
        priority: int | None = None,
    ) -> dict[str, Any]:
        """Create a DNS record in a zone.

        Args:
            zone: Domain name (zone)
            record_type: DNS record type (MX, TXT, CNAME, A, etc.)
            name: Record name ("" for root, "www", etc.)
            content: Record value/content
            ttl: Time to live in seconds (default 3600)
            priority: Priority for MX/SRV records

        Returns:
            Created record object with id
        """
        json_data: dict[str, Any] = {
            "name": name,
            "type": record_type,
            "content": content,
            "ttl": ttl,
        }
        if priority is not None:
            json_data["priority"] = priority

        data = self._request(
            "POST",
            f"/{self.account_id}/zones/{zone}/records",
            json_data=json_data,
        )
        return data.get("data", {})

    def update_record(
        self,
        zone: str,
        record_id: int,
        content: str | None = None,
        ttl: int | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing DNS record (partial update).

        Args:
            zone: Domain name (zone)
            record_id: Record ID to update
            content: New content (optional)
            ttl: New TTL (optional)
            priority: New priority (optional)

        Returns:
            Updated record object
        """
        json_data: dict[str, Any] = {}
        if content is not None:
            json_data["content"] = content
        if ttl is not None:
            json_data["ttl"] = ttl
        if priority is not None:
            json_data["priority"] = priority

        data = self._request(
            "PATCH",
            f"/{self.account_id}/zones/{zone}/records/{record_id}",
            json_data=json_data,
        )
        return data.get("data", {})

    def delete_record(self, zone: str, record_id: int) -> dict[str, Any]:
        """Delete a DNS record.

        Args:
            zone: Domain name (zone)
            record_id: Record ID to delete

        Returns:
            Empty dict (204 No Content)
        """
        return self._request(
            "DELETE",
            f"/{self.account_id}/zones/{zone}/records/{record_id}",
        )

    # =========================================================================
    # Contacts
    # =========================================================================

    def list_contacts(self) -> list[dict[str, Any]]:
        """List all contacts (registrants) in the account."""
        contacts = []
        page = 1
        per_page = 100

        while True:
            data = self._request(
                "GET",
                f"/{self.account_id}/contacts",
                params={"page": page, "per_page": per_page},
            )

            contacts.extend(data.get("data", []))

            pagination = data.get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1

        return contacts

    # =========================================================================
    # TLD & Pricing
    # =========================================================================

    def get_tld(self, tld: str) -> dict[str, Any]:
        """Get TLD details including transfer_enabled flag.

        Note: This endpoint is not account-scoped.
        """
        data = self._request("GET", f"/tlds/{tld}")
        return data.get("data", {})

    def get_domain_prices(self, domain: str, action: str = "transfer") -> dict[str, Any]:
        """Get domain pricing for a specific action (transfer, register, renew)."""
        data = self._request(
            "GET",
            f"/{self.account_id}/registrar/domains/{domain}/prices",
            params={"action": action},
        )
        return data.get("data", {})

    # =========================================================================
    # Domain Transfers
    # =========================================================================

    def transfer_domain(
        self,
        domain: str,
        registrant_id: int,
        auth_code: str,
        auto_renew: bool = True,
        whois_privacy: bool = False,
    ) -> dict[str, Any]:
        """Initiate a domain transfer into DNSimple.

        Args:
            domain: Domain name to transfer
            registrant_id: Contact ID for the registrant
            auth_code: Auth/EPP code from the current registrar
            auto_renew: Enable auto-renewal after transfer
            whois_privacy: Enable WHOIS privacy after transfer

        Returns:
            Transfer object with id, state, etc.
        """
        data = self._request(
            "POST",
            f"/{self.account_id}/registrar/domains/{domain}/transfers",
            json_data={
                "registrant_id": registrant_id,
                "auth_code": auth_code,
                "auto_renew": auto_renew,
                "whois_privacy": whois_privacy,
            },
        )
        return data.get("data", {})

    def get_transfer(self, domain: str, transfer_id: int) -> dict[str, Any]:
        """Get the status of a domain transfer.

        Args:
            domain: Domain name
            transfer_id: Transfer ID from initiation

        Returns:
            Transfer object with current state
        """
        data = self._request(
            "GET",
            f"/{self.account_id}/registrar/domains/{domain}/transfers/{transfer_id}",
        )
        return data.get("data", {})

    def cancel_transfer(self, domain: str, transfer_id: int) -> dict[str, Any]:
        """Cancel a pending domain transfer.

        Args:
            domain: Domain name
            transfer_id: Transfer ID to cancel

        Returns:
            Transfer object with updated state
        """
        data = self._request(
            "DELETE",
            f"/{self.account_id}/registrar/domains/{domain}/transfers/{transfer_id}",
        )
        return data.get("data", {})

    def get_delegation(self, domain: str) -> list[str]:
        """Get the current nameserver delegation for a domain at the registry.

        Args:
            domain: Domain name

        Returns:
            List of nameserver hostnames currently delegated at the registry.
        """
        data = self._request(
            "GET",
            f"/{self.account_id}/registrar/domains/{domain}/delegation",
        )
        return data.get("data") or []

    def change_delegation(self, domain: str, nameservers: list[str]) -> list[str]:
        """Update the nameserver delegation for a domain at the registry.

        Args:
            domain: Domain name (must be registered at DNSimple)
            nameservers: List of nameserver hostnames (typically 2-4)

        Returns:
            The new delegation as confirmed by the API.
        """
        data = self._request(
            "PUT",
            f"/{self.account_id}/registrar/domains/{domain}/delegation",
            json_data=nameservers,
        )
        return data.get("data") or []

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
