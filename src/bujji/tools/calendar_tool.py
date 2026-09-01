"""Google Calendar tool via OAuth2 REST API.

Called from tool executor/orchestrator. Registered as "calendar".
No existing file. Actions: get_events, create_event, delete_event.
OAuth2 credentials at ~/.bujji/google_credentials.json; token cached at
~/.bujji/google_token.json.
User instruction: do all remaining ones.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bujji.core.registry import ToolRegistry
from bujji.core.types import ToolResult
from bujji.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_CREDENTIALS_PATH = Path("~/.bujji/google_credentials.json").expanduser()
_TOKEN_PATH = Path("~/.bujji/google_token.json").expanduser()

_SETUP_MSG = (
    "Google Calendar is not configured. To set it up:\n"
    "1. Go to console.cloud.google.com → APIs & Services → Credentials\n"
    "2. Create an OAuth 2.0 Desktop App client ID\n"
    "3. Download credentials JSON and save to ~/.bujji/google_credentials.json\n"
    "4. Run bujji once with the calendar tool to complete OAuth flow."
)


def _build_service() -> Any:
    """Build and return an authenticated Google Calendar service."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ImportError(
            "Google API libraries not installed. Run: "
            "pip install google-api-python-client google-auth-oauthlib"
        ) from exc

    creds: Optional[Credentials] = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not _CREDENTIALS_PATH.exists():
                raise FileNotFoundError(_SETUP_MSG)
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), _SCOPES)
            creds = flow.run_local_server(port=0)

        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def _parse_date(date_str: str) -> tuple[str, str]:
    """Return (time_min, time_max) ISO strings for 'today', 'tomorrow', or ISO date."""
    now = datetime.now(tz=timezone.utc)
    if date_str in ("today", ""):
        day = now.date()
    elif date_str == "tomorrow":
        day = (now + timedelta(days=1)).date()
    else:
        day = datetime.fromisoformat(date_str).date()

    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


@ToolRegistry.register("calendar")
class CalendarTool(BaseTool):
    """Interact with Google Calendar — list, create, and delete events."""

    tool_id = "calendar"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar",
            description=(
                "Manage Google Calendar events. "
                "Actions: get_events, create_event, delete_event."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get_events", "create_event", "delete_event"],
                        "description": "Calendar action to perform",
                    },
                    "date": {
                        "type": "string",
                        "description": "'today', 'tomorrow', or ISO date (YYYY-MM-DD) for get_events",
                    },
                    "title": {"type": "string", "description": "Event title (create_event)"},
                    "start_time": {
                        "type": "string",
                        "description": "ISO 8601 datetime for event start (create_event)",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "ISO 8601 datetime for event end (create_event)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional event description (create_event)",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "Google Calendar event ID (delete_event)",
                    },
                },
                "required": ["action"],
            },
            category="productivity",
            timeout_seconds=20.0,
        )

    def execute(  # type: ignore[override]
        self,
        action: str = "",
        date: str = "today",
        title: str = "",
        start_time: str = "",
        end_time: str = "",
        description: str = "",
        event_id: str = "",
        **_: Any,
    ) -> ToolResult:
        try:
            service = _build_service()
        except ImportError as exc:
            return ToolResult(tool_name=self.tool_id, content=str(exc), success=False)
        except FileNotFoundError as exc:
            return ToolResult(tool_name=self.tool_id, content=str(exc), success=False)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"Auth error: {exc}", success=False)

        if action == "get_events":
            return self._get_events(service, date)
        elif action == "create_event":
            return self._create_event(service, title, start_time, end_time, description)
        elif action == "delete_event":
            return self._delete_event(service, event_id)
        else:
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Unknown action '{action}'. Use: get_events, create_event, delete_event",
                success=False,
            )

    def _get_events(self, service: Any, date: str) -> ToolResult:
        try:
            time_min, time_max = _parse_date(date)
            result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=20,
                )
                .execute()
            )
            events = result.get("items", [])
            out = [
                {
                    "id": e.get("id"),
                    "title": e.get("summary", "(no title)"),
                    "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
                    "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
                }
                for e in events
            ]
            return ToolResult(tool_name=self.tool_id, content=json.dumps(out), success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"get_events failed: {exc}", success=False)

    def _create_event(
        self, service: Any, title: str, start_time: str, end_time: str, description: str
    ) -> ToolResult:
        if not title or not start_time or not end_time:
            return ToolResult(
                tool_name=self.tool_id,
                content="title, start_time, and end_time are required for create_event",
                success=False,
            )
        try:
            body: dict = {
                "summary": title,
                "start": {"dateTime": start_time},
                "end": {"dateTime": end_time},
            }
            if description:
                body["description"] = description
            created = service.events().insert(calendarId="primary", body=body).execute()
            return ToolResult(
                tool_name=self.tool_id,
                content=json.dumps({"id": created["id"], "title": created.get("summary")}),
                success=True,
            )
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"create_event failed: {exc}", success=False)

    def _delete_event(self, service: Any, event_id: str) -> ToolResult:
        if not event_id:
            return ToolResult(
                tool_name=self.tool_id,
                content="event_id is required for delete_event",
                success=False,
            )
        try:
            service.events().delete(calendarId="primary", eventId=event_id).execute()
            return ToolResult(tool_name=self.tool_id, content=f"Deleted event {event_id}", success=True)
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"delete_event failed: {exc}", success=False)


__all__ = ["CalendarTool"]
