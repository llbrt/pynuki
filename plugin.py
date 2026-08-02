#!/usr/bin/env python3
# coding: utf-8
"""
<plugin key="NukiBridge" name="Nuki Bridge" author="llbrt" version="1.0.0"
        wikilink="https://github.com/llbrt/pynuki" externallink="https://nuki.io">
    <description>
        <h2>Nuki Bridge Plugin</h2>
        <p>Controls Nuki Smart Locks and Openers through one or more Nuki Bridges,
        using the Nuki Bridge HTTP API.</p>
        <h3>Features</h3>
        <ul style="list-style-type:square">
            <li>Automatic discovery of Nuki Bridges (Nuki cloud discovery service)</li>
            <li>Support for several bridges, each with several locks and/or openers</li>
            <li>Lock / Unlock / Unlatch control for Smart Locks, with battery and door
                sensor reporting</li>
            <li>Ring-to-Open, electric strike actuation and continuous mode control
                for Openers</li>
        </ul>
        <h3>Configuration</h3>
        <p><b>Bridge API Tokens</b> (required) is a list of <b>bridgeId=token</b> pairs
        separated by a semicolon, e.g.;</p>
        <p><code>123456789=aabbccddeeff...;987654321=00112233445566...</code></p>
        <p><b>Bridge IPs</b> (optional) lets you manually specify the address of bridges
        that could not be auto-discovered (e.g. bridges on a different subnet),
        formatted as <b>bridgeId=IP:port</b> pairs separated by a semicolon. If a bridge
        is auto-discovered, its discovered address always takes precedence over any
        address supplied here.</p>
        <p><b>Full Device Update</b> (optional) when True will recreate the deleted devices
        of existing locks or openers during the plugin startup or new devices if a new version
        of the plugin would create for a newly added bridge.</p>
        <h3>Bridge callbacks</h3>
        <p>The plugin runs a small HTTP server so that Nuki Bridges can notify it
        immediately of state changes (lock/unlock, door sensor, ring, ...) instead
        of waiting for the next poll. At startup, the plugin registers this
        callback URL on every valid bridge (reusing an existing registration if
        one is already present, and freeing up a slot on the bridge if needed).</p>
        <p><b>Server Address</b> (required) is the address the Nuki Bridge(s) should
        use to reach this plugin.</p>
        <p><b>Server Port</b> (required) is the local TCP port the plugin will listen
        on for bridge callbacks. <b>This port must be opened/forwarded on the
        Domoticz server's firewall</b> so that the Nuki Bridge(s) can reach the
        plugin over the network.</p>
    </description>
    <params>
        <param field="Mode1" label="Bridge API Tokens" width="500px" required="true" default=""/>
        <param field="Mode2" label="Bridge IPs" width="500px" required="false" default=""/>
        <param field="Mode4" label="Full Device Update" width="200px">
            <options>
                <option label="True" value="Full"/>
                <option label="False" value="Normal" default="true"/>
            </options>
        </param>
        <param field="Address" label="Server Address" width="200px" required="true" default=""/>
        <param field="Port" label="Server Port" width="100px" required="true" default="55234"/>
        <param field="Mode5" label="Poll Interval (s)" type="number" min="20" max="3600"
               step="20" default="300" width="100px"/>
        <param field="Mode6" label="Debug" width="200px">
            <options>
                <option label="True" value="Debug"/>
                <option label="False" value="Normal" default="true"/>
            </options>
        </param>
    </params>
</plugin>
"""

import json
import os
import sys

# Make sure the bundled 'pynuki' package (shipped alongside this plugin) can be
# imported regardless of Domoticz's working directory.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import DomoticzEx as Domoticz

_IMPORT_ERROR = None
try:
    import requests

    from pynuki import NukiBridge, NukiLock, NukiOpener
    from pynuki import constants as nuki_const
    from pynuki.bridge import InvalidCredentialsException
except ImportError as ex:  # pragma: no cover - depends on target environment
    _IMPORT_ERROR = str(ex)


# Domoticz device/unit numeric constants (see Domoticz wiki "Available Device Types")
TYPE_LIGHT_SWITCH = 244
SUBTYPE_SWITCH = 73
SWITCHTYPE_ONOFF = 0
SWITCHTYPE_CONTACT = 2
SWITCHTYPE_PUSH_ON = 9
SWITCHTYPE_DOOR_LOCK = 19

# Unit numbers used within a Lock device
U_LOCK = 1
U_UNLATCH = 2
U_DOORSENSOR = 3

# Unit numbers used within an Opener device
U_RTO = 1
U_STRIKE = 2
U_CONTINUOUS = 3

# A Nuki Bridge only has a limited number of callback slots available
NUKI_MAX_CALLBACKS = 3
CALLBACK_PATH = "/callback4Domoticz"

# Heart beat unit and default poll interval (should be the same that the step of the parameter 'Poll Interval')
HEART_BEAT_UNIT = 20
POLL_INTERVAL_DEFAULT = 300

def _parse_pairs(raw, label):
    """
    Parse a "key1=value1;key2=value2" string.
    Malformed entries are logged and ignored.
    Returns a dict of {key: value} with surrounding whitespace stripped.
    """
    result = {}
    if not raw:
        return result
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            Domoticz.Error(f"Ignoring malformed '{label}' entry: '{item}'")
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            Domoticz.Error(f"Ignoring malformed '{label}' entry: '{item}'")
            continue
        result[key] = value
    return result


def _parse_bridge_ips(raw):
    """
    Parse a "bridgeId1=IP1:port1;bridgeId2=IP2:port2" string.
    Malformed entries are logged and ignored.
    Returns a dict of {bridgeId: (ip, port)}.
    """
    result = {}
    pairs = _parse_pairs(raw, "Bridge IPs")
    for bridge_id, addr in pairs.items():
        ip, sep, port = addr.rpartition(":")
        if not sep or not ip or not port.isdigit():
            Domoticz.Error(f"Ignoring malformed 'Bridge IPs' entry: '{bridge_id}={addr}'")
            continue
        result[bridge_id] = (ip, int(port))
    return result


class BasePlugin:
    def __init__(self):
        # bridgeId (str) -> NukiBridge instance (successfully logged in)
        self.bridges = {}
        # bridgeId (str) -> NukiBridge instance, for every bridge we ever
        # logged into, even after it has since been marked invalid. Used at
        # onStop to still attempt callback removal.
        self.all_bridges = {}
        # bridgeId (str) -> True once a bridge has been marked as having an
        # invalid token; such bridges are ignored until the plugin is restarted
        self.invalid_bridges = set()
        # DeviceID (str, "<bridgeId>-<nukiId>") -> "lock" or "opener"
        self.device_kind = {}
        # nukiId (int) -> DeviceID (str), used to route incoming bridge
        # callbacks to the right Domoticz device
        self.nuki_id_index = {}
        # DeviceID (str) -> latest NukiLock/NukiOpener instance, kept around so
        # incoming callbacks can be merged into it via update_from_callback()
        self.nuki_devices = {}
        # The callback URL registered on the bridges, valid for the lifetime
        # of the plugin (set in onStart, used in onStop)
        self.callback_url = None
        # bridgeId (str) set of bridges on which our callback URL is known to
        # be registered; used at onStop to know where to remove it from
        self.callback_registered_bridges = set()
        # The Domoticz Connection object listening for incoming bridge callbacks
        self.listener_conn = None
        # Heart beat counter to do a real bridge update
        self.heart_beat_count = 0
        self.poll_interval = POLL_INTERVAL_DEFAULT

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _battery_level(self, dev):
        charge = getattr(dev, "battery_charge", None)
        if charge is not None:
            try:
                return max(0, min(100, int(charge)))
            except (TypeError, ValueError):
                pass
        critical = dev.battery_critical
        if critical is True:
            return 1
        if critical is False:
            return 100
        return 255

    def _mark_invalid_token(self, bridge_id):
        Domoticz.Error(
            f"Bridge {bridge_id}: token rejected by the bridge (HTTP 401). "
            "Ignoring this bridge until the plugin is restarted."
        )
        self.invalid_bridges.add(bridge_id)
        # Note: deliberately NOT removed from self.all_bridges - onStop still
        # needs the bridge object to attempt callback removal.
        self.bridges.pop(bridge_id, None)

    def _handle_bridge_exception(self, bridge_id, ex):
        """
        Returns True if the exception was handled (e.g. invalid token),
        False if it was just logged as a transient error.
        """
        if isinstance(ex, InvalidCredentialsException):
            self._mark_invalid_token(bridge_id)
            return True
        if isinstance(ex, requests.exceptions.HTTPError):
            status = ex.response.status_code if ex.response is not None else None
            if status == 401:
                self._mark_invalid_token(bridge_id)
                return True
        Domoticz.Error(f"Bridge {bridge_id}: request failed ({ex}).")
        return False

    # ------------------------------------------------------------------
    # Bridge callback (push notification) handling
    # ------------------------------------------------------------------

    def _ensure_bridge_callback(self, bridge_id, bridge):
        """
        Make sure our callback URL is registered on the given bridge. Reuses
        an existing matching registration if there is one, otherwise adds a
        new one, freeing up a slot (by removing the oldest registration) if
        the bridge has none available.
        """
        if not self.callback_url:
            return

        try:
            data = bridge.callback_list()
        except (InvalidCredentialsException, requests.exceptions.HTTPError) as ex:
            self._handle_bridge_exception(bridge_id, ex)
            return
        except Exception as ex:
            Domoticz.Error(f"Bridge {bridge_id}: failed to list callbacks ({ex}).")
            return

        callbacks = data.get("callbacks", []) if isinstance(data, dict) else []

        for cb in callbacks:
            if cb.get("url") == self.callback_url:
                Domoticz.Debug(
                    f"Bridge {bridge_id}: callback already registered "
                    f"({self.callback_url})."
                )
                self.callback_registered_bridges.add(bridge_id)
                return

        if len(callbacks) >= NUKI_MAX_CALLBACKS:
            victim = callbacks[0]
            try:
                bridge.callback_remove(victim.get("id"))
                Domoticz.Log(
                    f"Warning: Bridge {bridge_id} had no free callback slot; "
                    f"removed existing callback '{victim.get('url')}' to "
                    "make room for this plugin's callback."
                )
            except (InvalidCredentialsException, requests.exceptions.HTTPError) as ex:
                self._handle_bridge_exception(bridge_id, ex)
                return
            except Exception as ex:
                Domoticz.Error(
                    f"Bridge {bridge_id}: failed to remove callback "
                    f"'{victim.get('url')}' ({ex})."
                )
                return

        try:
            bridge.callback_add(self.callback_url)
            Domoticz.Log(f"Bridge {bridge_id}: registered callback {self.callback_url}")
            self.callback_registered_bridges.add(bridge_id)
        except (InvalidCredentialsException, requests.exceptions.HTTPError) as ex:
            self._handle_bridge_exception(bridge_id, ex)
        except Exception as ex:
            Domoticz.Error(f"Bridge {bridge_id}: failed to add callback ({ex}).")

    def _remove_bridge_callback(self, bridge_id):
        bridge = self.all_bridges.get(bridge_id)
        if bridge is None or not self.callback_url:
            return
        try:
            bridge.callback_remove_url(self.callback_url)
            Domoticz.Log(f"Bridge {bridge_id}: removed callback {self.callback_url}")
        except Exception as ex:
            Domoticz.Log(
                f"Warning: Bridge {bridge_id}: failed to remove callback "
                f"{self.callback_url} ({ex}). Continuing."
            )

    def _handle_callback_event(self, payload):
        """
        Process a JSON payload posted by a Nuki Bridge to our callback URL,
        identify the corresponding bridge/device and refresh its units.
        """
        nuki_id = payload.get("nukiId")
        if nuki_id is None:
            Domoticz.Log(f"Ignoring bridge callback event without a nukiId: {payload}")
            return

        device_id = self.nuki_id_index.get(nuki_id)
        if device_id is None:
            Domoticz.Log(
                f"Ignoring bridge callback event for unknown nukiId {nuki_id}."
            )
            return

        bridge_id = device_id.partition("-")[0]
        dev = self.nuki_devices.get(device_id)
        if dev is None:
            Domoticz.Log(
                f"Ignoring bridge callback event for {device_id}: no cached device state."
            )
            return

        Domoticz.Debug(f"Bridge {bridge_id}: callback event for device {device_id}: {payload}")
        try:
            dev.update_from_callback(payload)
        except Exception as ex:
            Domoticz.Error(f"Failed to apply callback event to {device_id}: {ex}")
            return

        if device_id in Devices:
            self._update_device(device_id, dev)

    # ------------------------------------------------------------------
    # Device / Unit creation
    # ------------------------------------------------------------------

    def _ensure_lock_units(self, device_id, dev):
        existing = Devices[device_id].Units if device_id in Devices else {}
        dev_name = getattr(dev, "name", "")
        Domoticz.Debug(f"Device {device_id}/{dev_name}: existing units {existing}")

        if U_LOCK not in existing:
            Domoticz.Unit(
                Name=f"Lock {dev_name}".strip(),
                DeviceID=device_id,
                Unit=U_LOCK,
                Type=TYPE_LIGHT_SWITCH,
                Subtype=SUBTYPE_SWITCH,
                Switchtype=SWITCHTYPE_DOOR_LOCK,
                Used=1,
                Description=dev.device_model_str,
            ).Create()

        existing = Devices[device_id].Units
        if U_UNLATCH not in existing:
            Domoticz.Unit(
                Name=f"Unlatch {dev_name}".strip(),
                DeviceID=device_id,
                Unit=U_UNLATCH,
                Type=TYPE_LIGHT_SWITCH,
                Subtype=SUBTYPE_SWITCH,
                Switchtype=SWITCHTYPE_PUSH_ON,
                Used=1,
            ).Create()

        existing = Devices[device_id].Units
        if dev.door_sensor_state is not None and U_DOORSENSOR not in existing:
            Domoticz.Unit(
                Name=f"Door Sensor {dev_name}".strip(),
                DeviceID=device_id,
                Unit=U_DOORSENSOR,
                Type=TYPE_LIGHT_SWITCH,
                Subtype=SUBTYPE_SWITCH,
                Switchtype=SWITCHTYPE_CONTACT,
                Used=1,
            ).Create()

    def _ensure_opener_units(self, device_id, dev):
        existing = Devices[device_id].Units if device_id in Devices else {}
        dev_name = getattr(dev, "name", "")

        if U_RTO not in existing:
            Domoticz.Unit(
                Name=f"Ring to Open {dev_name}".strip(),
                DeviceID=device_id,
                Unit=U_RTO,
                Type=TYPE_LIGHT_SWITCH,
                Subtype=SUBTYPE_SWITCH,
                Switchtype=SWITCHTYPE_ONOFF,
                Used=1,
                Description=dev.device_model_str,
            ).Create()

        existing = Devices[device_id].Units
        if U_STRIKE not in existing:
            Domoticz.Unit(
                Name=f"Electric Strike Actuation {dev_name}".strip(),
                DeviceID=device_id,
                Unit=U_STRIKE,
                Type=TYPE_LIGHT_SWITCH,
                Subtype=SUBTYPE_SWITCH,
                Switchtype=SWITCHTYPE_PUSH_ON,
                Used=1,
            ).Create()

        existing = Devices[device_id].Units
        if U_CONTINUOUS not in existing:
            Domoticz.Unit(
                Name=f"Continuous Mode {dev_name}".strip(),
                DeviceID=device_id,
                Unit=U_CONTINUOUS,
                Type=TYPE_LIGHT_SWITCH,
                Subtype=SUBTYPE_SWITCH,
                Switchtype=SWITCHTYPE_ONOFF,
                Used=1,
            ).Create()

    def _set_kind(self, device_id, dev):
        if isinstance(dev, NukiLock):
            self.device_kind[device_id] = "lock"
        elif isinstance(dev, NukiOpener):
            self.device_kind[device_id] = "opener"
        else:
            Domoticz.Log(
                f"Ignoring unsupported device type for {device_id} "
                f"({dev.device_type_str})."
            )
            return False
        return True

    def _ensure_units(self, device_id, dev):
        if isinstance(dev, NukiLock):
            self._ensure_lock_units(device_id, dev)
        elif isinstance(dev, NukiOpener):
            self._ensure_opener_units(device_id, dev)
        else:
            Domoticz.Log(
                f"Ignoring unsupported device type for {device_id} "
                f"({dev.device_type_str})."
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Device / Unit updates
    # ------------------------------------------------------------------

    def _update_unit(self, device_id, unit, nvalue, svalue, battery=None):
        u = Devices[device_id].Units[unit]
        changed = u.nValue != nvalue or u.sValue != svalue
        if battery is not None:
            changed = changed or u.BatteryLevel != battery
        if not changed:
            return
        u.nValue = nvalue
        u.sValue = svalue
        if battery is not None:
            u.BatteryLevel = battery
        u.Update()

    def _update_lock(self, device_id, dev):
        units = Devices[device_id].Units
        battery = self._battery_level(dev)

        if U_LOCK in units:
            nvalue = 1 if dev.is_locked else 0
            svalue = "Locked" if dev.is_locked else "Unlocked"
            self._update_unit(device_id, U_LOCK, nvalue, svalue, battery=battery)

        if U_DOORSENSOR in units and dev.door_sensor_state is not None:
            opened = bool(dev.is_door_sensor_activated)
            nvalue = 1 if opened else 0
            svalue = "Open" if opened else "Closed"
            self._update_unit(device_id, U_DOORSENSOR, nvalue, svalue)

    def _update_opener(self, device_id, dev):
        units = Devices[device_id].Units

        if U_RTO in units:
            nvalue = 1 if dev.is_rto_activated else 0
            svalue = "On" if nvalue else "Off"
            self._update_unit(device_id, U_RTO, nvalue, svalue)

        if U_CONTINUOUS in units:
            nvalue = 1 if dev.mode == nuki_const.MODE_OPENER_CONTINUOUS else 0
            svalue = "On" if nvalue else "Off"
            self._update_unit(device_id, U_CONTINUOUS, nvalue, svalue)

    def _update_device(self, device_id, dev):
        if isinstance(dev, NukiLock):
            self._update_lock(device_id, dev)
        elif isinstance(dev, NukiOpener):
            self._update_opener(device_id, dev)

    def _reset_push_button(self, device_id, unit):
        try:
            self._update_unit(device_id, unit, 0, "Off")
        except Exception as ex:
            Domoticz.Debug(f"Could not reset push button {device_id}/{unit}: {ex}")

    # ------------------------------------------------------------------
    # Bridge handling
    # ------------------------------------------------------------------

    def _refresh_bridge(self, bridge_id, bridge, create_missing=False, recreate_units=False):
        try:
            devices = bridge.devices
        except InvalidCredentialsException as ex:
            self._handle_bridge_exception(bridge_id, ex)
            return
        except requests.exceptions.HTTPError as ex:
            self._handle_bridge_exception(bridge_id, ex)
            return
        except Exception as ex:
            Domoticz.Error(f"Bridge {bridge_id}: failed to list devices ({ex}).")
            return

        for dev in devices:
            device_id = f"{bridge_id}-{dev.nuki_id}"
            if recreate_units or device_id not in Devices:
                if not create_missing:
                    continue
                if not self._ensure_units(device_id, dev):
                    continue
            self.nuki_id_index[dev.nuki_id] = device_id
            self.nuki_devices[device_id] = dev
            self._set_kind(device_id, dev)
            self._update_device(device_id, dev)

    # ------------------------------------------------------------------
    # Domoticz callbacks
    # ------------------------------------------------------------------

    def onStart(self):
        if Parameters.get("Mode6") == "Debug":
            Domoticz.Debugging(1)
            Domoticz.Debug("Debug mode enabled")

        recreate_units = False
        if Parameters.get("Mode4") == "Full":
            recreate_units = True

        # Setup heartbeat handling. No need to query the bridges too often, they are supposed to notify changes (callback)
        try:
            self.poll_interval = int(Parameters.get("Mode5") or POLL_INTERVAL_DEFAULT)
        except ValueError:
            pass
        Domoticz.Heartbeat(HEART_BEAT_UNIT)

        # --- Build the callback URL and start listening for bridge events --------
        address = (Parameters.get("Address") or "").strip()
        port = (Parameters.get("Port") or "").strip()
        if port:
            self.callback_url = f"http://{address}:{port}{CALLBACK_PATH}"
            Domoticz.Log(f"Bridge callback URL: {self.callback_url}")
            try:
                self.listener_conn = Domoticz.Connection(
                    Name="NukiCallbackListener",
                    Transport="TCP/IP",
                    Protocol="HTTP",
                    Port=port,
                )
                self.listener_conn.Listen()
            except Exception as ex:
                Domoticz.Error(
                    f"Failed to start callback listener on port {port}: {ex}. "
                    "Bridge push notifications will not be available; the "
                    "plugin will keep working using polling only."
                )
                self.listener_conn = None
                self.callback_url = None
        else:
            Domoticz.Error(
                "No 'Server Port' configured; bridge push notifications are "
                "disabled, the plugin will keep working using polling only."
            )

        if _IMPORT_ERROR:
            Domoticz.Error(
                "Could not import the 'pynuki' library or one of its "
                f"dependencies (requests, pynacl): {_IMPORT_ERROR}. "
                "Please install the missing package(s), e.g.: "
                "'pip3 install requests pynacl'."
            )
            return

        # --- 1. Discover bridges -------------------------------------------------
        discovered = {}
        try:
            found = NukiBridge.discover()
        except Exception as ex:
            Domoticz.Error(f"Bridge discovery failed: {ex}")
            found = None

        if found:
            for b in found:
                bridge_id = str(b.bridgeId)
                discovered[bridge_id] = (b.hostname, b.port)
                Domoticz.Log(
                    f"Discovered bridge {bridge_id} at {b.hostname}:{b.port}"
                )
        else:
            Domoticz.Log("No bridge discovered via the Nuki cloud discovery service.")

        # --- 2. Merge with manually configured bridge IPs -------------------------
        bridge_addrs = dict(discovered)
        manual_ips = _parse_bridge_ips(Parameters.get("Mode2", ""))
        for bridge_id, (ip, port) in manual_ips.items():
            if bridge_id in bridge_addrs:
                Domoticz.Log(
                    f"Bridge {bridge_id} was auto-discovered; ignoring the "
                    f"manually configured address ({ip}:{port}) in favor of "
                    "the discovered one."
                )
                continue
            bridge_addrs[bridge_id] = (ip, port)
            Domoticz.Log(f"Using manually configured bridge {bridge_id} at {ip}:{port}")

        if not bridge_addrs:
            Domoticz.Error(
                "No bridge address available (none discovered and none "
                "configured via 'Bridge IPs'). Aborting plugin start."
            )
            return

        # --- 3. Parse the API tokens ----------------------------------------------
        tokens = _parse_pairs(Parameters.get("Mode1", ""), "Bridge API Tokens")

        # --- 4. Keep only bridges that have both an address and a token ----------
        valid_bridges = {}
        for bridge_id, (ip, port) in bridge_addrs.items():
            token = tokens.get(bridge_id)
            if not token:
                Domoticz.Log(
                    f"Bridge {bridge_id} has no configured API token; ignoring it."
                )
                continue
            valid_bridges[bridge_id] = (ip, port, token)

        if not valid_bridges:
            Domoticz.Error(
                "No bridge with both a known address and a configured API "
                "token is available. Aborting plugin start."
            )
            return

        # --- 5. Log in to each bridge and create the Domoticz devices ------------
        for bridge_id, (ip, port, token) in valid_bridges.items():
            try:
                bridge = NukiBridge(hostname=ip, bridgeId=bridge_id, token=token, port=port)
            except InvalidCredentialsException:
                self._mark_invalid_token(bridge_id)
                continue
            except Exception as ex:
                Domoticz.Error(f"Bridge {bridge_id}: could not connect ({ex}). Ignoring it.")
                continue

            self.bridges[bridge_id] = bridge
            self.all_bridges[bridge_id] = bridge
            self._refresh_bridge(bridge_id, bridge, create_missing=True, recreate_units=recreate_units)
            self._ensure_bridge_callback(bridge_id, bridge)

        if not self.bridges:
            Domoticz.Error(
                "No bridge could be logged into successfully. The plugin "
                "will remain idle until it is restarted."
            )

    def onStop(self):
        Domoticz.Debug("onStop called")
        for bridge_id in list(self.callback_registered_bridges):
            # Try even if the bridge has since been marked invalid; failures
            # are logged as a warning and do not interrupt the loop.
            self._remove_bridge_callback(bridge_id)

        # Reset callback listener
        self.listener_conn = None

    def onHeartbeat(self):
        self.heart_beat_count += HEART_BEAT_UNIT
        if self.heart_beat_count < self.poll_interval:
            return

        self.heart_beat_count = 0
        for bridge_id in list(self.bridges.keys()):
            Domoticz.Debug(f"Refresh bridge {bridge_id} information")
            self._refresh_bridge(bridge_id, self.bridges[bridge_id], create_missing=True)

    def onConnect(self, Connection, Status, Description):
        if Status != 0:
            Domoticz.Error(f"Callback listener connection failed: {Description}")
            return

    def onMessage(self, Connection, Data):
        response_body = "{}"
        status = "200 OK"

        try:
            url = Data.get("URL", "")
            if url != CALLBACK_PATH:
                # Ignore unexpected URL
                status = "400 Bad Request"
            else:
                body = Data.get("Data", "") if isinstance(Data, dict) else ""
                payload = json.loads(body) if body else {}
                self._handle_callback_event(payload)
        except json.JSONDecodeError as ex:
            Domoticz.Error(f"Ignoring bridge callback: invalid JSON body ({ex}).")
            status = "400 Bad Request"
        except Exception as ex:
            Domoticz.Error(f"Error while processing bridge callback: {ex}")
            status = "500 Internal Server Error"

        try:
            Connection.Send(
                {
                    "Status": status,
                    "Headers": {
                        "Connection": "close",
                        "Content-Type": "application/json",
                    },
                    "Data": response_body,
                }
            )
        except Exception as ex:
            Domoticz.Debug(f"Failed to send response to bridge callback: {ex}")

    def onCommand(self, DeviceID, Unit, Command, Level, Color):
        bridge_id, sep, nuki_id_str = DeviceID.partition("-")
        if not sep:
            Domoticz.Error(f"Unexpected DeviceID '{DeviceID}', ignoring command.")
            return

        if bridge_id in self.invalid_bridges or bridge_id not in self.bridges:
            Domoticz.Error(
                f"Ignoring command for device {DeviceID}: bridge {bridge_id} "
                "is not available (invalid token or not connected)."
            )
            return

        bridge = self.bridges[bridge_id]
        try:
            nuki_id = int(nuki_id_str)
        except ValueError:
            Domoticz.Error(f"Unexpected DeviceID '{DeviceID}', ignoring command.")
            return

        kind = self.device_kind.get(DeviceID)
        cmd = (Command or "").strip().lower()

        try:
            if kind == "lock":
                if Unit == U_LOCK:
                    if cmd == "on":
                        bridge.lock(nuki_id)
                    else:
                        bridge.unlock(nuki_id)
                elif Unit == U_UNLATCH:
                    bridge.unlatch(nuki_id)
                    self._reset_push_button(DeviceID, Unit)
                else:
                    Domoticz.Log(f"Ignoring command on read-only unit {Unit} for {DeviceID}.")

            elif kind == "opener":
                if Unit == U_RTO:
                    action = (
                        nuki_const.ACTION_OPENER_ACTIVATE_RTO
                        if cmd == "on"
                        else nuki_const.ACTION_OPENER_DEACTIVATE_RTO
                    )
                    bridge.lock_action(nuki_id, action=action, device_type=nuki_const.DEVICE_TYPE_OPENER)
                elif Unit == U_STRIKE:
                    bridge.lock_action(
                        nuki_id,
                        action=nuki_const.ACTION_OPENER_ELECTRIC_STRIKE_ACTUATION,
                        device_type=nuki_const.DEVICE_TYPE_OPENER,
                    )
                    self._reset_push_button(DeviceID, Unit)
                elif Unit == U_CONTINUOUS:
                    action = (
                        nuki_const.ACTION_OPENER_ACTIVATE_CONTINUOUS
                        if cmd == "on"
                        else nuki_const.ACTION_OPENER_DEACTIVATE_CONTINUOUS
                    )
                    bridge.lock_action(nuki_id, action=action, device_type=nuki_const.DEVICE_TYPE_OPENER)
                else:
                    Domoticz.Log(f"Ignoring command on unknown unit {Unit} for {DeviceID}.")
            else:
                Domoticz.Error(f"Unknown device kind for {DeviceID}, ignoring command.")
                return
        except (InvalidCredentialsException, requests.exceptions.HTTPError) as ex:
            self._handle_bridge_exception(bridge_id, ex)
        except Exception as ex:
            Domoticz.Error(f"Command failed for {DeviceID}/{Unit}: {ex}")

        # Refreshing the device state shortly after sending a command isn't necessary, we can rely on
        # the call of the callback. We may also force a refresh on the next heart beat but setting
        # self.heart_beat_count to a high value.
        # self._refresh_bridge(bridge_id, bridge, create_missing=False)


global _plugin
_plugin = BasePlugin()


def onStart():
    global Devices, Parameters, Settings, Images
    _plugin.onStart()


def onStop():
    _plugin.onStop()


def onHeartbeat():
    _plugin.onHeartbeat()


def onConnect(Connection, Status, Description):
    _plugin.onConnect(Connection, Status, Description)


def onMessage(Connection, Data):
    _plugin.onMessage(Connection, Data)


def onCommand(DeviceID, Unit, Command, Level, Color):
    _plugin.onCommand(DeviceID, Unit, Command, Level, Color)
