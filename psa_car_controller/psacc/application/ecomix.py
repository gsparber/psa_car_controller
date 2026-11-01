import logging
from datetime import datetime, timedelta
from statistics import mean, StatisticsError
import xml.etree.ElementTree as ElT
import numbers
from typing import Union

import requests
import reverse_geocode
from pytz import UTC
from requests import RequestException

CO2_SIGNAL_REQ_INTERVAL = 600
CO2_SIGNAL_URL = "https://api.electricitymaps.com"
TIMEOUT_IN_S = 10
logger = logging.getLogger(__name__)


class Ecomix:
    _cache = {}
    _failed_at = {}
    co2_signal_key = None

    @staticmethod
    def get_data_france(start, end) -> Union[float, None]:
        start_str = start.strftime("%d/%m/%Y")
        end_str = end.strftime("%d/%m/%Y")
        try:
            res = requests.get(
                f"https://eco2mix.rte-france.com/curves/eco2mixWeb?type=co2&&dateDeb={start_str}"
                f"&dateFin={end_str}&mode=NORM",
                headers={
                    "Origin": "https://www.rte-france.com",
                    "Referer": "https://www.rte-france.com/eco2mix/les-emissions-de-co2-par-kwh-produit-en-france",
                },
                timeout=TIMEOUT_IN_S
            )
        except RequestException as e:
            logger.error("Can't get the CO2 values of France (%s)", type(e).__name__)
            return None
        try:
            etree = ElT.fromstring(res.text)
        except ElT.ParseError:
            logger.error("Bad answer for the CO2 values of France (HTTP %s)", res.status_code)
            return None
        period_start = (start.hour + int(start.minute / 30)) * 4
        period_end = (end.hour + int(end.minute / 30)) * 4

        valeurs = etree.iter("valeur")
        co2_per_kw = []
        try:
            valeur = next(valeurs)
            while int(valeur.attrib["periode"]) != period_start:
                valeur = next(valeurs)
            while int(valeur.attrib["periode"]) != period_end:
                co2_per_kw.append(int(valeur.text))
                valeur = next(valeurs)
        except (StopIteration, KeyError, TypeError, ValueError):
            logger.exception("Can't get CO2 value between %s %s", start, end)
            return None
        try:
            return mean(co2_per_kw)
        except StatisticsError:
            return None

    @staticmethod
    def get_data_from_co2_signal(latitude, longitude, country_code_default):
        if Ecomix.co2_signal_key is not None:
            try:
                now = datetime.now(UTC)
                country_code = Ecomix.get_country(latitude, longitude, country_code_default)
                if country_code is None:
                    logger.warning("Can't find the country of the car")
                    return False
                if country_code not in Ecomix._cache:
                    Ecomix._cache[country_code] = []
                elif len(Ecomix._cache[country_code]) > 0 and \
                        (now - Ecomix._cache[country_code][-1][0]).total_seconds() < CO2_SIGNAL_REQ_INTERVAL:
                    return False
                if (now - Ecomix._failed_at.get(country_code, datetime.min.replace(tzinfo=UTC))).total_seconds() \
                        < CO2_SIGNAL_REQ_INTERVAL:  # each try can hold up the refresh until the timeout
                    return False
                res = requests.get(CO2_SIGNAL_URL + "/v3/carbon-intensity/latest",
                                   headers={"auth-token": Ecomix.co2_signal_key},
                                   params={"zone": country_code},
                                   timeout=TIMEOUT_IN_S)
                if res.status_code != 200:
                    logger.warning("Bad answer from electricitymaps (HTTP %s)", res.status_code)
                    Ecomix._failed_at[country_code] = now
                    return False

                data = res.json()
                value = data["carbonIntensity"]
                if not isinstance(value, numbers.Number):
                    logger.error("carbonIntensity invalid value: '%s'", value)
                    return False
                Ecomix._cache[country_code].append([now, value])
                Ecomix._failed_at.pop(country_code, None)
                return True
            except RequestException as e:
                logger.warning("Can't reach electricitymaps (%s)", type(e).__name__)
                Ecomix._failed_at[country_code] = now
                return False
            except (NameError, KeyError, TypeError, ValueError):
                logger.debug("ecomix:", exc_info=True)
                return False
        else:
            return False

    @staticmethod
    def clean_cache():
        max_date = datetime.now(UTC) - timedelta(days=1)
        for values in Ecomix._cache.values():
            values[:] = [x for x in values if max_date < x[0]]

    @staticmethod
    def get_co2_from_signal_cache(start: datetime, end: datetime, country_code):
        Ecomix.clean_cache()
        co2_per_kw = []
        for row in Ecomix._cache.get(country_code, []):
            if start < row[0] < end:
                co2_per_kw.append(row[1])
        if len(co2_per_kw) == 0:
            return None
        return mean(co2_per_kw)

    @staticmethod
    def get_country(latitude, longitude, country_code_default):
        try:
            location = reverse_geocode.search([(latitude, longitude)])[0]
            country_code = location["country_code"]
            return country_code
        except (UnicodeDecodeError, IndexError, ValueError):
            logger.error("Can't find the country of the car")
            # return None
            country_code = country_code_default
            logger.warning("Using country of origin : %s (wrong co2 when traveling abroad)", country_code)
            return country_code

    @staticmethod
    def get_co2_per_kw(start: datetime, end: datetime, latitude, longitude, country_code_default):
        co2_per_kw = None
        country_code = Ecomix.get_country(latitude, longitude, country_code_default)
        if country_code is None:
            return None
        if Ecomix.co2_signal_key is not None:
            co2_per_kw = Ecomix.get_co2_from_signal_cache(start, end, country_code)
        elif country_code == 'FR':
            co2_per_kw = Ecomix.get_data_france(start, end)
        return co2_per_kw
