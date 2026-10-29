import copy
import json
import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from psa_car_controller.psa.oauth import OauthAPIClient
from psa_car_controller.psa.status_schema import enable_raw_status_log
from psa_car_controller.psacc.model.car_status import CarStatus, status_updated_at

# shape of real status responses (2025), which carry the deprecated `energy` list and the new `energies` one
ELECTRIC_ENERGIES = [{
    "createdAt": "2025-06-16T14:10:50Z", "type": "Electric", "subType": "ElectricEnergy", "level": 63.0,
    "autonomy": 192,
    "extension": {"electric": {
        "battery": {"health": {"resistance": 92}, "load": {"createdAt": "2025-06-16T14:10:50Z",
                                                           "capacity": 32576, "residual": 13280}},
        "charging": {"plugged": True, "status": "InProgress", "chargingRate": 11, "chargingMode": "Slow",
                     "nextDelayedTime": "PT12H", "remainingTime": "PT2H10M", "schedule": []}}}}]
ELECTRIC_ENERGY = [{
    "createdAt": "2025-06-16T14:10:50Z", "updatedAt": "2025-06-16T14:10:50Z", "type": "Electric", "level": 63.0,
    "autonomy": 192, "battery": {"health": {"resistance": 92}},
    "charging": {"plugged": True, "status": "InProgress", "chargingRate": 11, "chargingMode": "Slow",
                 "nextDelayedTime": "PT12H", "remainingTime": "PT2H10M"}}]
HYBRID_ENERGIES = [
    {"createdAt": "2025-06-08T11:04:17Z", "type": "Fuel", "subType": "FossilEnergy", "level": 20.0, "autonomy": 90,
     "extension": {"fuel": {"consumptions": {"total": 673.06, "instant": 12.9}}}},
    {"createdAt": "2025-06-08T11:04:17Z", "type": "Electric", "subType": "ElectricEnergy",
     "extension": {"electric": {"battery": {"load": {"capacity": 888, "residual": 470}}}}}]
VEHICLE_ID = "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56"


def status_response(energy=None, energies=None, vehicle_id=VEHICLE_ID):
    data = {"updatedAt": "2025-06-16T14:11:00Z",
            "lastPosition": {"type": "Feature",
                             "geometry": {"type": "Point", "coordinates": [7.4321, 46.9876, 540]},
                             "properties": {"updatedAt": "2025-06-16T14:10:50Z"}},
            "_links": {"self": {"href": "https://api.groupe-psa.com/connectedcar/v4/user/vehicles/" + vehicle_id
                                + "/status"}}}
    if energy is not None:
        data["energy"] = energy
    if energies is not None:
        data["energies"] = energies
    return data


def deserialize(data) -> CarStatus:
    response = SimpleNamespace(data=json.dumps(data).encode())
    status = OauthAPIClient().deserialize(response, "Status")
    status.__class__ = CarStatus
    return status


def charging(status: CarStatus):
    return status.get_energy("Electric").charging


class TestEnergiesFallback(unittest.TestCase):

    def test_status_with_only_energies_gets_its_energy(self):
        # GIVEN a status without the deprecated energy list
        # WHEN it is deserialized
        status = deserialize(status_response(energies=ELECTRIC_ENERGIES))

        # THEN the electric energy is read from energies
        electric = status.get_energy("Electric")
        self.assertEqual(63.0, electric.level)
        self.assertEqual(192, electric.autonomy)
        self.assertEqual(92, electric.battery.health.resistance)
        self.assertTrue(charging(status).plugged)
        self.assertEqual("InProgress", charging(status).status)
        self.assertEqual(11, charging(status).charging_rate)
        self.assertEqual("Slow", charging(status).charging_mode)
        self.assertEqual("PT2H10M", charging(status).remaining_time)
        # energies has no updatedAt: its createdAt dates the status like the energy list did
        self.assertEqual(datetime(2025, 6, 16, 14, 10, 50, tzinfo=timezone.utc), electric.updated_at)

    def test_same_result_as_with_the_energy_list(self):
        from_energy = deserialize(status_response(energy=ELECTRIC_ENERGY))
        from_energies = deserialize(status_response(energies=ELECTRIC_ENERGIES))

        self.assertEqual(from_energy.get_energy("Electric").to_dict(), from_energies.get_energy("Electric").to_dict())

    def test_hybrid_energies(self):
        status = deserialize(status_response(energies=copy.deepcopy(HYBRID_ENERGIES)))

        self.assertEqual(["Fuel", "Electric"], [energy.type for energy in status.energy])
        self.assertEqual(20.0, status.get_energy("Fuel").level)
        self.assertEqual(12.9, status.get_energy("Fuel").consumption)
        self.assertIsNone(status.get_energy("Electric").level)
        status.correct(False)
        self.assertEqual("Electric", status.energy[0].type)

    def test_energy_list_wins_over_energies(self):
        # GIVEN a status with both lists, as the API sends them now, but different values
        energies = copy.deepcopy(ELECTRIC_ENERGIES)
        energies[0]["level"] = 10.0

        status = deserialize(status_response(energy=ELECTRIC_ENERGY, energies=energies))

        # THEN the energy list is used as before
        self.assertEqual(63.0, status.get_energy("Electric").level)
        self.assertEqual(1, len(status.energy))

    def test_status_without_any_energy(self):
        status = deserialize(status_response())

        self.assertIsNone(status.energy)
        self.assertEqual(datetime(2025, 6, 16, 14, 11, tzinfo=timezone.utc), status_updated_at(status))

    def test_broken_energies_are_ignored(self):
        status = deserialize(status_response(energies=["Electric", {"type": "Fuel", "extension": "x"}]))

        self.assertEqual(["Fuel"], [energy.type for energy in status.energy])


class TestRawStatusLog(unittest.TestCase):

    def setUp(self):
        self.addCleanup(enable_raw_status_log, False)

    def logged(self, *responses):
        with self.assertLogs("psa_car_controller.psa.status_schema", level=logging.DEBUG) as logs:
            logging.getLogger("psa_car_controller.psa.status_schema").debug("start")
            for response in responses:
                deserialize(response)
        return [record.getMessage() for record in logs.records if "Raw vehicle status" in record.getMessage()]

    def test_not_logged_by_default(self):
        enable_raw_status_log(False)

        self.assertEqual([], self.logged(status_response(energy=ELECTRIC_ENERGY)))

    def test_logged_once_per_vehicle_without_position_and_ids(self):
        enable_raw_status_log()

        messages = self.logged(status_response(energy=ELECTRIC_ENERGY, energies=ELECTRIC_ENERGIES),
                               status_response(energy=ELECTRIC_ENERGY),
                               status_response(energy=ELECTRIC_ENERGY, vehicle_id="other"))

        self.assertEqual(2, len(messages))
        self.assertIn('"energies"', messages[0])
        self.assertIn('"nextDelayedTime"', messages[0])
        self.assertNotIn("46.9876", messages[0])
        self.assertNotIn(VEHICLE_ID, messages[0])

    def test_logged_status_is_the_raw_one(self):
        # the log shows what the API sent, not the energy list added from energies
        enable_raw_status_log()

        messages = self.logged(status_response(energies=ELECTRIC_ENERGIES))

        self.assertNotIn('"energy"', messages[0])


if __name__ == '__main__':
    unittest.main()
