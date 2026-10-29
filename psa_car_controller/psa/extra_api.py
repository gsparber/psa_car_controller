"""Stellantis endpoints the generated connected car client lacks: maintenance, vehicle rights and trip pages.

Their answers are returned as plain JSON (dict), the format is only known from the brand apps and the
Home Assistant integration (andreadegiovine/homeassistant-stellantis-vehicles), not from a specification.
"""
from urllib.parse import parse_qsl, urlsplit

from psa_car_controller.psa.connected_car_api.api_client import ApiClient

API_ROOT = "https://api.groupe-psa.com"
CONNECTED_CAR_PATH = "/connectedcar/v4"
RIGHTS_PATH = "/applications/cvs/v4/rights/vehicle/{vin}"
TRIPS_PATH = CONNECTED_CAR_PATH + "/user/vehicles/{id}/trips"
AUTH_SETTINGS = ['Vehicle_auth', 'client_id', 'realm']
HAL_JSON = "application/hal+json"
# trips shorter than 100 m are noise (the car moved in a car park)
MIN_TRIP_DISTANCE = "0.1-"


def page_token(href):
    """pageToken query value of a paging link, None without one."""
    if not href:
        return None
    return dict(parse_qsl(urlsplit(href).query)).get("pageToken")


class ExtraApi:
    def __init__(self, api_client: ApiClient):
        # the client must be rooted at API_ROOT: the endpoints aren't all below /connectedcar/v4
        self.api_client = api_client

    def get_json(self, url, query=None, accept=HAL_JSON) -> dict:
        """GET a path below API_ROOT, or a full link the API returned, with the account's credentials."""
        parts = urlsplit(url)
        if parts.scheme and f"{parts.scheme}://{parts.netloc}" != API_ROOT:
            # never send the access token to another host a link points to
            raise ValueError(f"unexpected API host in {url}")
        query_params = parse_qsl(parts.query) + list(query or [])
        result = self.api_client.call_api(parts.path, 'GET', query_params=query_params,
                                          header_params={'Accept': accept}, response_type='object',
                                          auth_settings=AUTH_SETTINGS, _return_http_data_only=True)
        return result if isinstance(result, dict) else {}

    def get_maintenance(self, href) -> dict:
        """Maintenance link of /user/vehicles: {"mileageBeforeMaintenance", "daysBeforeMaintenance", ...}"""
        return self.get_json(href)

    def get_rights(self, vin) -> dict:
        """Remote services the car's subscription covers: {"services": {<unit>: [{"code", "name",
        "mqtt_services"}]}}"""
        # the brand app asks for plain json on this endpoint
        return self.get_json(RIGHTS_PATH.format(vin=vin), accept="application/json")

    def get_trips_page(self, vehicle_id, since=None, token=None) -> dict:
        """One page of the trips the car recorded: {"_embedded": {"trips": [...]}, "_links": {"next": ...}}"""
        query = [("distance", MIN_TRIP_DISTANCE)]
        if since is not None:
            # an open interval: every trip from this date on
            query.append(("timestamps", since.isoformat(timespec="seconds") + "/"))
        if token is not None:
            query.append(("pageToken", token))
        return self.get_json(TRIPS_PATH.format(id=vehicle_id), query)

    def get_trips(self, vehicle_id, since=None, max_pages=5):
        """Trips of every page up to max_pages, and whether more pages were left."""
        trips = []
        token = None
        seen = set()
        for _ in range(max_pages):
            page = self.get_trips_page(vehicle_id, since, token)
            trips.extend(_page_trips(page))
            token = page_token(((page.get("_links") or {}).get("next") or {}).get("href"))
            if token is None or token in seen:
                return trips, False
            seen.add(token)
        return trips, True


def _page_trips(page: dict) -> list:
    trips = (page.get("_embedded") or {}).get("trips")
    return [trip for trip in trips if isinstance(trip, dict)] if isinstance(trips, list) else []
