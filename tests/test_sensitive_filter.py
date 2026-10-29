import io
import json
import logging
import unittest

from psa_car_controller.common.mylogger import file_handler, stream_handler
from psa_car_controller.common.sensitive_filter import SensitiveDataFilter, sensitive_filter
from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.model.car import Car, Cars

VIN = "VR3UHZKXZLT123456"
ACCESS_TOKEN = "f2a1c9d0-7b4e-4c1a-9e3d-2b8f6a5c4d3e"
CUSTOMER_ID = "AP-ACNT200000123456"


def get_logger(log_filter: SensitiveDataFilter):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(log_filter)
    test_logger = logging.getLogger("tests.sensitive_filter")
    test_logger.handlers = [handler]
    test_logger.propagate = False
    test_logger.setLevel(logging.DEBUG)
    return test_logger, stream


class TestSensitiveFilter(unittest.TestCase):

    def test_registered_values_are_masked(self):
        # GIVEN a filter knowing the current token, VIN and customer id
        log_filter = SensitiveDataFilter()
        log_filter.add_source(lambda: [ACCESS_TOKEN, VIN, CUSTOMER_ID, None, "abc"])
        test_logger, stream = get_logger(log_filter)

        # WHEN they are logged as plain values (e.g. MQTT topic)
        test_logger.info("publish to psa/RemoteServices/from/cid/%s/%s token %s", CUSTOMER_ID, VIN, ACCESS_TOKEN)

        # THEN only the last characters remain
        output = stream.getvalue()
        self.assertNotIn(ACCESS_TOKEN, output)
        self.assertNotIn(VIN, output)
        self.assertNotIn(CUSTOMER_ID, output)
        self.assertIn("***3456", output)
        self.assertIn("***4d3e", output)

    def test_sources_are_read_on_each_record(self):
        # GIVEN a token that is refreshed after the source was registered
        log_filter = SensitiveDataFilter()
        tokens = {"remote": "old-remote-token"}
        log_filter.add_source(lambda: [tokens["remote"]])
        test_logger, stream = get_logger(log_filter)

        # WHEN the new token is logged
        tokens["remote"] = "new-remote-token"
        test_logger.info("token: %s", "new-remote-token")

        # THEN it is masked without registering it again
        self.assertNotIn("new-remote-token", stream.getvalue())

    def test_token_keys_are_masked_without_registration(self):
        # GIVEN a filter without any known value
        test_logger, stream = get_logger(SensitiveDataFilter())
        message = json.dumps({"access_token": "abcdefgh12345678", "customer_id": "x",
                              "req_parameters": {"action": "activate"}})

        # WHEN json, dict, form data and headers with secrets are logged
        test_logger.debug("mqtt publish: %s", message)
        test_logger.debug("refresh: %s", {"refresh_token": "rt-0123456789", "expires_in": 3599})
        test_logger.debug("POST grant_type=refresh_token&refresh_token=rt-form-9876&client_secret=cs-1234")
        test_logger.debug("headers: {'Authorization': 'Bearer eyJhbGciOi.payload.sig'}")
        test_logger.debug("remote token: %s", {"refresh_token": None})

        # THEN the values are masked and the other fields are kept
        output = stream.getvalue()
        for secret in ("abcdefgh12345678", "rt-0123456789", "rt-form-9876", "cs-1234", "eyJhbGciOi"):
            self.assertNotIn(secret, output)
        self.assertIn('"access_token": "***"', output)
        self.assertIn('"action": "activate"', output)
        self.assertIn("'expires_in': 3599", output)
        self.assertIn("grant_type=refresh_token", output)
        self.assertIn("'refresh_token': None", output)

    def test_unknown_vin_is_masked(self):
        # GIVEN a VIN that isn't registered yet (e.g. during setup)
        test_logger, stream = get_logger(SensitiveDataFilter())

        # WHEN it is logged
        test_logger.info("Your vehicles: %s", [{"vin": "VXKUHZKXZL4567890"}])

        # THEN it is masked
        output = stream.getvalue()
        self.assertNotIn("VXKUHZKXZL4567890", output)
        self.assertIn("***7890", output)

    def test_exception_text_is_masked(self):
        # GIVEN a filter knowing the token
        log_filter = SensitiveDataFilter()
        log_filter.add_source(lambda: [ACCESS_TOKEN])
        test_logger, stream = get_logger(log_filter)

        # WHEN an exception containing it is logged
        try:
            raise ValueError("bad token " + ACCESS_TOKEN)
        except ValueError:
            test_logger.exception("error")

        # THEN the traceback is masked too
        output = stream.getvalue()
        self.assertIn("ValueError", output)
        self.assertNotIn(ACCESS_TOKEN, output)

    def test_app_handlers_use_filter(self):
        self.assertIn(sensitive_filter, file_handler.filters)
        self.assertIn(sensitive_filter, stream_handler.filters)

    def test_psa_client_registers_its_secrets(self):
        # GIVEN a configured client with a car
        myp = PSAClient.load_config("tests/data/config.json")
        myp.vehicles_list = Cars([Car(VIN, "vid", "Peugeot")])
        myp.remote_client.remoteCredentials.access_token = ACCESS_TOKEN
        myp.service_information.client_secret = "client-secret-0123"

        # WHEN its secrets are masked by the global filter
        text = sensitive_filter.mask(" ".join(
            [myp.manager.refresh_token, myp.service_information.client_secret, myp.customer_id,
             myp.account_info.get_mqtt_customer_id(), VIN, ACCESS_TOKEN]))

        # THEN none of them is left
        for secret in (myp.manager.refresh_token, myp.service_information.client_secret, myp.customer_id,
                       myp.account_info.get_mqtt_customer_id(), VIN, ACCESS_TOKEN):
            self.assertNotIn(secret, text)

    def test_psa_client_without_customer_id(self):
        # GIVEN a client during setup, before the customer id is known
        myp = PSAClient.load_config("tests/data/config.json")
        myp.customer_id = None

        # THEN its secrets can still be read
        self.assertIn(myp.manager.refresh_token, myp.sensitive_values())


if __name__ == '__main__':
    unittest.main()
