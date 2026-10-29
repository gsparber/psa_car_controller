"""First setup when the account information of the brand has no user id (#1279).

The customer id was built from it, Stellantis stopped sending it for some accounts:
    KeyError: 'id'
"""
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import requests
from oauth2_client.credentials_manager import OAuthError

from psa_car_controller.common.utils import RateLimitException
from psa_car_controller.psa.constants import BRAND
from psa_car_controller.psa.setup.app_decoder import InitialSetup, MissingCustomerIdError, describe_response

PEUGEOT = "com.psa.mym.mypeugeot"
MODULE = "psa_car_controller.psa.setup.app_decoder"
EMAIL = "user@example.com"
VIN = "VR3UHZKX00000001"
# keys of the answer reported in #1279
USER_INFO_WITHOUT_ID = {"language": "de", "country": "DE", "profile": {"email": EMAIL}, "dealers": [],
                        "vehicles": [{"vin": VIN, "short_label": "NEW 208"}], "settings_update": None,
                        "terms_service": None, "early_adopter": False, "service_state": None,
                        "cgu_validation": None}


def response(data):
    res = MagicMock()
    res.json.return_value = data
    res.text = str(data)
    return res


class TestMissingUserId(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.prefix = tmp_dir.name + os.sep

    @staticmethod
    def setup(user_info, existing_config=None):
        apk_parser = MagicMock(culture="de_DE", client_id="client-id", site_code="AP_DE_ESP", version="1.0")
        brand_token = response({"accessToken": "brand-token"})
        with patch(MODULE + ".get_content_from_apk", return_value=apk_parser), \
                patch(MODULE + ".requests.post", side_effect=[brand_token, response({"success": user_info})]), \
                patch(MODULE + ".path.isfile", return_value=existing_config is not None), \
                patch(MODULE + ".PSAClient") as psa_client:
            psa_client.load_config.return_value = existing_config
            psa_client.return_value.customer_id = None
            return InitialSetup(PEUGEOT, EMAIL, "password", "DE"), psa_client

    def connect(self, setup, car_associations):
        psacc = setup.psacc
        psacc.customer_id = setup.customer_id
        psacc.get_vehicles.return_value = [MagicMock(vin=VIN)]
        if isinstance(car_associations, Exception):
            psacc.manager.get.side_effect = car_associations
        else:
            psacc.manager.get.return_value = response(car_associations)
        psacc.save_config.side_effect = lambda name: self.assertTrue(psacc.customer_id)
        with patch(MODULE + ".PSACarController"):
            setup.connect("code", config_prefix=self.prefix)
        return psacc

    def test_user_id_builds_the_customer_id(self):
        setup, psa_client = self.setup(dict(USER_INFO_WITHOUT_ID, id="ACNT1234"))
        self.assertEqual("AP-ACNT1234", setup.customer_id)
        self.assertEqual("AP-ACNT1234", psa_client.call_args.args[4])

    def test_reuses_the_customer_id_of_the_existing_configuration(self):
        existing = MagicMock(realm=BRAND[PEUGEOT]["realm"], customer_id="AP-ACNT1234")
        setup, _ = self.setup(USER_INFO_WITHOUT_ID, existing)
        self.assertIs(existing, setup.psacc)
        self.assertEqual("AP-ACNT1234", setup.customer_id)
        # the car labels still come from the account information
        self.assertIs(USER_INFO_WITHOUT_ID, setup.user_info)

    def test_reads_the_customer_id_from_the_car_associations_after_the_login(self):
        setup, _ = self.setup(USER_INFO_WITHOUT_ID)
        self.assertIsNone(setup.customer_id)
        psacc = self.connect(setup, [{"customer": "AP-ACNT1234", "vehicle": VIN}])
        self.assertEqual("AP-ACNT1234", psacc.customer_id)
        self.assertEqual("AP-ACNT1234", psacc.account_info.customer_id)
        psacc.save_config.assert_called_once()
        url = psacc.manager.get.call_args.args[0]
        self.assertTrue(url.endswith("/cvs/v4/mauv/car-associations"))
        kwargs = psacc.manager.get.call_args.kwargs
        self.assertEqual({"client_id": "client-id", "locale": "de-DE"}, kwargs["params"])
        self.assertEqual(BRAND[PEUGEOT]["realm"], kwargs["headers"]["x-introspect-realm"])

    def test_adds_the_brand_code_to_a_customer_id_without_it(self):
        setup, _ = self.setup(USER_INFO_WITHOUT_ID)
        psacc = self.connect(setup, [{"vehicle": VIN}, {"customer": "ACNT1234"}])
        self.assertEqual("AP-ACNT1234", psacc.customer_id)

    def test_explains_the_failure_without_customer_id(self):
        for answer in ({"error": "unknown", "email": EMAIL}, [], [{"customer": None}],
                       requests.ConnectionError("down"),
                       OAuthError(HTTPStatus.BAD_REQUEST, "invalid_grant"), RateLimitException("refresh")):
            with self.subTest(answer=answer):
                setup, _ = self.setup(USER_INFO_WITHOUT_ID)
                with self.assertRaises(MissingCustomerIdError) as error:
                    self.connect(setup, answer)
                setup.psacc.save_config.assert_not_called()
                message = str(error.exception)
                self.assertIn("config.json", message)
                self.assertIn("1279", message)
                self.assertNotIn(EMAIL, message)

    def test_explains_unexpected_account_information(self):
        with self.assertRaises(ConnectionError) as error:
            self.setup(["not", "a", "dict"])
        self.assertIn("account information", str(error.exception))


class TestDescribeResponse(unittest.TestCase):
    def test_lists_the_keys_without_the_values(self):
        text = describe_response(USER_INFO_WITHOUT_ID)
        self.assertIn("profile{email}", text)
        self.assertIn("vehicles", text)
        self.assertNotIn(EMAIL, text)
        self.assertNotIn(VIN, text)

    def test_describes_lists_and_masks_other_values(self):
        self.assertEqual("list of 0", describe_response([]))
        self.assertIn("first item keys: customer", describe_response([{"customer": "AP-ACNT1234"}]))
        self.assertNotIn("abcdefghijkl", describe_response('{"access_token": "abcdefghijkl"}'))
        self.assertNotIn(EMAIL, describe_response(f"Unknown user {EMAIL}"))

    def test_is_shortened(self):
        self.assertLessEqual(len(describe_response({str(i): i for i in range(500)})), 303)


if __name__ == '__main__':
    unittest.main()
