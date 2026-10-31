"""Download the vehicle picture the PSA API links to, once, and keep it next to the config files."""
import glob
import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

PREFIX = "vehicle_picture_"
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_SIZE = 5 * 1024 * 1024
# After a failed download, wait this long before asking the API again
RETRY_AFTER_S = 24 * 3600


class VehiclePicture:
    def __init__(self, psacc, directory="."):
        self.psacc = psacc
        self.directory = directory
        self._failed_at = {}
        self._lock = threading.Lock()

    def get_cached(self, vin) -> Optional[str]:
        files = glob.glob(os.path.join(self.directory, f"{PREFIX}{glob.escape(vin)}.*"))
        return files[0] if files else None

    def _failed_recently(self, vin) -> bool:
        return time.time() - self._failed_at.get(vin, 0) < RETRY_AFTER_S

    def fetch_in_background(self, vin):
        """Start a download unless one ran recently; the picture shows on a later page refresh.
        The way for whoever serves a request: it asks PSA and a foreign server, and a request must not wait for them."""
        if self.get_cached(vin) or self._failed_recently(vin):
            return
        if self._lock.locked():
            return
        threading.Thread(target=self.fetch, args=(vin,), daemon=True).start()

    def fetch(self, vin) -> Optional[str]:
        with self._lock:
            if self.get_cached(vin):
                return self.get_cached(vin)
            # also here: a second thread can have waited for the lock while the first one failed
            if self._failed_recently(vin):
                return None
            try:
                for url in self._picture_urls(vin):
                    path = self._download(vin, url)
                    if path:
                        logger.info("vehicle picture of %s saved to %s", vin, path)
                        return path
                logger.info("no vehicle picture available for %s", vin)
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("can't get vehicle picture of %s: %s", vin, e)
                logger.debug("vehicle picture error", exc_info=True)
            self._failed_at[vin] = time.time()
            return None

    def _picture_urls(self, vin):
        res = self.psacc.api().get_vehicles_by_device()
        for vehicle in res.embedded.vehicles:
            if vehicle.vin == vin:
                # The API declares pictures as a list of Url, which has no fields, so the client keeps the raw strings
                return [url for url in (vehicle.pictures or []) if isinstance(url, str)]
        return []

    def _download(self, vin, url) -> Optional[str]:
        res = requests.get(url, timeout=15, stream=True)
        content_type = res.headers.get("Content-Type", "").split(";")[0].strip()
        if res.status_code != 200 or content_type not in EXTENSIONS:
            logger.debug("picture %s: HTTP %s %s", url, res.status_code, content_type)
            return None
        content = res.raw.read(MAX_SIZE + 1, decode_content=True)
        if len(content) > MAX_SIZE:
            return None
        path = os.path.join(self.directory, f"{PREFIX}{vin}.{EXTENSIONS[content_type]}")
        with open(path, "wb") as f:
            f.write(content)
        return path


_INSTANCE: Optional[VehiclePicture] = None


def get_vehicle_picture(psacc) -> VehiclePicture:
    global _INSTANCE  # pylint: disable=global-statement
    if _INSTANCE is None or _INSTANCE.psacc is not psacc:
        _INSTANCE = VehiclePicture(psacc)
    return _INSTANCE
