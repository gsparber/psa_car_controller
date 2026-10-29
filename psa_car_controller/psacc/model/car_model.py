DEFAULT_BATTERY_POWER = 46
DEFAULT_FUEL_CAPACITY = 0
DEFAULT_MAX_ELEC_CONSUMPTION = 70
DEFAULT_MAX_FUEL_CONSUMPTION = 30


class CarModel:
    # the YAML loader doesn't call __init__: a field missing from car_models.yml falls back to these,
    # the values of the "unknown" model
    name = "unknown"
    battery_power = DEFAULT_BATTERY_POWER
    fuel_capacity = DEFAULT_FUEL_CAPACITY
    abrp_name = None
    reg = None
    max_elec_consumption = DEFAULT_MAX_ELEC_CONSUMPTION
    max_fuel_consumption = DEFAULT_MAX_FUEL_CONSUMPTION

    # pylint: disable=too-many-positional-arguments,too-many-arguments
    def __init__(self, name, battery_power, fuel_capacity, abrp_name=None, reg=None,
                 max_elec_consumption=DEFAULT_MAX_ELEC_CONSUMPTION, max_fuel_consumption=DEFAULT_MAX_FUEL_CONSUMPTION):
        self.name = name
        self.battery_power = battery_power
        self.fuel_capacity = fuel_capacity
        self.abrp_name = abrp_name
        self.reg = reg
        self.max_elec_consumption = max_elec_consumption
        self.max_fuel_consumption = max_fuel_consumption

    def match(self, vin):
        if self.reg is None:
            return False
        return vin.startswith(self.reg)


class ElecModel(CarModel):
    fuel_capacity = 0
    max_fuel_consumption = 0

    # pylint: disable=too-many-positional-arguments,too-many-arguments
    def __init__(self, name, battery_power, abrp_name=None, reg=None,
                 max_elec_consumption=DEFAULT_MAX_ELEC_CONSUMPTION):
        super().__init__(name, battery_power, 0, abrp_name, reg, max_elec_consumption, 0)
