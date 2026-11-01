"""The web server handles requests in parallel: requests must not block each other or break what they share."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from psa_car_controller.common.utils import atomic_write
from psa_car_controller.psa.otp.otp import load_otp, save_otp
from psa_car_controller.psacc.application.charge_control import ChargeControl, ChargeControls
from psa_car_controller.psacc.application.charging import Charging
from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.model.car import Car, Cars
from psa_car_controller.psacc.model.charge import Charge
from psa_car_controller.psacc.repository.config_repository import ConfigRepository
from psa_car_controller.psacc.repository.db import Database
from tests.utils import get_new_test_db, get_web_view

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRITES = 150
SERVER = """
import sys
sys.argv = ["psacc", "--web-conf", "--remote-disable"]
from psa_car_controller.psacc.application.car_controller import PSACarController
from psa_car_controller.web import app
PSACarController().load_app()
app.run(app.config_flask("test", "/", False, "127.0.0.1", {port}))
"""


def run_in_threads(count, work):
    """Runs work(index) in threads that start together; raises what one of them raised."""
    barrier = threading.Barrier(count)

    def start(index):
        barrier.wait(timeout=10)
        return work(index)

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(start, range(count)))


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="psacc-threaded-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def assert_always_complete(self, name, write, read):
        """Writers in parallel while a reader looks at the file: it is always complete, nothing else stays behind."""
        write(0)
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    read()
                except Exception as e:  # pylint: disable=broad-except
                    bad.append(repr(e))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            run_in_threads(4, lambda index: [write(index * WRITES + i) for i in range(WRITES)])
        finally:
            stop.set()
            thread.join()
        self.assertEqual([], bad[:3])
        read()
        self.assertEqual([name], [f for f in os.listdir(self.dir) if f.startswith(name)])


class TestServerRunsInParallel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.workdir = tempfile.mkdtemp(prefix="psacc-threaded-server-")
        env = dict(os.environ, PYTHONPATH=ROOT)
        cls.process = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-c", SERVER.format(port=cls.port)], cwd=cls.workdir, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                cls.get("/health", timeout=2)
                return
            except OSError:
                time.sleep(0.2)
        cls.tearDownClass()
        raise RuntimeError("server didn't start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(10)
        shutil.rmtree(cls.workdir, ignore_errors=True)

    @classmethod
    def get(cls, path, timeout=3, headers=None):
        request = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - loopback only
            return response.read().decode("utf-8")

    def test_idle_connection_blocks_nobody(self):
        # what WebKit does before a page load: connect, then send nothing for ~30 s
        with socket.create_connection(("127.0.0.1", self.port)):
            time.sleep(0.2)
            started = time.monotonic()
            self.get("/health")
            self.assertLess(time.monotonic() - started, 2)

    def test_request_in_progress_blocks_nobody(self):
        # half a request: the thread of this connection waits for the rest
        with socket.create_connection(("127.0.0.1", self.port)) as sock:
            sock.sendall(b"POST /_dash-update-component HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
            time.sleep(0.2)
            self.get("/health")

    def test_each_request_keeps_its_prefix(self):
        def load(index):
            prefix = f"/api/hassio_ingress/token{index % 3}"
            wrong = []
            for _ in range(25):
                body = self.get("/", timeout=20, headers={"X-Ingress-Path": prefix})
                in_json = f'"requests_pathname_prefix":"{prefix}/"'.replace("/", "\\u002f")
                if in_json not in body or f'href="{prefix}/assets/' not in body:
                    wrong.append(prefix)
                # a sensor polling the API directly in between
                self.get("/health", timeout=20)
            return wrong

        self.assertEqual([[]] * 6, run_in_threads(6, load))


class TestFilesStayComplete(TempDirTest):
    def test_config_json(self):
        client = MagicMock()
        client.manager.refresh_token = "refresh"
        client.manager.proxies = {}
        client.service_information.client_secret = "secret"
        client.abrp = {}
        client.remote_client.remoteCredentials.refresh_token = "remote"
        client.account_info = SimpleNamespace(customer_id="c", client_id="i", realm="r", country_code="FR")
        client.weather_api = None
        client._config_hash = None  # pylint: disable=protected-access
        client._save_lock = threading.Lock()  # pylint: disable=protected-access
        name = self.path("config.json")

        def write(value):
            client.manager.refresh_token = f"refresh{value}"
            PSAClient.save_config(client, name)

        def read():
            with open(name, encoding="utf-8") as f:
                self.assertIn("refresh", json.load(f)["refresh_token"])

        self.assert_always_complete("config.json", write, read)

    def test_cars_json(self):
        cars = Cars([Car("VIN1", "id", "Peugeot"), Car("VIN2", "id2", "Peugeot")])
        name = self.path("cars.json")

        def read():
            with open(name, encoding="utf-8") as f:
                self.assertEqual(2, len(json.load(f)))

        self.assert_always_complete("cars.json", lambda _: cars.save_cars(name), read)

    def test_charge_config_json(self):
        controls = ChargeControls(self.path("charge_config.json"))
        controls["VIN1"] = ChargeControl(MagicMock(), "VIN1", 80, [1, 2])

        def write(value):
            controls["VIN1"].percentage_threshold = value % 100
            controls.save_config()

        def read():
            with open(controls.file_name, encoding="utf-8") as f:
                self.assertIn("percentage_threshold", json.load(f)["VIN1"])

        self.assert_always_complete("charge_config.json", write, read)

    def test_config_ini(self):
        config = ConfigRepository.config_file_to_dto(ConfigRepository.get_default_config())
        name = self.path("config.ini")

        def read():
            with open(name, encoding="utf-8") as f:
                self.assertIn("[Home Assistant MQTT]", f.read())

        self.assert_always_complete("config.ini", lambda _: config.write_config(name), read)

    def test_otp_bin(self):
        name = self.path("otp.bin")

        def read():
            self.assertEqual(["data"] * 2000, load_otp(name))

        self.assert_always_complete("otp.bin", lambda _: save_otp(["data"] * 2000, name), read)

    def test_slower_save_doesnt_put_older_values_back(self):
        # the last value set is what stays in the file, also when the save before it ends later
        cars = Cars([Car("VIN1", "id", "Peugeot")])
        name = self.path("cars.json")
        real_write = atomic_write
        first = threading.Event()
        second = threading.Event()

        def slow_write(file_name, data):
            if b'"old"' in data:
                first.set()
                second.wait(0.3)
            real_write(file_name, data)

        cars[0].label = "old"
        with patch("psa_car_controller.psacc.model.car.atomic_write", slow_write):
            old = threading.Thread(target=cars.save_cars, args=[name])
            old.start()
            first.wait(5)

            def save_new():
                cars[0].label = "new"
                cars.save_cars(name)
                second.set()

            new = threading.Thread(target=save_new)
            new.start()
            old.join()
            new.join()
        with open(name, encoding="utf-8") as f:
            self.assertEqual("new", json.load(f)[0]["label"])


class TestAtomicWrite(TempDirTest):
    def test_keeps_mode(self):
        name = self.path("config.json")
        atomic_write(name, b"1")
        os.chmod(name, 0o640)
        atomic_write(name, b"2")
        self.assertEqual(0o640, os.stat(name).st_mode & 0o777)

    def test_new_file_follows_umask(self):
        umask = os.umask(0o027)
        self.addCleanup(os.umask, umask)
        name = self.path("new.json")
        atomic_write(name, b"1")
        self.assertEqual(0o640, os.stat(name).st_mode & 0o777)

    def test_symlink_stays(self):
        target = self.path("target.json")
        link = self.path("link.json")
        atomic_write(target, b"1")
        os.symlink(target, link)
        atomic_write(link, b"2")
        self.assertTrue(os.path.islink(link))
        with open(target, "rb") as f:
            self.assertEqual(b"2", f.read())

    def test_failed_write_keeps_the_file(self):
        name = self.path("config.json")
        atomic_write(name, b"old")
        with patch("psa_car_controller.common.utils.os.fsync", side_effect=OSError(28, "No space left on device")), \
                self.assertRaises(OSError):
            atomic_write(name, b"new")
        with open(name, "rb") as f:
            self.assertEqual(b"old", f.read())
        self.assertEqual(["config.json"], os.listdir(self.dir))

    def test_file_mounted_on_its_own_is_written_in_place(self):
        # docker run -v ./config.json:/config/config.json: rename gives EBUSY
        name = self.path("config.json")
        atomic_write(name, b"old")
        with patch("psa_car_controller.common.utils.os.replace", side_effect=OSError(16, "Device or resource busy")), \
                self.assertLogs("psa_car_controller.common.utils", "WARNING"):
            atomic_write(name, b"new")
        with open(name, "rb") as f:
            self.assertEqual(b"new", f.read())
        self.assertEqual(["config.json"], os.listdir(self.dir))


class TestSharedDatabase(unittest.TestCase):
    """All threads share one sqlite connection, and with it one transaction."""

    def setUp(self):
        self.conn = get_new_test_db()
        self.addCleanup(Database.close_db)
        Database.set_db_callback(lambda: None)

    def count_soh(self):
        return self.conn.execute("SELECT COUNT(*) FROM battery_soh").fetchone()[0]

    def test_close_in_another_thread_keeps_a_write_in_progress(self):
        date = datetime(2026, 1, 1)
        with self.conn.write_lock:
            self.conn.execute("INSERT INTO battery_soh(date, VIN, level) VALUES(?,?,?)", (date, "VIN", 90.0))
            closer = threading.Thread(target=self.conn.close)  # a request that only read something
            closer.start()
            closer.join()
            self.conn.commit()
        self.assertEqual(1, self.count_soh())

    def test_close_still_rolls_back_its_own_write(self):
        self.conn.execute("INSERT INTO battery_soh(date, VIN, level) VALUES(?,?,?)",
                          (datetime(2026, 1, 1), "VIN", 90.0))
        self.conn.close()
        self.assertEqual(0, self.count_soh())

    def test_no_write_lost_between_readers(self):
        writes = 200

        def work(index):
            if index < 2:
                for i in range(writes):
                    Database.record_battery_soh(f"VIN{index}", datetime(2026, 1, 1) + timedelta(seconds=i), float(i))
            else:
                for _ in range(writes):
                    Database.get_last_soh_by_vin("VIN0")
                    Database.get_db().close()

        run_in_threads(6, work)
        self.assertEqual(2 * writes, self.count_soh())

    def test_charge_is_stored_before_the_network_request(self):
        seen = []

        def co2_request(*_):
            # a slow answer must not keep the transaction open: nothing to lose, no other writer waiting
            seen.append(self.conn.in_transaction)
            holder = threading.Thread(target=lambda: seen.append(self.conn.write_lock.acquire(timeout=1)))
            holder.start()
            holder.join()

        with patch("psa_car_controller.psacc.application.charging.Ecomix.get_data_from_co2_signal", co2_request):
            Charging.record_charging(Car("VIN-charge", "id", "Peugeot"), "InProgress", datetime(2026, 1, 1), 50, 1.0,
                                     2.0, "FR", "Slow", 3, 100, 1000)
        self.assertEqual([False, True], seen)
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM battery").fetchone()[0])

    def test_every_write_holds_the_write_lock(self):
        writes = []

        def trace(statement):
            words = statement.replace("(", " ").split()
            if words[0] in ("INSERT", "UPDATE", "DELETE"):
                table = words[1] if words[0] == "UPDATE" else words[2]
                writes.append((f"{words[0]} {table}", self.conn.write_lock._is_owned()))  # pylint: disable=W0212

        self.conn.set_trace_callback(trace)
        self.addCleanup(self.conn.set_trace_callback, None)
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(3):  # the same values three times: clean_position removes the one in the middle
            Database.record_position(None, "VIN", 10, 1.0, 2.0, None, date + timedelta(minutes=i), 50, None, False, 20)
        Database.record_battery_soh("VIN", date, 90.0)
        car = Car("VIN", "id", "Peugeot")
        ecomix = "psa_car_controller.psacc.application.charging.Ecomix."
        with patch(ecomix + "get_data_from_co2_signal"), patch(ecomix + "get_co2_per_kw", return_value=50):
            Charging.record_charging(car, "InProgress", date, 50, 1.0, 2.0, "FR", "Slow", 3, 100, 1000)
            Charging.record_charging(car, "Finished", date + timedelta(hours=1), 60, 1.0, 2.0, "FR", "Slow", 3, 100,
                                     1000)
        Database.set_chargings_price(self.conn, Charge(date, vin="VIN", price=5))
        positions = [{"elevation": 5, "location": {"lat": 1.0, "lng": 2.0}}]
        with patch("psa_car_controller.psacc.repository.db.utils.get_positions", return_value=positions):
            Database.add_altitude_to_db(self.conn)
        self.assertEqual({"INSERT position", "DELETE position", "INSERT battery_soh", "INSERT battery",
                          "INSERT battery_curve", "UPDATE battery", "DELETE battery", "UPDATE position"},
                         {write for write, _ in writes})
        self.assertEqual([], [write for write, owned in writes if not owned])

    def test_cursor_behaves_like_the_default_one(self):
        self.conn.execute("INSERT INTO battery_soh(date, VIN, level) VALUES(?,?,?)", (datetime(2026, 1, 1), "A", 1.0))
        cursor = self.conn.execute("INSERT INTO battery_soh(date, VIN, level) VALUES(?,?,?)",
                                   (datetime(2026, 1, 2), "B", 2.0))
        self.assertEqual(1, cursor.rowcount)
        self.conn.commit()
        select = "SELECT VIN FROM battery_soh ORDER BY VIN"
        self.assertEqual("A", self.conn.execute(select).fetchone()["VIN"])
        self.assertEqual(["A", "B"], [row["VIN"] for row in self.conn.execute(select)])
        self.assertEqual(2, len(self.conn.execute(select).fetchall()))
        cursor = self.conn.cursor()
        cursor.execute(select)
        self.assertEqual(["VIN"], [column[0] for column in cursor.description])
        self.assertEqual(1, len(cursor.fetchmany(1)))
        self.assertEqual(1, len(cursor.fetchall()))
        self.assertIsNone(cursor.fetchone())

    def test_same_query_in_parallel(self):
        self.conn.execute("INSERT INTO battery_soh(date, VIN, level) VALUES(?,?,?)", (datetime(2026, 1, 1), "A", 1.0))
        self.conn.commit()

        def work(_):
            for _ in range(1500):
                self.assertEqual("A", self.conn.execute("SELECT VIN FROM battery_soh").fetchone()["VIN"])

        run_in_threads(8, work)

    def test_callbacks_can_change_while_they_run(self):
        ran = threading.Event()
        carry_on = threading.Event()

        def slow_callback():
            ran.set()
            carry_on.wait(5)

        self.conn.callbacks.clear()
        self.conn.callbacks.update([slow_callback, lambda: None])
        errors = []

        def close():
            try:
                self.conn.execute_callbacks()
            except RuntimeError as e:
                errors.append(e)

        thread = threading.Thread(target=close)
        thread.start()
        ran.wait(5)
        Database.set_db_callback(lambda: None)  # what get_db does in every other thread
        carry_on.set()
        thread.join()
        self.assertEqual([], errors)


class TestCars(unittest.TestCase):
    def test_parallel_listing_adds_a_car_once(self):
        cars = Cars()
        real_get = Cars.get_car_by_vin

        def slow_get(self_, vin):
            res = real_get(self_, vin)
            time.sleep(0.02)
            return res

        with patch.object(Cars, "get_car_by_vin", slow_get):
            run_in_threads(4, lambda _: cars.add(Car("VIN1", "id", "Peugeot")))
        self.assertEqual(1, len(cars))


class TestServicesRunOneAtATime(unittest.TestCase):
    def test_login_and_reconnect_dont_overlap(self):
        from psa_car_controller.psacc.application.car_controller import PSACarController  # pylint: disable=C0415
        with patch("sys.argv", ["psacc", "--web-conf"]):
            app = PSACarController()
        running = []
        overlaps = []

        def start():
            running.append(1)
            time.sleep(0.05)
            overlaps.append(len(running))
            running.pop()

        myp = MagicMock()
        myp.remote_client.start.side_effect = start
        with patch.object(app, "myp", myp, create=True), patch.object(app.args, "remote_disable", False), \
                patch.object(app.args, "web_conf", False), patch.object(app.args, "charge_control", None):
            run_in_threads(4, lambda _: app.start_remote_control())
        self.assertEqual([1, 1, 1, 1], overlaps)


class TestPagesWithParallelRequests(unittest.TestCase):
    def setUp(self):
        self.views = get_web_view("views")
        self.car_access = get_web_view("car_access")
        self.client = self.views.dash_app.server.test_client

    def test_button_registers_its_callback_once(self):
        button = get_web_view("control").Button
        dash_app = self.views.dash_app
        real_callback = dash_app.callback

        def slow_callback(*args, **kwargs):
            time.sleep(0.05)
            return real_callback(*args, **kwargs)

        before = len(dash_app._callback_list)  # pylint: disable=protected-access
        with patch.object(dash_app, "callback", slow_callback):
            run_in_threads(4, lambda _: button("test-role", "VIN-once", "label", MagicMock()))
        self.assertEqual(before + 1, len(dash_app._callback_list))  # pylint: disable=protected-access

    def test_first_requests_wait_for_the_setup(self):
        dash_app = self.views.dash_app
        order = []
        real_setup = type(dash_app).__mro__[1]._setup_server  # pylint: disable=protected-access

        def slow_setup(self_):
            already_done = self_._got_first_request["setup_server"]  # pylint: disable=protected-access
            if not already_done:
                self_._got_first_request["setup_server"] = True  # pylint: disable=protected-access
                time.sleep(0.2)
                order.append("setup done")
            else:
                real_setup(self_)
            order.append("request goes on")

        started = threading.Barrier(2)

        def request(_):
            started.wait(5)
            dash_app._setup_server()  # pylint: disable=protected-access

        flag = dict(dash_app._got_first_request)  # pylint: disable=protected-access
        dash_app._got_first_request["setup_server"] = False  # pylint: disable=protected-access
        self.addCleanup(dash_app._got_first_request.update, flag)  # pylint: disable=protected-access
        with patch.object(type(dash_app).__mro__[1], "_setup_server", slow_setup):
            run_in_threads(2, request)
        self.assertEqual("setup done", order[0])

    def test_two_clicks_send_one_command(self):
        car = Car("VIN-click", "id", "Peugeot")
        remote = MagicMock()
        remote.commands.pending.return_value = None
        remote.track_commands.return_value.__enter__.return_value = []

        def wakeup(_):
            time.sleep(0.1)

        remote.wakeup.side_effect = wakeup
        fake_app = SimpleNamespace(myp=SimpleNamespace(remote_client=remote))
        with patch.object(self.car_access, "app", lambda: fake_app):
            results = run_in_threads(2, lambda _: self.car_access.send_command("refresh", car))
            self.assertEqual(1, remote.wakeup.call_count)
            self.assertEqual([False, True], sorted(res[1] for res in results))
            self.assertIn("busy", next(res[0] for res in results if not res[1]))
            # the car is free again when the other request is done
            self.assertTrue(self.car_access.send_command("refresh", car)[1])

    def test_failed_command_frees_the_car(self):
        car = Car("VIN-fail", "id", "Peugeot")
        remote = MagicMock()
        remote.commands.pending.return_value = None
        remote.track_commands.side_effect = RuntimeError("broken")
        fake_app = SimpleNamespace(myp=SimpleNamespace(remote_client=remote))
        with patch.object(self.car_access, "app", lambda: fake_app), self.assertRaises(RuntimeError):
            self.car_access.send_command("refresh", car)
        self.assertNotIn(car.vin, self.car_access._sending)  # pylint: disable=protected-access

    def test_layout_is_built_once_and_not_while_waiting_for_data(self):
        views = self.views
        builds = []

        def slow_summary():
            builds.append(1)
            time.sleep(0.05)
            return {}

        with patch.object(views, "DATA_READY", True), patch.object(views, "cached_layout", None), \
                patch.object(views.figures, "get_summary_cards", slow_summary), \
                patch.object(views, "create_card", lambda _: []):
            run_in_threads(4, lambda _: views.serve_layout())
        self.assertEqual(1, len(builds))

    def test_update_trips_can_finish_while_a_request_waits_for_data(self):
        views = self.views
        with patch.object(views, "DATA_READY", False), patch.object(views.APP, "is_good", True), \
                patch.object(views, "time", SimpleNamespace(sleep=lambda _: time.sleep(0.01))), \
                patch.object(views, "_cached_or_new_layout", return_value="layout"), \
                patch.object(views.Database, "get_db"), patch.object(views.Database, "add_altitude_to_db"), \
                patch.object(views, "_load_trips"):
            with ThreadPoolExecutor(1) as pool:
                waiting = pool.submit(views.serve_layout)
                time.sleep(0.05)
                views.update_trips()
                self.assertEqual("layout", waiting.result(timeout=5))

    def test_reads_during_a_client_swap_dont_fail(self):
        # a page reads the app while a login replaces the client (load_app): old or new client, never an error
        views = self.views
        app = views.APP
        cars = Cars([Car("VIN-swap", "id", "Peugeot")])
        clients = [MagicMock(vehicles_list=cars), MagicMock(vehicles_list=cars)]
        for client in clients:
            client.remote_client.commands.to_list.return_value = []
            client.remote_client.commands.pending.return_value = None
        stop = threading.Event()

        def swap():
            while not stop.is_set():
                for client in clients:
                    app.myp = client

        statuses = []

        def read(_):
            client = self.client()
            for _ in range(30):
                res = client.post("/_dash-update-component", json={
                    "output": "page-content.children", "outputs": {"id": "page-content", "property": "children"},
                    "inputs": [{"id": "url", "property": "pathname", "value": "/"},
                               {"id": "url", "property": "search", "value": ""}],
                    "changedPropIds": ["url.pathname"]})
                statuses.append(res.status_code)

        with patch.object(app, "myp", clients[0], create=True), patch.object(app, "is_good", True), \
                patch.object(app, "chc", None):
            swapper = threading.Thread(target=swap)
            swapper.start()
            try:
                run_in_threads(4, read)
            finally:
                stop.set()
                swapper.join()
        self.assertEqual({200}, set(statuses))


if __name__ == "__main__":
    unittest.main()
