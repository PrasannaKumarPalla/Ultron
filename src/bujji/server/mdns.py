"""mDNS / Zeroconf service advertisement for the Android companion app.

Advertises ``_bujji._tcp.local`` on the local network so the Android app can
auto-discover the PC without manual IP entry.

Usage::

    advertiser = BujjiMDNS(port=8000)
    advertiser.start()
    # ... server running ...
    advertiser.stop()
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_SERVICE_TYPE = "_bujji._tcp.local."


def _local_ip() -> str:
    """Best-effort detection of the LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class BujjiMDNS:
    """Advertise the B.U.J.J.I API over mDNS so Android can find the PC."""

    def __init__(
        self,
        port: int = 8000,
        *,
        host_ip: Optional[str] = None,
        product_name: str = "B.U.J.J.I",
    ) -> None:
        self._port = port
        self._host_ip = host_ip or _local_ip()
        self._product_name = product_name
        self._zeroconf: Optional[object] = None
        self._service_info: Optional[object] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Start mDNS advertisement. Returns False if zeroconf is not installed."""
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.info("zeroconf not installed — mDNS discovery disabled. pip install zeroconf")
            return False

        hostname = socket.gethostname().replace(" ", "-").lower()
        # Dots inside the instance label (e.g. "B.U.J.J.I") are invalid and make
        # zeroconf raise BadTypeInNameException — strip them for the label.
        instance_label = self._product_name.replace(".", "").strip() or "bujji"
        service_name = f"{instance_label}.{_SERVICE_TYPE}"

        self._service_info = ServiceInfo(
            _SERVICE_TYPE,
            service_name,
            addresses=[socket.inet_aton(self._host_ip)],
            port=self._port,
            properties={
                b"product": self._product_name.encode(),
                b"version": b"1",
                b"host": hostname.encode(),
            },
            server=f"{hostname}.local.",
        )
        self._zeroconf = Zeroconf()
        self._zeroconf.register_service(self._service_info)
        logger.info(
            "mDNS: advertising %s at %s:%d",
            service_name,
            self._host_ip,
            self._port,
        )
        return True

    def stop(self) -> None:
        """Unregister the mDNS service."""
        if self._zeroconf is None:
            return
        try:
            if self._service_info is not None:
                self._zeroconf.unregister_service(self._service_info)
            self._zeroconf.close()
        except Exception as exc:
            logger.debug("mDNS stop error: %s", exc)
        finally:
            self._zeroconf = None
            self._service_info = None
        logger.info("mDNS: service unregistered")


__all__ = ["BujjiMDNS"]
