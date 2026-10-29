import json
import os
import unittest
from configparser import ConfigParser
from unittest.mock import patch

from psa_car_controller.common.mylogger import my_logger

from psa_car_controller.psacc.repository.config_repository import ConfigRepository


class TestUnit(unittest.TestCase):
    def test_read_config(self):
        from psa_car_controller.psacc.repository.config_repository import ConfigRepository
        ConfigRepository.read_config()

    @patch("psa_car_controller.psacc.repository.config_repository.ConfigRepository._write")
    @patch("psa_car_controller.psacc.repository.config_repository.ConfigRepository._read_file")
    def test_read_old_config(self, mock_read, mock_write):
        old_config_str = """[General]
currency = €

[Electricity config]
day price = 42
night price = 0.1
night hour start = 1h00
night hour end = 4h42

"""
        mock_read.return_value = old_config_str
        conf = ConfigRepository.read_config()
        conf.write_config(conf)
        config_written: ConfigParser = mock_write.call_args_list[0][0][1]
        self.assertEqual(config_written["General"]["currency"].value, "€")
        self.assertEqual(config_written["Electricity config"]["day price"].value, 42)
        self.assertEqual(config_written["Electricity config"]["night price"].value, 0.1)
        self.assertEqual(str(config_written["Electricity config"]["night hour start"].value), "1h00")
        self.assertEqual(str(config_written["Electricity config"]["night hour end"].value), "4h42")
        conf.model_dump_json()

    def test_read_non_existent_config(self):
        from psa_car_controller.psacc.repository.config_repository import ConfigRepository
        self.assertRaises(FileNotFoundError, lambda: ConfigRepository._read_file("nonexistent.ini"))

    def test_read_config_non_existent_config(self):
        from psa_car_controller.psacc.repository.config_repository import ConfigRepository
        result = ConfigRepository.read_config("nonexistent.ini")
        expected_result = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        self.assertEqual(result, expected_result)

    @patch("psa_car_controller.psacc.repository.config_repository.ConfigRepository._write")
    def test_read_invalid_hour(self, mock_write):
        conf = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        with self.assertRaises(ValueError):
            conf.Electricity_config.night_hour_start = "200"
        self.assertIsNone(conf.Electricity_config.night_hour_start)
        conf.write_config()
        mock_write.assert_called_once()

    @patch("psa_car_controller.psacc.repository.config_repository.ConfigRepository._write")
    def test_write_numbers_from_web_settings(self, mock_write):
        # /settings/<section> converts every value that looks like a number
        conf = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        conf.General.currency = 1
        conf.Home_Assistant_MQTT.password = 1234
        conf.write_config()
        config_written: ConfigParser = mock_write.call_args_list[0][0][1]
        self.assertEqual("1234", config_written["Home Assistant MQTT"]["password"].value)
        self.assertEqual("1", config_written["General"]["currency"].value)

    def test_hours_are_serialized_as_text(self):
        conf = ConfigRepository(General={}, Electricity_config={"night_hour_start": "22h30", "night_price": "0.1"})
        hour = conf.Electricity_config.night_hour_start
        self.assertEqual((22, 30), (hour.hours, hour.minutes))
        electricity = json.loads(conf.model_dump_json())["Electricity_config"]
        self.assertEqual("22h30", electricity["night_hour_start"])
        self.assertIsNone(electricity["night_hour_end"])
        self.assertEqual(0.1, electricity["night_price"])


if __name__ == '__main__':
    my_logger(handler_level=os.environ.get("DEBUG_LEVEL", 20))
    unittest.main()
