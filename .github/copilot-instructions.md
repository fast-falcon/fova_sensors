# FOVA Sensors - Copilot Instructions

## Architecture Overview

This is a **distributed sensor management system** with a dual-role architecture:

- **Central Box**: Hub that collects data from multiple sensor boxes, forwards to a Django server, and hosts a dashboard UI
- **Sensor Boxes**: Edge devices that capture environmental data, audio, and health metrics, pushing to the central box

The system uses **hybrid encryption** (RSA + AES-GCM) for inter-box communication and encrypts all payloads before transmission.

## Core Components

### Configuration & Initialization
- `panel_main.py`: Entry point that checks `panel_config.json` to determine role (central/sensor) or triggers wizard
- `panel_config.py`: Manages JSON config with role, box_id, auth credentials; thread-safe caching via `_CONFIG_LOCK`
- `panel_wizard.py`: Initial setup wizard for role assignment and auth generation
- `panel_paths.py`: Centralized path management (sdcard root, database, crypto keys, audio, sensor logs)

### Data Flow: Sensor → Central
1. **Sensor box** captures data via `panel_sensors_local.py` (environment readings), `panel_audio_local.py` (audio segments)
2. **sensor_to_central.py**: Periodically encrypts payload with central's public key (from `/api/public_key`), sends via HTTP POST to `/api/sensor_push` with Basic Auth
3. **central_api.py**: Receives encrypted data, decrypts with private key, stores in SQLite via `central_sensors_link.py`
4. **central_sensors_link.py**: Maintains in-memory sensor state (`_SENSORS` dict), stores samples in DB for archival

### Data Flow: Central → Server
- **central_server_link.py**: Opens persistent TCP socket to Django server (port 9000), performs `hello`/`welcome` handshake
- Payloads are queued in `panel_db.outbox` table if offline, sent when connection restored
- Fetches config from `/monitoring/api/config/<central_id>` to update push intervals dynamically

### Encryption & Security
- `panel_crypto.py`: RSA key generation (2048-bit), hybrid encryption using PKCS1-OAEP (key) + AES-GCM (data)
- `panel_ssh.py`: SSH key generation for reverse tunneling to server
- `panel_net_common.py`: Basic Auth header parsing

### Data Persistence
- `panel_db.py`: SQLite with thread-safe access via `_DB_LOCK`; four tables:
  - `kv_store`: Config cache (push intervals, public keys)
  - `outbox`: Queued payloads for server
  - `sensor_samples`: Environmental data history per sensor
  - `audio_segments`: Metadata for audio recordings
- Sensors/audio stored on `/sdcard/panel/` (fallback to local `sdcard_sim` for testing)

### UI & APIs
- **central_api.py**: Flask dashboard at `/`, API endpoints for sensors (`/api/sensors_state`), debug endpoints
- **sensor_api.py**: Minimal Flask panel for sensor box
- Templates in `pannel/templates/` with Jinja2 (separate for central/sensor roles)

### Background Services
- `panel_sensors_local.py`: Spawns thread reading sensors (e.g., I2C, analog) every 5 sec
- `panel_audio_local.py`: Spawns thread capturing 30-sec audio segments
- `panel_monitor.py`: Health monitoring loop (uptime, free space, CPU)
- `panel_health.py`: System health metrics collection

### Root-Level Tools
- `wifi_tool.py`: WiFi management via `su_env` wrapper (disable/enable/scan)
- `ap_tool.py`: Access point configuration
- `hotspot_tool.py`: Hotspot setup
- `sensor_lesten.py`: Local sensor simulation for testing

## Key Patterns & Conventions

### Thread Safety
- Use `threading.RLock()` for shared state (config, database, sensor dict)
- All DB access goes through `_get_conn()` which enforces `check_same_thread=False`
- Functions like `load_config()` cache results in module-level globals to avoid repeated I/O

### Config Propagation
- **Push interval** management: Central stores per-sensor intervals in `kv_store` (key: `sensor_push_interval:<sensor_id>`), returns in `/api/sensor_config/<sensor_id>`
- Sensor fetches and applies new intervals dynamically in `sensor_to_central.py`
- Central config updates come from server via HTTP polling (60-sec intervals)

### Payload Structure
Both sensor→central and central→server use similar encrypted JSON payloads:
```json
{
  "type": "sensor_samples",
  "sensor_id": "...",
  "ts": "ISO-8601",
  "env": {"temperature": ..., "humidity": ...},
  "audio_summary": {"last_segment_ts": "...", "duration_sec": ...},
  "health": {"uptime_sec": ..., "free_space_mb": ...},
  "auth_user": "auto-generated or provided",
  "auth_pass": "auto-generated or provided"
}
```

### Error Handling
- Network errors (URLError, HTTPError, socket timeouts) are caught and logged; operations retry in next cycle
- Missing config defaults to `None` with explicit checks; wizard is triggered if needed
- Fake crypto keys generated if PyCryptodome not installed (test mode)

### Naming Conventions
- **box_id**: Unique identifier (`bx_<uuid>_<random>`) for physical boxes
- **sensor_id**: For central box, reuses box_id; for sensor boxes, same as box_id
- **sensor_name**: Human-readable label (from config or defaults to box_id)

## Development Workflow

### Running Locally
1. Config: Edit or delete `pannel/panel_config.json` to test wizard or set role
2. Start: `python pannel/panel_main.py` (will prompt for setup if needed)
3. Web UI: Central dashboard at `http://localhost:8080`, sensor API at `http://localhost:8080`
4. Logs: Check console output; database at `sdcard_sim/panel_data.sqlite3` in test mode

### Testing Inter-Box Communication
- Use `sensor_lesten.py` to simulate sensor or test from separate terminal/box
- Central requires active sensor to push data before `/api/sensors_state` shows data
- Verify encryption in logs: "handshake OK", "payload encrypted"

### Key Files to Modify When
- Adding new sensor type: Update `panel_sensors_local.py` (sensor reading) and payload JSON fields
- Changing auth: Modify `sensor_to_central.py` Basic Auth logic or central config endpoints
- New dashboard widget: Add template in `pannel/templates/central/` and Flask route in `central_api.py`
- Server communication: Edit `central_server_link.py` handshake or payload format (must match Django backend)

## Important Caveats
- **Hardcoded paths**: `/opt/bin/su_env`, `/opt/bin/iw-arm` (WiFi tools expect ARM Linux environment)
- **Urdu comments**: Much of the codebase is commented in Farsi; docstrings explain functionality
- **No test suite**: Validation is manual; logs are primary debugging tool
- **PyCryptodome dependency**: Encryption falls back to fake keys if not installed; only for demo/testing
