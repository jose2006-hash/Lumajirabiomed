# Arquitectura de datos — Lumajira

## Flujo completo de un dato desde el ESP32 hasta el dashboard

```
ESP32 en el campo (Lima)
│
│  WiFi → Internet
│  Protocolo: MQTT (puerto 1883)
│  Payload: JSON liviano {"ts":..., "ch1":0.82, "ch2":0.34}
│
▼
MQTT Broker (Mosquitto)
│  Recibe y distribuye mensajes
│  Topics: lumajira/kit/LJ-0042/emg
│          lumajira/kit/LJ-0042/gesture
│          lumajira/kit/LJ-0042/battery
│
▼
mqtt_bridge.py (backend Python)
│  Escucha todos los topics
│  Parsea el JSON
│  Decide a dónde va cada dato
│
├──→ TimescaleDB (datos de alta frecuencia)
│    Tabla: emg_readings  ← 20 inserts/seg por kit
│    Tabla: gesture_events
│    Tabla: usage_sessions
│    Tabla: battery_logs
│    
│    ¿Por qué TimescaleDB y no PostgreSQL normal?
│    PostgreSQL normal: 240 inserts/seg → se degrada
│    TimescaleDB:       240 inserts/seg → sin problema
│    (partición automática por tiempo = "chunks")
│
├──→ PostgreSQL (datos clínicos)
│    Tabla: kits (actualizar last_seen, battery_pct, status)
│    Tabla: alerts (si hay anomalía)
│
├──→ Redis pub/sub (tiempo real)
│    Canal: kit:LJ-0042:emg
│    Canal: kit:LJ-0042:gesture
│    ← el WebSocket escucha aquí y empuja al browser
│
└──→ OpenAI GPT-4o (solo en anomalías)
     ← analiza los últimos 50 gestos
     ← genera mensaje de alerta para el protesista
     ← guarda en tabla alerts


## Diagrama de las dos bases de datos

┌─────────────────────────────────────────────────────────────┐
│  PostgreSQL: lumajira_db                                    │
│  (datos clínicos — cambian poco, se leen mucho)             │
│                                                             │
│  clinics ──→ prosthetists                                   │
│     └──→ patients ──→ kits ──→ ota_logs                     │
│               └──→ clinical_notes                           │
│               └──→ alerts                                   │
│                                                             │
│  Filas típicas: cientos a miles                             │
│  Queries: SELECT patient + kit + last alerts (< 5ms)        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  TimescaleDB: lumajira_telemetry                            │
│  (series de tiempo — inserts constantemente)                │
│                                                             │
│  emg_readings     → 20/seg/kit → 20M filas/día/kit          │
│  gesture_events   → ~1/seg/kit                              │
│  usage_sessions   → ~2/día/kit                              │
│  battery_logs     → 1 cada 5min/kit                         │
│                                                             │
│  Filas típicas: decenas de millones                         │
│  Queries: AVG por gesto en última semana (< 10ms)           │
│  Retención: EMG crudo 30 días, gestos forever               │
└─────────────────────────────────────────────────────────────┘

## ¿Cuándo usar cada tecnología?

| Dato                       | Dónde va        | Por qué                    |
|----------------------------|-----------------|----------------------------|
| Nombre del paciente        | PostgreSQL      | No cambia, pocas filas     |
| Parámetros del kit         | PostgreSQL      | Se actualiza 1x/semana     |
| Señal EMG (cada 50ms)      | TimescaleDB     | Millones de filas/día      |
| Gesto detectado            | TimescaleDB     | Serie temporal             |
| Batería (cada 5min)        | TimescaleDB     | Serie temporal             |
| EMG en vivo al browser     | Redis pub/sub   | Necesita < 100ms           |
| Nota clínica               | PostgreSQL      | Texto, pocas filas         |
| Alerta de IA               | PostgreSQL      | Dato clínico importante    |
| Sesión de usuario activa   | Redis           | Expira solo, rápido        |

## Resumen de puertos

| Servicio       | Puerto | Protocolo |
|----------------|--------|-----------|
| FastAPI        | 8000   | HTTP/WS   |
| PostgreSQL     | 5432   | TCP       |
| TimescaleDB    | 5432   | TCP (mismo servidor, distinta DB) |
| Redis          | 6379   | TCP       |
| MQTT Broker    | 1883   | MQTT      |
| MQTT WebSocket | 9001   | WS (opcional, para browser) |
```
