"""Read-only GHL API v3 client. Never sends customer messages or writes to GHL."""

from urllib.parse import quote

import httpx


GHL_API_URL = "https://services.leadconnectorhq.com"


class GHLAPIError(Exception):
    """An upstream failure with no token, URL parameters or customer content."""

    def __init__(self, code: str, status_code: int = 502):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class GHLClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None):
        if not token.strip():
            raise ValueError("Missing GHL private integration token")
        self.http = httpx.Client(
            base_url=GHL_API_URL,
            headers={"Authorization": f"Bearer {token}", "Version": "v3", "Accept": "application/json"},
            timeout=httpx.Timeout(10.0),
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.http.close()

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = self.http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise GHLAPIError("ghl_timeout", 504) from exc
        except httpx.HTTPError as exc:
            raise GHLAPIError("ghl_connection_error") from exc

        if response.status_code == 429:
            # Do not retry immediately: GHL's quota window may need 10 seconds or longer.
            raise GHLAPIError("ghl_rate_limited", 429)
        if response.status_code in (401, 403):
            raise GHLAPIError("ghl_auth_or_scope_error", 502)
        if response.status_code >= 400:
            raise GHLAPIError("ghl_http_error", 502)

        try:
            data = response.json()
        except ValueError as exc:
            raise GHLAPIError("ghl_invalid_json") from exc
        if not isinstance(data, dict):
            raise GHLAPIError("ghl_invalid_response")
        return data

    def search_recent_conversations(self, location_id: str, *, limit: int = 5) -> list[dict]:
        data = self._get(
            "/conversations/search",
            {
                "locationId": location_id,
                "limit": limit,
                "sortBy": "last_message_date",
                "sort": "desc",
                "status": "all",
            },
        )
        conversations = data.get("conversations")
        if not isinstance(conversations, list) or not all(isinstance(item, dict) for item in conversations):
            raise GHLAPIError("ghl_invalid_conversations")
        return conversations[:limit]

    def get_messages(self, conversation_id: str, *, max_pages: int = 3) -> tuple[list[dict], bool]:
        """Return messages and whether there may be older, uncollected pages."""
        if not conversation_id or len(conversation_id) > 128:
            raise GHLAPIError("ghl_invalid_conversation_id")
        path = f"/conversations/{quote(conversation_id, safe='')}/messages"
        messages: list[dict] = []
        cursor: str | None = None
        used_cursors: set[str] = set()

        for _ in range(max_pages):
            params: dict[str, str | int] = {"limit": 20}
            if cursor:
                params["lastMessageId"] = cursor
            data = self._get(path, params)
            page = data.get("messages")
            if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
                raise GHLAPIError("ghl_invalid_messages")
            items = page["messages"]
            if not all(isinstance(item, dict) for item in items):
                raise GHLAPIError("ghl_invalid_messages")
            messages.extend(items)
            if not page.get("nextPage"):
                return messages, False
            next_cursor = page.get("lastMessageId")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in used_cursors:
                raise GHLAPIError("ghl_invalid_pagination")
            used_cursors.add(next_cursor)
            cursor = next_cursor

        return messages, True
