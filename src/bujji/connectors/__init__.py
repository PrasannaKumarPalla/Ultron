"""Data source connectors for Deep Research."""

from bujji.connectors._stubs import (
    Attachment,
    BaseConnector,
    Document,
    SyncStatus,
)
from bujji.connectors.store import KnowledgeStore

__all__ = ["Attachment", "BaseConnector", "Document", "KnowledgeStore", "SyncStatus"]

# Auto-register built-in connectors
import bujji.connectors.obsidian  # noqa: F401

try:
    import bujji.connectors.gmail  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.gmail_imap  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.gdrive  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import bujji.connectors.notion  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.granola  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.gcontacts  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.imessage  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.apple_notes  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.apple_music  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.apple_contacts  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.slack_connector  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.outlook  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.gcalendar  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.dropbox  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import bujji.connectors.whatsapp  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.oura  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.apple_health  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.strava  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.spotify  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.google_tasks  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.weather  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.github_notifications  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.hackernews  # noqa: F401
except ImportError:
    pass

try:
    import bujji.connectors.news_rss  # noqa: F401
except ImportError:
    pass
