import unittest
from unittest.mock import MagicMock

from psa_car_controller.psacc.application.psa_client import PSAClient


def get_client(status=None) -> PSAClient:
    client = PSAClient.__new__(PSAClient)  # bypass __init__, it does I/O
    car = MagicMock(status=status)
    client.vehicles_list = MagicMock()
    client.vehicles_list.get_car_by_vin.return_value = car
    client.api = MagicMock()
    client.remote_client = MagicMock()
    client._record_enabled = False
    return client


class TestVehicleInfoCache(unittest.TestCase):

    def test_a_cached_read_returns_the_stored_status(self):
        status = MagicMock()
        client = get_client(status)
        self.assertIs(status, client.get_vehicle_info("VIN", cache=True))
        client.api.assert_not_called()

    def test_a_cached_read_with_an_empty_cache_does_not_call_psa(self):
        client = get_client()
        self.assertIsNone(client.get_vehicle_info("VIN", cache=True))
        client.api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
