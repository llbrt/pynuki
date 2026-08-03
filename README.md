# Domoticz Nuki plugin based on pynuki

Python plugin for Domoticz to interact with [Nuki](https://nuki.io) smart locks and openers
via a [Nuki Bridge](https://help.nuki.io/hc/en-001/sections/360004474718-Bridge)

The plugin is based on [pynuki](https://github.com/pschmitt/pynuki/)

## Status

| Operation | Status |
|-----------|--------|
| Bridge cloud discovery  | :white_check_mark: |
| Lock door switch  | :white_check_mark: |
| Lock unlatch switch  | :white_check_mark: |
| Lock door sensor  | should work |
| Opener ring to open  | should work |
| Opener electric strike actuation  | should work |
| Opener continuous mode  | should work |

## Requirements

The Nuki Bridge(s) must be setup and reachable from the Domoticz server with HTTPS.

Via the Nuki smartphone application (Android or iOS), enable API access on the bridge and note the API token.
Go to manage my devices, and select the bridge or wifi enabled smart lock and connect to the device.
Turn on the HTTP API and check the details in the screen. The API token should be 6-20 characters long,
even though the app allows you to set a longer one.

For a better experience, the plugin uses the callback feature of the Nuki Bridge. This requires your Domoticz server
to be reachable via HTTP by the Nuki Bridge, as HTTPS is not supported by the Nuki bridge. The IP address and
port used must be set on the plugin parameters.

Consider setting a constant local IP address on the bridge and the Domoticz server, for example with a DCHP reservation based on the MAC address.


## Usage

Clone this repository or extract the contents of an archive in the plugin folder of the Domoticz server.

The python libraries `requests` and `PyNaCl` are necessary. The plugin was tested with `PyNaCl` version `1.6.2`. You can install these with `pip3`:

```shell
pip3 install requests PyNaCl==1.6.2
```

In the `Hardware` page, select the type `Nuki Bridge`.

Enter a name and the required parameters: Nuki Bridge API token, Domoticz server IP and port for the callback.

If setting up of the callback fails, you should consider lowering the poll interval.

## Docker integration

`punuki` included in this repository requires the python libraries `requests` and `PyNaCl`.

The Docker image `domoticz/domoticz` includes `requests`.

The tricky parts are to add the dependency and to expose the port for the Nuki Bridge callback.

You may create a local image to get the dependency once for all or get the dependency at Domoticz
startup using the script `customstart.sh`.

You may use Docker compose to automatically configure the network.

### Example with local Docker image and Docker compose

Create a folder `Domoticz` and create a local `Dockerfile`:

```bash
mkdir Domoticz
cd Domoticz
cat > Dockerfile << EOF
FROM domoticz/domoticz:stable

RUN pip3 install PyNaCl==1.6.2
EOF
```

Create a local image named `domoticz-nuki`

```bash
docker build -t domoticz-nuki .
```

Prepare the Domoticz volume with the plugin

```bash
mkdir -p domoticz/plugins/
cd domoticz/plugins/
git clone https://github.com/llbrt/DomoticzNukiBridge.git
cd ../..
```

Create a docker compose file exposing the port for the callback (here the default port `55234`)

```bash
cat > docker-compose.yml << EOF
services:
  domoticz:
    image: domoticz-nuki:latest
    container_name: domoticz
    restart: unless-stopped
    ports:
      - "8080:8080"
      - "55234:55234"
    volumes:
      - ./domoticz:/opt/domoticz/userdata
EOF
```

Start Domoticz then setup the Nuki Bridge plugin in the Web UI

```bash
docker compose up -d
```
