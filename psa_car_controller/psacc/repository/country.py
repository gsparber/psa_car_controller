"""Country of a position: the one of the nearest town of GeoNames, as reverse_geocode found it.

reverse_geocode needed scipy for this, about 80 MB, for one search now and then. geonames_towns.npz has the towns of
its data (GeoNames cities1000), see geonames_towns_SOURCE.txt; the search gives the same towns.
"""
from functools import lru_cache
from pathlib import Path

import numpy as np

TOWNS_FILE = Path(__file__).parent.parent / "resources" / "geonames_towns.npz"
# the coordinates are stored as integers of this many degrees
SCALE = 1e5


@lru_cache(maxsize=1)
def _towns():
    with np.load(TOWNS_FILE) as data:
        return data["latitude"] / SCALE, data["longitude"] / SCALE, data["country"], data["codes"]


def country_code(latitude: float, longitude: float) -> str:
    """Raises ValueError for a position that isn't a number."""
    latitude, longitude = float(latitude), float(longitude)
    if np.isnan(latitude) or np.isnan(longitude):
        raise ValueError(f"no position: {latitude}, {longitude}")
    latitudes, longitudes, countries, codes = _towns()
    # in degrees, as before: the same nearest town, even if not the nearest one on earth near the poles
    nearest = np.argmin((latitudes - latitude) ** 2 + (longitudes - longitude) ** 2)
    # pylint can't see what np.load returns
    return str(codes[countries[nearest]])  # pylint: disable=unsubscriptable-object
