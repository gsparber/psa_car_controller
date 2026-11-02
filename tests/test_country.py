import unittest

from psa_car_controller.psacc.application.ecomix import Ecomix
from psa_car_controller.psacc.repository.country import country_code


class TestCountry(unittest.TestCase):
    """The answers of reverse_geocode 1.6.6 for these positions, close to borders and coasts."""

    def test_country_of_positions(self):
        for latitude, longitude, expected in ((48.2, 16.37, "AT"), (47.56, 7.59, "CH"), (43.73, 7.42, "MC"),
                                              (51.44, 5.48, "NL"), (46.2, 6.15, "CH"), (-33.9, 18.4, "ZA"),
                                              (48.8566, 2.3522, "FR")):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertEqual(expected, country_code(latitude, longitude))

    def test_text_of_a_number(self):
        self.assertEqual("AT", country_code("48.2", "16.37"))

    def test_no_position(self):
        for latitude, longitude, error in ((float("nan"), 2.0, ValueError), (None, 2.0, TypeError),
                                           ("north", 2.0, ValueError)):
            with self.subTest(latitude=latitude), self.assertRaises(error):
                country_code(latitude, longitude)

    def test_ecomix_falls_back_to_the_default_country(self):
        self.assertEqual("FR", Ecomix.get_country(None, None, "FR"))
