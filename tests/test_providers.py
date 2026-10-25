"""Pure offline tests: no HA, credentials, SDK login, or network required.

Run from the repository root: python -m unittest discover -s tests \
    -p test_providers.py -v
"""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch


_PATH = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway/providers.py"
# Load relative pure helpers without importing the HA integration __init__.
_PACKAGE = ModuleType("_gateway_providers_offline_test")
_PACKAGE.__path__ = [str(_PATH.parent)]
sys.modules[_PACKAGE.__name__] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location(_PACKAGE.__name__ + ".providers", _PATH)
p = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = p
_SPEC.loader.exec_module(p)

MAC = "123456789ABC"
DID = "blt.synthetic"
KEY = bytes(range(16))  # Public synthetic test data, not a device credential.


def metadata(did=DID, mac=MAC, model=p.CO2_MODEL, name="Test CO2"):
    return {"did": did, "mac": mac, "model": model, "name": name,
            "token": "must never be copied", "beaconkey": "must never be copied"}


def acknowledgement(**changes):
    return {"did": DID, "siid": 8, "piid": 1029, "code": 0, **changes}


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.released = False

    async def text(self):
        return self.body

    def release(self):
        self.released = True


class Session:
    def __init__(self):
        self.headers = {"shared": "unchanged"}
        self.calls = []
        self.reply = Response(json.dumps({"code": 0, "result": [acknowledgement()]}))

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


class Cloud:
    sid = "xiaomiio"
    user_id = "test-user"
    default_server = "cn"
    service_token = "synthetic-token"
    ssecurity = "synthetic-security"

    def __init__(self):
        self.session = Session()
        self.device_reads = 0
        self.rows = [metadata()]
        self.prepared = []
        self.decrypts = 0

    async def async_get_devices(self, renew=False):
        assert renew is False
        self.device_reads += 1
        return self.rows

    def get_api_url(self, api):
        return "https://synthetic.invalid/app/" + api

    json_encode = staticmethod(json.dumps)

    def rc4_params(self, method, url, params):
        self.prepared.append((method, url, json.loads(params["data"])))
        return {"data": "synthetic-encrypted-data", "_nonce": "synthetic-nonce"}

    def api_session(self, **kwargs):
        assert kwargs == {"async": True}
        return self.session

    def signed_nonce(self, nonce):
        assert nonce == "synthetic-nonce"
        return "synthetic-signed-nonce"

    def decrypt_data(self, nonce, raw):
        assert nonce == "synthetic-signed-nonce"
        self.decrypts += 1
        # Crypto itself belongs to the SDK: mock its primitive, not its API.
        return base64.b64decode(raw, validate=True)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cloud = Cloud()
        self.hass = NS(data={"xiaomi_miot": {"entry": {"xiaomi_cloud": self.cloud}}})
        self.device = p.DeviceRef(MAC, DID, p.CO2_MODEL, "Test CO2")
        self.entry = NS(hass=self.hass, cloud=self.cloud, cloud_devices={DID: metadata()})
        hass_entry_module = ModuleType("custom_components.xiaomi_miot.core.hass_entry")
        hass_entry_module.HassEntry = NS(ALL={"entry": self.entry})
        # aiohttp isn't required in the offline test environment. Only its
        # timeout value object is used; Session above is entirely synthetic.
        aiohttp = ModuleType("aiohttp")
        aiohttp.ClientTimeout = lambda **kwargs: NS(**kwargs)
        self.modules = patch.dict(sys.modules, {
            "custom_components.xiaomi_miot.core.hass_entry": hass_entry_module,
            "aiohttp": aiohttp,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def home(self, uid="test-user", region="cn", rows=None):
        entry = NS(domain="xiaomi_home", data={"uid": uid, "cloud_server": region})
        self.hass.config_entries = NS(async_get_entry=lambda entry_id: entry)
        self.hass.data["xiaomi_home"] = {"miot_clients": {
            "home-entry": NS(device_list=rows if rows is not None else {DID: metadata(name="Home name")})}}

    def reply(self, result, code=0, status=200, encrypted=False):
        raw = json.dumps({"code": code, "result": result})
        if encrypted:
            raw = base64.b64encode(raw.encode()).decode()
        self.cloud.session.reply = Response(raw, status)

    async def test_target_scale_ble_token_is_ram_only(self):
        row = metadata(model=p.SCALE_MODEL)
        row["token"] = bytes(range(12)).hex()  # Deliberately public synthetic token.
        self.cloud.rows = [row]
        self.entry.cloud_devices = {DID: row}
        device = p.DeviceRef(MAC, DID, p.SCALE_MODEL, "Synthetic scale")
        self.assertEqual(await p.get_scale_ble_token(self.hass, "auto", device), bytes(range(12)))
        self.assertNotIn(row["token"], repr(device))
        self.assertEqual(self.cloud.session.calls, [])

    async def test_scale_ble_token_rejects_bindkey_and_conflicts(self):
        device = p.DeviceRef(MAC, DID, p.SCALE_MODEL, "Synthetic scale")
        for tokens in ([bytes(range(16)).hex()], [""],
                       [bytes(range(12)).hex(), bytes(reversed(range(12))).hex()]):
            self.cloud.rows = [{**metadata(model=p.SCALE_MODEL), "token": token}
                               for token in tokens]
            self.entry.cloud_devices = {DID: self.cloud.rows[0]}
            with self.assertRaises(p.ProviderError):
                await p.get_scale_ble_token(self.hass, "auto", device)
        self.assertEqual(self.cloud.session.calls, [])

    async def test_loaded_cache_and_home_metadata_merge(self):
        self.entry.cloud_devices[DID].pop("mac")
        self.home(rows={DID: metadata(mac="12:34:56:78:9a:bc", name="Home name")})
        accounts = p.available_accounts(self.hass)
        self.assertEqual(list(accounts), ["test-user_cn_xiaomiio"])
        self.assertTrue(accounts["test-user_cn_xiaomiio"].can_upload)
        devices = await p.get_devices(self.hass, "auto")
        self.assertEqual(devices, [p.DeviceRef(MAC, DID, p.CO2_MODEL, "Home name")])
        self.assertEqual(set(vars(devices[0])), {"mac", "did", "model", "name"})
        self.assertEqual(self.cloud.device_reads, 1)
        self.assertEqual(self.cloud.session.calls, [])

    async def test_filtered_cache_does_not_hide_full_account_devices(self):
        scale = metadata(did="blt.scale", mac="ABCDEF123456", model=p.SCALE_MODEL, name="Scale")
        self.cloud.rows = [metadata(), scale,
                           metadata(did="unsupported", model="other.model"),
                           metadata(did="no-mac", mac=None)]
        home_scale = dict(scale)
        home_scale.pop("mac")
        self.home(rows={"blt.scale": home_scale})
        for _ in range(2):
            devices = await p.get_devices(self.hass, "auto")
            self.assertEqual({device.did for device in devices}, {DID, "blt.scale"})
            self.assertEqual({device.mac for device in devices}, {MAC, "ABCDEF123456"})
        # The mock asserts renew=False on every public read; SDK cache policy
        # remains responsible for avoiding forced refreshes.
        self.assertEqual(self.cloud.device_reads, 2)
        self.assertEqual(set(self.entry.cloud_devices), {DID})
        self.assertEqual(self.cloud.session.calls, [])

    async def test_source_repr_excludes_runtime_and_raw_metadata(self):
        source = next(iter(p._sources(self.hass).values()))
        self.assertNotIn("must never be copied", repr(source))
        self.assertNotIn("token", repr(source))
        self.assertNotIn("cloud=", repr(source))
        self.assertNotIn("caches=", repr(source))
        self.assertNotIn("homes=", repr(source))

    async def test_home_only_is_explicitly_metadata_only(self):
        self.hass.data.pop("xiaomi_miot")
        self.entry.cloud = None
        self.home()
        account = next(iter(p.available_accounts(self.hass).values()))
        self.assertFalse(account.can_upload)
        self.assertEqual(len(await p.get_devices(self.hass, account.id)), 1)
        for operation in (p.get_bindkey(self.hass, account.id, self.device),
                          p.report_co2(self.hass, account.id, self.device, 500)):
            with self.assertRaisesRegex(p.ProviderError, "SOURCE_METADATA_ONLY"):
                await operation
        self.assertEqual(self.cloud.session.calls, [])

    async def test_no_model_guess_or_cross_account_join(self):
        self.entry.cloud_devices[DID].pop("mac")
        self.cloud.rows[0].pop("mac")
        self.home(uid="other-user")
        self.assertEqual(await p.get_devices(self.hass, "test-user_cn_xiaomiio"), [])
        self.home(region="de")
        self.assertEqual(await p.get_devices(self.hass, "test-user_cn_xiaomiio"), [])
        self.home(rows={"different-did": metadata(did="different-did")})
        devices = await p.get_devices(self.hass, "auto")
        self.assertEqual([row.did for row in devices], ["different-did"])
        with self.assertRaisesRegex(p.ProviderError, "DEVICE_NOT_VERIFIED"):
            await p.get_bindkey(self.hass, "auto", self.device)

    async def test_conflicting_and_unsupported_devices_are_excluded(self):
        self.entry.cloud_devices["second"] = metadata(did="second")
        self.entry.cloud_devices["unsupported"] = metadata(did="unsupported", model="other.model")
        self.assertEqual(await p.get_devices(self.hass, "auto"), [])
        self.entry.cloud_devices.pop("second")
        self.home(rows={DID: metadata(mac="ABCDEF123456")})
        self.assertEqual(await p.get_devices(self.hass, "auto"), [])

    async def test_public_device_fallback_and_source_reload(self):
        self.entry.cloud_devices = None
        await p.get_devices(self.hass, "auto")
        self.assertEqual(self.cloud.device_reads, 1)
        new_cloud = Cloud()
        self.hass.data["xiaomi_miot"]["entry"]["xiaomi_cloud"] = new_cloud
        self.entry.cloud = new_cloud
        self.entry.cloud_devices = {DID: metadata()}
        await p.report_co2(self.hass, "auto", self.device, 750)
        self.assertEqual(self.cloud.session.calls, [])
        self.assertEqual(len(new_cloud.session.calls), 1)

    async def test_auto_refuses_multiple_miot_accounts_and_filters_sid(self):
        other = Cloud()
        other.user_id = "second-user"
        self.hass.data["xiaomi_miot"]["second"] = {"xiaomi_cloud": other}
        with self.assertRaisesRegex(p.ProviderError, "SOURCE_ACCOUNT_AMBIGUOUS"):
            await p.get_devices(self.hass, "auto")
        other.sid = "micoapi"
        self.assertEqual(len(p.available_accounts(self.hass)), 1)

    async def test_headers_payload_and_ack_wrappers(self):
        for result in ([acknowledgement()], {"results": [acknowledgement()]},
                       {"result": [acknowledgement()]}):
            self.reply(result)
            await p.report_co2(self.hass, "auto", self.device, 800)
        url, kwargs = self.cloud.session.calls[-1]
        self.assertTrue(url.endswith("v2/miotspec/prop_changed"))
        self.assertEqual(kwargs["headers"], {
            "MIOT-REQUEST-MODEL": p.CO2_MODEL,
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4", "Accept-Encoding": "identity"})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"].total, 25)
        self.assertEqual(self.cloud.session.headers, {"shared": "unchanged"})
        self.assertEqual(self.cloud.prepared[-1][2], {"props": [
            {"did": DID, "siid": 8, "piid": 1029, "value": 800}]})
        self.assertTrue(self.cloud.session.reply.released)

    async def test_environment_float_values_are_preserved(self):
        for temperature, humidity in ((0.0, 0.0), (-12.3, 54.0), (23, 55),
                                      (-30, 55.0), (100.0, 100.0)):
            with self.subTest(temperature=temperature, humidity=humidity):
                self.reply([acknowledgement(siid=7, piid=1001),
                            acknowledgement(siid=7, piid=1002)])
                result = await p.report_environment(self.hass, "auto", self.device, {
                    "temperature": temperature, "humidity": humidity})
                self.assertIsNone(result)
                props = self.cloud.prepared[-1][2]["props"]
                self.assertEqual(props, [
                    {"did": DID, "siid": 7, "piid": 1001, "value": temperature},
                    {"did": DID, "siid": 7, "piid": 1002, "value": humidity}])
                self.assertIs(type(props[0]["value"]), type(temperature))
                self.assertIs(type(props[1]["value"]), int)

    async def test_environment_temperature_float_tail_is_not_a_new_precision(self):
        self.reply([acknowledgement(siid=7, piid=1001)])
        await p.report_environment(self.hass, "auto", self.device,
                                   {"temperature": 27.700000762939453})
        self.assertEqual(self.cloud.prepared[-1][2]["props"][0]["value"], 27.7)

    async def test_environment_batch_and_reordered_ack_wrappers(self):
        acks = [acknowledgement(siid=7, piid=1002), acknowledgement(),
                acknowledgement(siid=7, piid=1001)]
        for reply in (acks, {"result": acks}, {"results": acks}):
            self.reply(reply)
            count = len(self.cloud.session.calls)
            self.assertIsNone(await p.report_environment(self.hass, "auto", self.device, {
                "co2": 789, "temperature": -4.7, "humidity": 62.0}))
            self.assertEqual(len(self.cloud.session.calls), count + 1)
            self.assertEqual(self.cloud.prepared[-1][2], {"props": [
                {"did": DID, "siid": 8, "piid": 1029, "value": 789},
                {"did": DID, "siid": 7, "piid": 1001, "value": -4.7},
                {"did": DID, "siid": 7, "piid": 1002, "value": 62}]})
            url, kwargs = self.cloud.session.calls[-1]
            self.assertTrue(url.endswith("v2/miotspec/prop_changed"))
            self.assertEqual(kwargs["headers"]["MIOT-REQUEST-MODEL"], p.CO2_MODEL)

    async def test_environment_invalid_selection_and_values_never_send(self):
        invalid = [None, [], {}, {"battery": 90}, {"temperature": 12, "secret-key": 9}]
        for key in ("temperature", "humidity"):
            invalid.extend({key: value} for value in (
                True, False, "12", None, float("nan"), float("inf"), -float("inf")))
        invalid.extend([{"humidity": -0.1}, {"humidity": 100.1}, {"humidity": 55.3},
                        {"temperature": -30.1}, {"temperature": 100.1},
                        {"temperature": -31}, {"temperature": 101},
                        {"co2": True}, {"co2": 1.5}, {"co2": -1}, {"co2": 65536}])
        for readings in invalid:
            with self.subTest(readings=readings):
                with self.assertRaisesRegex(p.ProviderError, "INVALID_VALUE") as caught:
                    await p.report_environment(self.hass, "auto", self.device, readings)
                self.assertIn(str(caught.exception), (
                    "INVALID_VALUE: invalid environment selection",
                    "INVALID_VALUE: invalid environment measurement"))
        self.assertEqual(self.cloud.device_reads, 0)
        self.assertEqual(self.cloud.session.calls, [])

    async def test_environment_batch_rejects_uncertain_ack_without_retry(self):
        co2 = acknowledgement()
        temp = acknowledgement(siid=7, piid=1001)
        humidity = acknowledgement(siid=7, piid=1002)
        invalid = [[co2, temp], [co2, temp, temp],
                   [co2, temp, dict(humidity, code=-1)],
                   [co2, temp, humidity, humidity],
                   [co2, temp, dict(humidity, piid=999)],
                   {"result": [co2, temp, humidity], "results": [co2, temp, humidity]}]
        for field, value in (("did", 123), ("did", True), ("did", None),
                             ("siid", 7.0), ("siid", True), ("piid", "1002"),
                             ("code", False), ("code", 0.0)):
            invalid.append([co2, temp, dict(humidity, **{field: value})])
        for reply in invalid:
            with self.subTest(reply=reply):
                self.reply(reply)
                count = len(self.cloud.session.calls)
                with self.assertRaises(p.ProviderError) as caught:
                    await p.report_environment(self.hass, "auto", self.device, {
                        "co2": 789, "temperature": -4.7, "humidity": 62.0})
                self.assertEqual(len(self.cloud.session.calls), count + 1)
                for secret in (DID, "789", "-4.7", "62.0", "temperature", "humidity"):
                    self.assertNotIn(secret, str(caught.exception))

    async def test_co2_wrapper_delegates(self):
        with patch.object(p, "report_environment", return_value=None) as report:
            self.assertIsNone(await p.report_co2(self.hass, "auto", self.device, 801))
        report.assert_awaited_once_with(self.hass, "auto", self.device, {"co2": 801})

    async def test_bindkey_is_validated_and_not_logged(self):
        self.reply({"beaconkey": KEY.hex()}, encrypted=True)
        with self.assertNoLogs(level="DEBUG"):
            self.assertEqual(await p.get_bindkey(self.hass, "auto", self.device), KEY)
        self.assertEqual(self.cloud.decrypts, 1)
        self.assertEqual(self.cloud.prepared[-1][2], {"did": DID, "pdid": 1})
        self.assertTrue(self.cloud.prepared[-1][1].endswith("v2/device/blt_get_beaconkey"))
        for key in ("01", "x" * 32, None, " " + KEY.hex()):
            self.reply({"beaconkey": key})
            with self.assertRaisesRegex(p.ProviderError, "invalid beaconkey"):
                await p.get_bindkey(self.hass, "auto", self.device)

    async def test_http_and_top_business_code_must_succeed(self):
        for code, status in ((-1, 200), (0, 302), (0, 401), (None, 200), (False, 200)):
            self.reply([acknowledgement()], code=code, status=status)
            with self.assertRaises(p.ProviderError):
                await p.report_co2(self.hass, "auto", self.device, 800)
        # A plaintext error JSON must not be misinterpreted as encrypted data.
        self.assertEqual(self.cloud.decrypts, 0)
        self.assertEqual(len(self.cloud.session.calls), 5)

    async def test_strict_property_acknowledgements(self):
        invalid = [None, [], {}, [acknowledgement(code=-1)],
                   [acknowledgement(did="wrong")], [acknowledgement(siid=9)],
                   [acknowledgement(piid=1)], [acknowledgement(code=False)],
                   [acknowledgement(), acknowledgement()],
                   {"results": [acknowledgement()], "result": [acknowledgement()]}]
        for result in invalid:
            with self.subTest(result=result):
                self.reply(result)
                with self.assertRaises(p.ProviderError):
                    await p.report_co2(self.hass, "auto", self.device, 800)
        self.assertEqual(len(self.cloud.session.calls), len(invalid))

    async def test_unknown_never_retries_or_exposes_raw_failure(self):
        for reply in (asyncio.TimeoutError("secret"), asyncio.CancelledError("secret"),
                      Response("secret raw invalid error body"), Response("[]")):
            with self.subTest(reply=type(reply).__name__):
                count = len(self.cloud.session.calls)
                self.cloud.session.reply = reply
                with self.assertRaisesRegex(p.ProviderError, "UNKNOWN") as caught:
                    await p.report_co2(self.hass, "auto", self.device, 800)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(self.cloud.session.calls), count + 1)
        self.reply([acknowledgement()], encrypted=True)
        await p.report_co2(self.hass, "auto", self.device, 800)

    async def test_scale_and_invalid_values_never_write(self):
        scale = p.DeviceRef(MAC, DID, p.SCALE_MODEL, "Scale")
        with self.assertRaisesRegex(p.ProviderError, "only supports CO2"):
            await p.report_co2(self.hass, "auto", scale, 800)
        for value in (True, 1.5, -1, 65536, "800"):
            with self.assertRaisesRegex(p.ProviderError, "INVALID_VALUE"):
                await p.report_co2(self.hass, "auto", self.device, value)
        self.assertEqual(self.cloud.session.calls, [])

    async def test_missing_token_does_not_login_or_send(self):
        self.cloud.service_token = None
        self.assertEqual(await p.get_devices(self.hass, "auto"), [self.device])
        with self.assertRaisesRegex(p.ProviderError, "SOURCE_ACCOUNT_NOT_READY"):
            await p.report_co2(self.hass, "auto", self.device, 800)
        self.entry.cloud_devices = None
        with self.assertRaisesRegex(p.ProviderError, "SOURCE_ACCOUNT_NOT_READY"):
            await p.get_devices(self.hass, "auto")
        self.assertEqual(self.cloud.device_reads, 0)
        self.assertEqual(self.cloud.session.calls, [])


class ScaleProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = p.DeviceRef(MAC, DID, p.SCALE_MODEL, "Synthetic scale")
        self.cloud = Cloud()
        self.cloud.rows = [metadata(model=p.SCALE_MODEL)]
        self.hass = NS(data={"xiaomi_miot": {"entry": self.cloud}})
        self.users = [{"accountId": f"profile-{i}", "uid": f"synthetic-{i}",
                       "accountCode": 40 + i, "type": 10 + i, "name": f"Person {i}",
                       "deviceId": DID, "height": "synthetic-preserved-field"}
                      for i in range(4)]
        self.expected = p.build_weight_record(model=p.SCALE_MODEL, did=DID,
                                             user=self.users[2], weight_kg=61.25,
                                             measurement_time=1800000000)
        self.reads = [[], [self.expected]]
        self.histories = {user["accountId"]: [] for user in self.users}
        self.history_active = 0
        self.history_peak = 0
        self.clock = patch.object(p.time, "time_ns", return_value=1800000100000000000)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.add_error = None
        self.calls = []
        self.target = patch.object(p, "_target", return_value=self.cloud)
        self.target.start()
        self.addCleanup(self.target.stop)
        self.request = patch.object(p, "_request", side_effect=self.fake_request)
        self.request.start()
        self.addCleanup(self.request.stop)
        self.yield_sleep = asyncio.sleep
        self.sleep = patch.object(p.asyncio, "sleep", return_value=None)
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def fake_request(self, cloud, model, api, payload):
        self.assertIs(cloud, self.cloud)
        self.assertEqual(model, p.SCALE_MODEL)
        self.calls.append((api, payload))
        if api.endswith("indexInfo"):
            return {"accountInfoList": self.users}
        if api.endswith("getUserDataByPage"):
            if payload["endTime"] == 0:
                self.assertEqual(payload, {
                    "model": p.SCALE_MODEL, "uid": next(
                        user["uid"] for user in self.users
                        if str(user["accountId"]) == payload["accountId"]),
                    "did": DID, "accountId": payload["accountId"],
                    "beginTime": 1800000100000, "endTime": 0, "pageSize": 20})
                self.history_active += 1
                self.history_peak = max(self.history_peak, self.history_active)
                # Yield without using the patched polling sleep.
                await self.yield_sleep(0)
                self.history_active -= 1
                rows = self.histories[payload["accountId"]]
                if isinstance(rows, BaseException):
                    raise rows
                return rows
            return self.reads.pop(0)
        if api.endswith("/add"):
            if self.add_error:
                raise self.add_error
            return {"irrelevant": "not a record ID or proof"}
        self.fail("unexpected API")

    async def report(self, profile_id="profile-2"):
        return await p.report_scale_weight(self.hass, "auto", self.device,
                                           profile_id, 61.25, 1800000000)

    def adds(self):
        return [payload for api, payload in self.calls if api.endswith("/add")]

    def history(self, index, weight, timestamp=1799999000, source="UserEditor"):
        row = p.build_weight_record(model=p.SCALE_MODEL, did=DID,
                                    user=self.users[index], weight_kg=weight,
                                    measurement_time=timestamp)
        data = json.loads(row["data"])
        data["reportFrom"] = source
        return dict(row, data=json.dumps(data))

    async def nearest(self, weight=61.25):
        return await p.report_scale_weight_nearest(
            self.hass, "auto", self.device, weight, 1800000000)

    async def test_nearest_writes_only_winner_from_one_snapshot_and_reads_back(self):
        for i, weight in enumerate((60, 62, 61.2, 80)):
            self.histories[f"profile-{i}"] = [self.history(i, weight)]
        with self.assertNoLogs(level="DEBUG"):
            result = await self.nearest()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["matched_profile_id"], "profile-2")
        self.assertEqual(result["matched_profile_name"], "Person 2")
        self.assertEqual(result["difference"], 0.05)
        self.assertEqual(self.adds(), [self.expected])
        self.assertEqual(len([api for api, _ in self.calls if api.endswith("indexInfo")]), 1)
        self.assertEqual(len(result["references"]), 4)
        self.assertEqual(self.history_peak, 4)
        for ref in result["references"]:
            self.assertEqual(set(ref), {"profile_id", "name", "weight", "reference_time", "source"})
            self.assertEqual(ref["source"], "history")
        self.assertNotIn("user", result)
        self.assertNotIn("synthetic-preserved-field", repr(result))
        # No local baseline: the next weighing reads the newly uploaded record.
        self.histories["profile-2"] = [self.expected, self.history(2, 61.2)]
        refs = await p.get_scale_weight_references(self.hass, "auto", self.device)
        self.assertEqual(refs[2]["weight"], 61.25)
        self.assertEqual(refs[2]["reference_time"], 1800000000000)

    async def test_nearest_no_match_tie_no_reference_never_add(self):
        for weights, expected_status in (((None, None, None, None), "no_reference"),
                                         ((50, 80, None, None), "no_match"),
                                         ((60, 62.5, None, None), "ambiguous")):
            self.calls.clear()
            self.histories = {f"profile-{i}": [] if weight is None else [self.history(i, weight)]
                              for i, weight in enumerate(weights)}
            result = await self.nearest()
            self.assertEqual(result["status"], expected_status)
            self.assertEqual(result["code"], expected_status.upper())
            self.assertEqual(self.adds(), [])
            self.assertNotIn("matched_profile_id", result)
            self.assertEqual(len(result["references"]), 4)

    async def test_cloud_baseline_priority_missing_reference_and_boundary(self):
        self.histories["profile-2"] = [self.history(2, 80)]
        self.users[2].update(weightTarget=58.75, weightUpdateTime=1799999500000)
        result = await self.nearest()  # inclusive 2.5, other profiles have no reference
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["difference"], 2.5)
        self.assertEqual(result["references"][2]["source"], "cloud_baseline")
        self.assertEqual(result["references"][0], {
            "profile_id": "profile-0", "name": "Person 0", "weight": None,
            "reference_time": None, "source": "no_reference"})
        # Older baseline cannot override history; empty history can use baseline.
        self.users[2]["weightUpdateTime"] = 1799998000000
        refs = await p.get_scale_weight_references(self.hass, "auto", self.device)
        self.assertEqual(refs[2]["source"], "history")
        self.histories["profile-2"] = []
        refs = await p.get_scale_weight_references(self.hass, "auto", self.device)
        self.assertEqual(refs[2]["source"], "cloud_baseline")

    async def test_reference_failure_never_falls_back_or_adds(self):
        self.users[0].update(weightTarget=61.25, weightUpdateTime=1800000000000)
        for rows in (p.ProviderError("UNKNOWN: synthetic read failure"),
                     p.ProviderError("CLOUD_REJECTED: synthetic rejection"),
                     {"records": []}, [dict(self.history(0, 61), uid="wrong")],
                     [self.history(0, 61), self.history(0, 62)]):
            self.calls.clear()
            self.histories["profile-0"] = rows
            with self.assertRaises(p.ProviderError):
                await self.nearest()
            self.assertEqual(self.adds(), [])
            self.assertEqual(len([api for api, _ in self.calls
                                  if api.endswith("getUserDataByPage")]), 4)

    async def test_nearest_precheck_and_unknown_still_never_resend(self):
        self.histories["profile-2"] = [self.history(2, 61)]
        self.reads = [[self.expected]]
        self.assertEqual((await self.nearest())["status"], "existing_verified")
        self.assertEqual(self.adds(), [])
        self.reads = [[self.history(2, 60, 1800000000)]]
        self.assertEqual((await self.nearest())["status"], "conflict")
        self.assertEqual(self.adds(), [])
        self.reads = [[], [], [], []]
        self.add_error = p.ProviderError("UNKNOWN: synthetic timeout")
        with self.assertRaisesRegex(p.ProviderError, "UNKNOWN"):
            await self.nearest()
        self.assertEqual(len(self.adds()), 1)
        self.assertEqual(self.sleep_mock.await_count, 2)

    async def test_budget_cancellation_during_add_does_not_start_readback(self):
        self.histories["profile-2"] = [self.history(2, 61)]
        original_request = self.fake_request

        async def slow_add(cloud, model, api, payload):
            if not api.endswith("/add"):
                return await original_request(cloud, model, api, payload)
            self.calls.append((api, payload))
            try:
                await self.yield_sleep(10)
            except asyncio.CancelledError:
                # Same sanitization as the real _request transport boundary.
                raise p.ProviderError("UNKNOWN: cloud request cancelled; do not resend") from None

        original_timeout = asyncio.timeout
        with patch.object(p, "_request", side_effect=slow_add), patch.object(
                p.asyncio, "timeout", side_effect=lambda seconds: original_timeout(0.02)):
            with self.assertRaisesRegex(p.ProviderError, "UNKNOWN"):
                await self.nearest()
        self.assertEqual(len(self.adds()), 1)
        queries = [payload for api, payload in self.calls if api.endswith("getUserDataByPage")]
        self.assertEqual(len(queries), 5)  # four references + precheck, no readback

    async def test_profiles_are_did_scoped_preserved_and_not_in_repr(self):
        profiles = await p.get_scale_profiles(self.hass, "auto", self.device)
        self.assertEqual(len(profiles), 4)
        self.assertEqual(self.calls[0], ("eco/common/scale/indexInfo", {
            "uid": self.cloud.user_id, "accountId": self.cloud.user_id,
            "deviceId": DID, "model": p.SCALE_MODEL}))
        self.assertEqual(profiles[2].user, self.users[2])
        self.assertNotIn("synthetic-preserved-field", repr(profiles[2]))
        profiles[2].user["height"] = "changed locally"
        self.assertEqual(self.users[2]["height"], "synthetic-preserved-field")
        self.users[0]["deviceId"] = "another-did"
        with self.assertRaisesRegex(p.ProviderError, "another device"):
            await p.get_scale_profiles(self.hass, "auto", self.device)
        self.assertEqual(self.adds(), [])

    async def test_exact_selected_profile_and_real_readback(self):
        with self.assertRaisesRegex(p.ProviderError, "SCALE_PROFILE_NOT_FOUND"):
            await self.report("missing")
        self.assertEqual(self.adds(), [])
        result = await self.report()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["measurement_time"], 1800000000)
        self.assertEqual(result["weight_kg"], 61.25)
        self.assertNotIn("user", result)
        self.assertEqual(self.adds(), [self.expected])
        queries = [payload for api, payload in self.calls if api.endswith("getUserDataByPage")]
        self.assertEqual(queries, [{"model": p.SCALE_MODEL, "uid": "synthetic-2",
                                   "did": DID, "accountId": "profile-2",
                                   "beginTime": 1800000001000, "endTime": 1799999999000,
                                   "pageSize": 20}] * 2)

    async def test_existing_conflict_and_ambiguity_never_write(self):
        conflicting = dict(self.expected)
        data = json.loads(conflicting["data"])
        data["weight"] = 61.251
        conflicting["data"] = json.dumps(data)
        unverified = dict(self.expected)
        data["weight"] = 61.25
        data["reportFrom"] = "Regular"
        unverified["data"] = json.dumps(data)
        for rows, status in (([self.expected], "existing_verified"),
                             ([conflicting], "existing_conflict"),
                             ([self.expected, conflicting], "existing"),
                             ([unverified], "existing")):
            self.reads = [rows]
            self.assertEqual((await self.report())["status"], status)
        self.assertEqual(self.adds(), [])

    async def test_unknown_write_only_queries_never_resends(self):
        self.add_error = p.ProviderError("UNKNOWN: synthetic timeout")
        self.assertEqual((await self.report())["status"], "verified")
        self.assertEqual(len(self.adds()), 1)
        self.calls.clear()
        self.reads = [[], [], [], []]
        with self.assertRaisesRegex(p.ProviderError, "UNKNOWN"):
            await self.report()
        self.assertEqual(len(self.adds()), 1)
        self.assertEqual(self.sleep_mock.await_count, 2)

    async def test_no_fabricated_success_for_bad_readback_or_shapes(self):
        wrong = dict(self.expected, did="wrong-device")
        for rows in ([wrong], {"records": []}, [{"data": {}}],
                     [self.expected, self.expected]):
            self.calls.clear()
            self.reads = [[], rows]
            with self.assertRaisesRegex(p.ProviderError, "UNKNOWN"):
                await self.report()
            self.assertEqual(len(self.adds()), 1)
        self.calls.clear()
        self.reads = [{"records": []}]
        with self.assertRaisesRegex(p.ProviderError, "UNKNOWN"):
            await self.report()
        self.assertEqual(self.adds(), [])
        self.reads = [[]]
        self.add_error = p.ProviderError("CLOUD_REJECTED: synthetic rejection")
        with self.assertRaisesRegex(p.ProviderError, "CLOUD_REJECTED"):
            await self.report()
        self.assertEqual(len(self.adds()), 1)

    def native(self, *, profile_index=2, weight_raw="6125", status=1):
        user = self.users[profile_index]
        user["uid"] = str(9007199254740993123 + profile_index)
        raw = [str(100 + i) for i in range(34)]
        raw[:7] = [user["uid"], str(user["accountCode"]), str(user["type"]),
                   weight_raw, "70", str(status), "1800000000"]
        # Import under the same synthetic package as providers, not HA.
        module = sys.modules[p.native_measurement.__module__]
        return module.parse_native_csv(",".join(["7"] + raw), offline=True)

    async def unified(self, native_record=None, mode="fixed", weight=99):
        return await p.report_scale_measurement(
            self.hass, "auto", self.device, weight_kg=weight,
            measurement_time=1800000000, mode=mode, profile_id="profile-2",
            native_record=native_record)

    async def test_native_status1_full_schema_uses_native_weight_and_time(self):
        native = self.native()
        self.expected = p.build_native_record(p.SCALE_MODEL, DID, self.users[2], native)
        self.reads = [[], [self.expected]]
        result = await self.unified(native)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["record_kind"], "native")
        self.assertEqual(result["weight_kg"], 61.25)  # NOT broadcast 99
        self.assertEqual(self.adds(), [self.expected])
        data = json.loads(self.adds()[0]["data"])
        self.assertEqual(len(data), 37)
        self.assertEqual(data["status"], 1)
        self.assertEqual(data["reportFrom"], "Offline")
        self.assertGreater(data["bmi"], 0)
        self.assertGreater(data["bodyResOriginZ50"], 0)
        self.assertNotIn("user", result)
        self.assertNotIn("raw_fields", result)
        self.assertNotIn("synthetic-preserved-field", repr(result))

    async def test_native_nearest_uses_native_not_broadcast_weight(self):
        native = self.native()
        self.expected = p.build_native_record(p.SCALE_MODEL, DID, self.users[2], native)
        self.reads = [[], [self.expected]]
        self.histories["profile-0"] = [self.history(0, 99)]
        self.histories["profile-2"] = [self.history(2, 61.2)]
        result = await self.unified(native, mode="nearest", weight=99)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["matched_profile_id"], "profile-2")
        self.assertEqual(result["difference"], 0.05)
        self.assertEqual(len(self.adds()), 1)

    async def test_native_profile_triple_conflict_never_writes_or_downgrades(self):
        native = self.native(profile_index=0)
        self.histories["profile-2"] = [self.history(2, 61.2)]
        for mode in ("fixed", "nearest"):
            self.calls.clear()
            result = await self.unified(native, mode=mode)
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(result["code"], "NATIVE_PROFILE_CONFLICT")
            self.assertEqual(self.adds(), [])
            self.assertFalse(any(payload.get("endTime") != 0 for api, payload in self.calls
                                 if api.endswith("getUserDataByPage")))
        # Check uid separately from duid/type, not merely an accountCode join.
        native = self.native()
        self.users[2]["uid"] = "9007199254740993999"
        self.assertEqual((await self.unified(native))["status"], "conflict")
        self.assertEqual(self.adds(), [])

    async def test_existing_usereditor_blocks_native_add_never_matches_full(self):
        native = self.native()
        editor = self.history(2, 61.25, 1800000000)
        self.expected = p.build_native_record(p.SCALE_MODEL, DID, self.users[2], native)
        for rows, status in (([editor], "conflict"), ([self.expected], "existing_verified"),
                             ([editor, self.expected], "conflict")):
            self.reads = [rows]
            result = await self.unified(native)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["record_kind"], "native")
        self.assertEqual(self.adds(), [])

    async def test_native_readback_numeric_equivalence_but_not_missing_body_fields(self):
        native = self.native()
        expected = p.build_native_record(p.SCALE_MODEL, DID, self.users[2], native)
        data = json.loads(expected["data"])
        numeric = {key: float(value) if type(value) is int else value
                   for key, value in data.items()}
        row = {**expected, "createTime": float(expected["createTime"]),
               "data": json.dumps(numeric)}
        self.reads = [[row]]
        self.assertEqual((await self.unified(native))["status"], "existing_verified")
        numeric.pop("bodyResOriginZ50")
        self.reads = [[{**row, "data": json.dumps(numeric)}]]
        self.assertEqual((await self.unified(native))["status"], "conflict")
        self.assertEqual(self.adds(), [])

    async def test_unknown_native_readback_usereditor_is_not_verified_or_rewritten(self):
        native = self.native()
        self.add_error = p.ProviderError("UNKNOWN: synthetic timeout")
        self.reads = [[], [self.history(2, 61.25, 1800000000)]]
        with self.assertRaisesRegex(p.ProviderError, "UNKNOWN"):
            await self.unified(native)
        self.assertEqual(len(self.adds()), 1)
        self.assertEqual(json.loads(self.adds()[0]["data"])["reportFrom"], "Offline")

    async def test_unknown_native_add_only_reads_never_second_weight_only_write(self):
        native = self.native()
        self.expected = p.build_native_record(p.SCALE_MODEL, DID, self.users[2], native)
        self.add_error = p.ProviderError("UNKNOWN: synthetic timeout")
        self.reads = [[], [], [], []]
        with self.assertRaisesRegex(p.ProviderError, "UNKNOWN"):
            await self.unified(native)
        self.assertEqual(self.adds(), [self.expected])
        self.assertEqual(len([api for api, _ in self.calls if api.endswith("getUserDataByPage")]), 4)
        self.assertEqual(json.loads(self.adds()[0]["data"])["reportFrom"], "Offline")
        self.assertEqual(self.sleep_mock.await_count, 2)
        self.calls.clear()
        self.reads = [[], [self.expected]]
        self.assertEqual((await self.unified(native))["status"], "verified")
        self.assertEqual(self.adds(), [self.expected])

    async def test_invalid_native_never_writes_and_time_is_not_replaced(self):
        native = self.native()
        native["data"].pop("bmi")
        with self.assertRaises(p.ProviderError):
            await self.unified(native)
        self.assertEqual(self.calls, [])
        native = self.native()
        result = await p.report_scale_measurement(
            self.hass, "auto", self.device, weight_kg=61.25,
            measurement_time=1800000001, mode="fixed", profile_id="profile-2",
            native_record=native)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(self.calls, [])

    async def test_unified_weight_only_retains_fixed_and_nearest_semantics(self):
        result = await self.unified(weight=61.25)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["record_kind"], "weight_only")
        self.assertEqual(json.loads(self.adds()[0]["data"])["reportFrom"], "UserEditor")
        self.calls.clear()
        self.histories["profile-2"] = [self.history(2, 61.2)]
        self.reads = [[self.expected]]
        result = await self.unified(mode="nearest", weight=61.25)
        self.assertEqual(result["status"], "existing_verified")
        self.assertEqual(result["matched_profile_id"], "profile-2")
        self.assertEqual(self.adds(), [])

    async def test_readback_can_poll_but_add_is_once(self):
        self.reads = [[], [], [], [self.expected]]
        self.assertEqual((await self.report())["status"], "verified")
        self.assertEqual(len(self.adds()), 1)
        self.assertEqual(self.sleep_mock.await_count, 2)


if __name__ == "__main__":
    unittest.main()
