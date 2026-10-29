#!/usr/bin/env python3
import json
import logging
import re
import traceback
from os import path
from typing import Optional

import requests

from psa_car_controller.common.sensitive_filter import MASK, sensitive_filter
from psa_car_controller.psa.constants import BRAND, MQTT_BRANDCODE
from psa_car_controller.psa.setup.apk_parser import ApkParser
from psa_car_controller.psa.setup.github import urlretrieve_from_github
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.psacc.application.psa_client import PSAClient, DEFAULT_CONFIG_FILENAME
from psa_car_controller.psacc.application.charge_control import ChargeControl, ChargeControls

logger = logging.getLogger(__name__)

GITHUB_USER = "flobz"
GITHUB_REPO = "psa_apk"
TIMEOUT_IN_S = 30
CAR_ASSOCIATIONS_URL = "https://api.groupe-psa.com/applications/cvs/v4/mauv/car-associations"
MAX_DESCRIPTION_LENGTH = 300
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def describe_response(data) -> str:
    """Shape of a server answer without its values: they hold the email, ids and VINs of the user."""
    if isinstance(data, dict):
        keys = []
        for key, value in data.items():
            if isinstance(value, dict):
                keys.append(f"{key}{{{', '.join(str(k) for k in value)}}}")
            else:
                keys.append(str(key))
        text = "keys: " + ", ".join(keys)
    elif isinstance(data, list):
        text = f"list of {len(data)}"
        if data:
            text += ", first item " + describe_response(data[0])
    else:
        text = f"{type(data).__name__}: {_EMAIL_RE.sub(MASK, sensitive_filter.mask(str(data)))}"
    if len(text) > MAX_DESCRIPTION_LENGTH:
        text = text[:MAX_DESCRIPTION_LENGTH] + "..."
    return text


class MissingCustomerIdError(ValueError):
    def __init__(self, car_associations):
        details = "" if car_associations is None else f" (answer: {describe_response(car_associations)})"
        super().__init__(
            "Stellantis did not send the user id of your account and the customer id could not be read from "
            f"the Stellantis API either{details}. If this account was configured before, restore the previous "
            f"{DEFAULT_CONFIG_FILENAME} (it contains the customer_id) and log in again. "
            "See https://github.com/flobz/psa_car_controller/issues/1279")


def get_content_from_apk(filename: str, country_code: str) -> ApkParser:
    urlretrieve_from_github(GITHUB_USER, GITHUB_REPO, "", filename)
    apk_parser = ApkParser(filename, country_code)
    apk_parser.retrieve_content_from_apk()
    return apk_parser


class InitialSetup:
    def __init__(self, package_name, client_email, client_password, country_code):
        self.package_name = package_name
        filename = package_name.split(".")[-1] + ".apk"
        apk_parser = get_content_from_apk(filename, country_code)
        self.culture = apk_parser.culture
        self.site_code = apk_parser.site_code
        self.client_id = apk_parser.client_id
        self.client_secret = apk_parser.client_secret
        self.version = apk_parser.version
        self.country_code = country_code
        self.user_info = None
        self.customer_id = None
        try:
            self.token = self.__fetch_brand_token(apk_parser, client_email, client_password)
            self.user_info = self.__fetch_user_info()
        except ConnectionError:
            # The brand login only provides the customer id and the car labels. When config.json
            # already has them, the OAuth login alone is enough to renew the connection.
            self.psacc = self.__load_existing_config()
            if self.psacc is None:
                raise
            logger.warning("The brand login service failed, renewing only the OAuth tokens of %s",
                           DEFAULT_CONFIG_FILENAME)
            self.customer_id = self.psacc.customer_id
            return
        user_id = self.user_info.get("id")
        if not isinstance(user_id, str) or not user_id:
            # Stellantis stopped sending it for some accounts (#1279)
            logger.warning("The account information of the brand has no user id (%s)",
                           describe_response(self.user_info))
            existing = self.__load_existing_config()
            if existing is not None and existing.customer_id:
                logger.warning("Reusing the customer id of %s", DEFAULT_CONFIG_FILENAME)
                self.psacc = existing
                self.customer_id = existing.customer_id
                return
            logger.warning("The customer id will be requested from the Stellantis API after the login")
        else:
            self.customer_id = BRAND[self.package_name]["brand_code"] + "-" + user_id

        self.psacc = PSAClient(None, self.client_id, self.client_secret,
                               None, self.customer_id, BRAND[self.package_name]["realm"],
                               self.country_code, BRAND[self.package_name]["brand_code"])

    @staticmethod
    def __fetch_brand_token(apk_parser: ApkParser, client_email, client_password) -> str:
        try:
            res = requests.post(apk_parser.host_brandid_prod + "/GetAccessToken",
                                headers={
                                    "Connection": "Keep-Alive",
                                    "Content-Type": "application/json",
                                    "User-Agent": "okhttp/2.3.0"
                                },
                                params={"jsonRequest": json.dumps(
                                    {"siteCode": apk_parser.site_code, "culture": "fr-FR", "action": "authenticate",
                                     "fields": {"USR_EMAIL": {"value": client_email},
                                                "USR_PASSWORD": {"value": client_password}}
                                     }
                                )},
                                timeout=TIMEOUT_IN_S
                                )
            data = res.json()
        except Exception as ex:
            msg = traceback.format_exc() + f"\nHOST_BRANDID : {apk_parser.host_brandid_prod} " \
                f"sitecode: {apk_parser.site_code}"
            try:
                msg += res.text
            except BaseException:
                pass
            logger.error(msg)
            # the details are in the log, the user only needs to know what to do
            raise ConnectionError("The login service of the brand did not answer correctly. This is usually a "
                                  "temporary problem on the Stellantis side, please try again later.") from ex
        token = data.get("accessToken") or data.get("token")
        if not token:
            logger.error("No access token in response: %s", res.text)
            raise ConnectionError(f"The login was refused: {data.get('returnCode', res.text)}")
        return token

    def __load_existing_config(self) -> Optional[PSAClient]:
        if not path.isfile(DEFAULT_CONFIG_FILENAME):
            return None
        try:
            psacc = PSAClient.load_config(DEFAULT_CONFIG_FILENAME)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Can't reuse %s", DEFAULT_CONFIG_FILENAME)
            return None
        if psacc.realm != BRAND[self.package_name]["realm"]:
            # configured for another brand, its customer id doesn't apply
            return None
        return psacc

    def __fetch_user_info(self):
        try:
            res2 = requests.post(
                f"https://mw-{BRAND[self.package_name]['brand_code'].lower()}-m2c.mym.awsmpsa.com/api/v1/user",
                params={
                    "culture": self.culture,
                    "width": 1080,
                    "version": self.version
                },
                data=json.dumps({"site_code": self.site_code, "ticket": self.token}),
                headers={
                    "Connection": "Keep-Alive",
                    "Content-Type": "application/json;charset=UTF-8",
                    "Source-Agent": "App-Android",
                    "Token": self.token,
                    "User-Agent": "okhttp/4.8.0",
                    "Version": self.version
                },
                cert=("certs/public.pem", "certs/private.pem"),
                timeout=TIMEOUT_IN_S
            )

            res_dict = res2.json()["success"]
            if not isinstance(res_dict, dict):
                raise TypeError(f"unexpected account information: {describe_response(res_dict)}")
            logger.debug("Account information of the brand: %s", describe_response(res_dict))
        except Exception as ex:
            msg = traceback.format_exc()
            try:
                msg += res2.text
            except BaseException:
                pass
            logger.error(msg)
            raise ConnectionError("Can't get the account information from the brand, this is usually a "
                                  "temporary problem on the Stellantis side, please try again later.") from ex
        return res_dict

    @staticmethod
    def __read_charge_settings(file_name) -> dict:
        try:
            with open(file_name, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Can't read %s, charge control settings will be reset", file_name, exc_info=True)
            return {}
        if not isinstance(settings, dict):
            logger.warning("Unexpected content in %s, charge control settings will be reset", file_name)
            return {}
        return settings

    def __new_charge_control(self, vin, settings) -> ChargeControl:
        if settings is not None:
            if isinstance(settings, dict) and isinstance(settings.get("percentage_threshold", 100), int):
                try:
                    return ChargeControl(self.psacc, vin, settings.get("percentage_threshold", 100),
                                         settings.get("stop_hour", [0, 0]))
                except (ValueError, TypeError, IndexError):
                    pass
            logger.warning("Invalid charge control settings for %s will be reset: %s", vin, settings)
        return ChargeControl(self.psacc, vin, 100, [0, 0])

    def __fetch_customer_id(self) -> str:
        """Fallback when the account information has no user id: needs the OAuth token.

        Same request as the Home Assistant integration, the answer is expected to be a list of
        car associations with a "customer" field.
        """
        brand_code = BRAND[self.package_name]["brand_code"]
        try:
            res = self.psacc.manager.get(CAR_ASSOCIATIONS_URL,
                                         params={"client_id": self.client_id,
                                                 "locale": (self.culture or "").replace("_", "-")},
                                         headers={"Accept": "application/hal+json",
                                                  "User-Agent": "okhttp/4.8.0",
                                                  "x-introspect-realm": BRAND[self.package_name]["realm"],
                                                  "x-transaction-id": "1234"},
                                         timeout=TIMEOUT_IN_S)
            res.raise_for_status()
            data = res.json()
        except (requests.RequestException, ValueError) as ex:
            logger.error("Can't get the car associations of the account: %s", ex)
            raise MissingCustomerIdError(None) from ex
        customer = None
        if isinstance(data, list):
            customer = next((association["customer"] for association in data
                             if isinstance(association, dict) and isinstance(association.get("customer"), str)
                             and association["customer"]), None)
        if customer is None:
            logger.error("No customer id in the car associations of the account: %s", describe_response(data))
            raise MissingCustomerIdError(data)
        if customer[:2] not in MQTT_BRANDCODE or customer[2:3] != "-":
            customer = brand_code + "-" + customer
        logger.info("Customer id read from the car associations of the account")
        return customer

    def connect(self, code, config_prefix=""):
        self.psacc.connect(code)
        if not self.psacc.customer_id:
            self.customer_id = self.__fetch_customer_id()
            self.psacc.customer_id = self.customer_id
            self.psacc.account_info.customer_id = self.customer_id
        self.psacc.save_config(name=config_prefix + "config.json")
        res = self.psacc.get_vehicles()

        if len(res) == 0:
            raise ValueError(
                "No vehicle in your account is compatible with this API, you vehicle is probably too old...")

        for vehicle in self.user_info["vehicles"] if self.user_info else []:
            if vin := vehicle.get("vin"):
                car = self.psacc.vehicles_list.get_car_by_vin(vin)
                if car is not None and "short_label" in vehicle and car.label == "unknown":
                    car.label = vehicle["short_label"].split(" ")[-1]  # remove new, nouvelle, neu word....
        self.psacc.vehicles_list.save_cars()

        logger.info("\nYour vehicles: %s", res)

        # Charge control: keep the settings of cars already configured, new cars start with charge control off
        charge_config_name = config_prefix + "charge_config.json"
        previous_settings = self.__read_charge_settings(charge_config_name)
        charge_controls = ChargeControls(charge_config_name)
        for vehicle in res:
            charge_controls[vehicle.vin] = self.__new_charge_control(vehicle.vin, previous_settings.get(vehicle.vin))
        charge_controls.save_config()
        app = PSACarController()
        app.load_app()
        app.start_remote_control()
