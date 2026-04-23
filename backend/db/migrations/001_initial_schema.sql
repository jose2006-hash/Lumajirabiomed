-- ═══════════════════════════════════════════════════════════════
-- LUMAJIRA — Esquema de Base de Datos
-- ═══════════════════════════════════════════════════════════════
--
-- DOS bases de datos, un solo servidor PostgreSQL:
--
--   lumajira_db         → datos clínicos (cambian poco)
--   lumajira_telemetry  → datos del hardware (llegan constantemente)
--
-- ¿Por qué separar?
--   Los datos de telemetría (EMG) llegan a 20 lecturas/seg por kit.
--   Con 12 kits = 240 inserts/seg = 20 millones de filas/día.
--   TimescaleDB particiona esto automáticamente en "chunks" por tiempo,
--   haciendo las queries 10-100x más rápidas que PostgreSQL normal.
-- ═══════════════════════════════════════════════════════════════


-- ─── BASE DE DATOS PRINCIPAL ───────────────────────────────────
\c lumajira_db;

-- Clínicas
CREATE TABLE clinics (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(200) NOT NULL,
    city            VARCHAR(100),
    country         CHAR(3) DEFAULT 'PER',
    license_active  BOOLEAN DEFAULT TRUE,
    license_since   TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Protesistas (usuarios del dashboard)
CREATE TABLE prosthetists (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID REFERENCES clinics(id),
    name            VARCHAR(200) NOT NULL,
    email           VARCHAR(200) UNIQUE NOT NULL,
    password_hash   VARCHAR(200) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Pacientes
CREATE TABLE patients (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID REFERENCES clinics(id),
    prosthetist_id      UUID REFERENCES prosthetists(id),
    name                VARCHAR(200) NOT NULL,
    age                 INTEGER,
    occupation          VARCHAR(200),
    city                VARCHAR(100),
    amputation_type     VARCHAR(50),  -- 'transradial', 'transhumeral'
    dominant_side       VARCHAR(10),  -- 'right', 'left'
    rehab_week          INTEGER DEFAULT 1,
    rehab_target_weeks  INTEGER DEFAULT 8,
    active              BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Kits electrónicos (hardware)
CREATE TABLE kits (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    serial               VARCHAR(20) UNIQUE NOT NULL,  -- ej: 'LJ-0042'
    patient_id           UUID REFERENCES patients(id),
    firmware_version     VARCHAR(20) DEFAULT '2.3.1',
    status               VARCHAR(20) DEFAULT 'offline',
    battery_pct          INTEGER DEFAULT 100,
    last_seen            TIMESTAMPTZ,
    -- Parámetros ajustables remotamente
    emg_sensitivity      FLOAT DEFAULT 7.0,
    activation_threshold FLOAT DEFAULT 4.0,
    motor_speed          FLOAT DEFAULT 6.0,
    channel2_gain        FLOAT DEFAULT 5.0,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

-- Logs de actualizaciones OTA
CREATE TABLE ota_logs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kit_id       UUID REFERENCES kits(id),
    version      VARCHAR(20) NOT NULL,
    changes      TEXT,
    sent_at      TIMESTAMPTZ DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    success      BOOLEAN
);

-- Notas clínicas
CREATE TABLE clinical_notes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID REFERENCES patients(id),
    prosthetist_id  UUID REFERENCES prosthetists(id),
    text            TEXT NOT NULL,
    ai_summary      TEXT,  -- resumen generado por OpenAI
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Alertas (generadas por reglas o por OpenAI)
CREATE TABLE alerts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id  UUID REFERENCES patients(id),
    kit_id      VARCHAR(50) NOT NULL,
    severity    VARCHAR(10) DEFAULT 'medium',  -- 'low', 'medium', 'high'
    type        VARCHAR(50),  -- 'low_precision', 'battery', 'no_usage', 'firmware'
    message     TEXT,
    resolved    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Índices
CREATE INDEX idx_patients_clinic ON patients(clinic_id);
CREATE INDEX idx_kits_patient ON kits(patient_id);
CREATE INDEX idx_alerts_patient ON alerts(patient_id, resolved);
CREATE INDEX idx_notes_patient ON clinical_notes(patient_id, created_at DESC);


-- ─── BASE DE DATOS TELEMETRÍA (TIMESCALEDB) ────────────────────
\c lumajira_telemetry;

-- Habilitar TimescaleDB
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── Tabla 1: Señal EMG cruda ─────────────────────────────────
-- Llega cada 50ms desde el ESP32
-- 20 lecturas/seg × 12 kits = 240 inserts/seg
CREATE TABLE emg_readings (
    time        TIMESTAMPTZ NOT NULL,
    kit_id      VARCHAR(50) NOT NULL,
    channel_1   FLOAT,   -- flexor superficial (µV normalizado)
    channel_2   FLOAT,   -- extensor
    channel_3   FLOAT    -- referencia (ground noise)
);

-- Convertir a hypertable (particionada por tiempo, chunks de 1 día)
SELECT create_hypertable('emg_readings', 'time', chunk_time_interval => INTERVAL '1 day');

-- Política de retención: guardar solo 30 días de EMG crudo
-- (los gestos y estadísticas se guardan para siempre)
SELECT add_retention_policy('emg_readings', INTERVAL '30 days');

-- Compresión automática de datos viejos (ahorra 90% de espacio)
ALTER TABLE emg_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'kit_id'
);
SELECT add_compression_policy('emg_readings', INTERVAL '7 days');


-- ── Tabla 2: Eventos de gesto ────────────────────────────────
-- Cada vez que el kit clasifica un gesto exitoso
CREATE TABLE gesture_events (
    time        TIMESTAMPTZ NOT NULL,
    kit_id      VARCHAR(50) NOT NULL,
    gesture     VARCHAR(50),       -- 'pinza', 'agarre', 'extension', 'rotacion'
    confidence  FLOAT,             -- 0.0 - 1.0
    latency_ms  INTEGER,           -- ms desde contracción hasta actuación
    correct     BOOLEAN            -- null si libre, true/false en ejercicio guiado
);

SELECT create_hypertable('gesture_events', 'time', chunk_time_interval => INTERVAL '7 days');
-- Guardar gestos indefinidamente (son el historial clínico)


-- ── Tabla 3: Sesiones de uso ────────────────────────────────
-- Una fila por cada sesión de encendido/apagado del kit
CREATE TABLE usage_sessions (
    time           TIMESTAMPTZ NOT NULL,
    kit_id         VARCHAR(50) NOT NULL,
    start_time     TIMESTAMPTZ,
    end_time       TIMESTAMPTZ,
    hours          FLOAT,
    avg_confidence FLOAT,
    total_gestures INTEGER
);

SELECT create_hypertable('usage_sessions', 'time', chunk_time_interval => INTERVAL '7 days');


-- ── Tabla 4: Batería ────────────────────────────────────────
-- Nivel de batería cada 5 minutos
CREATE TABLE battery_logs (
    time    TIMESTAMPTZ NOT NULL,
    kit_id  VARCHAR(50) NOT NULL,
    pct     INTEGER,
    voltage FLOAT
);

SELECT create_hypertable('battery_logs', 'time', chunk_time_interval => INTERVAL '1 day');
SELECT add_retention_policy('battery_logs', INTERVAL '90 days');


-- ── Índices para queries frecuentes ─────────────────────────
CREATE INDEX ON gesture_events (kit_id, time DESC);
CREATE INDEX ON usage_sessions (kit_id, time DESC);
CREATE INDEX ON battery_logs   (kit_id, time DESC);


-- ═══════════════════════════════════════════════════════════════
-- QUERIES DE EJEMPLO (para el dashboard)
-- ═══════════════════════════════════════════════════════════════

-- Precisión promedio por gesto — última semana
-- SELECT gesture, 
--        COUNT(*) as total,
--        ROUND(AVG(confidence)::numeric * 100, 1) as avg_pct
-- FROM gesture_events
-- WHERE kit_id = 'LJ-0042'
--   AND time > NOW() - INTERVAL '7 days'
-- GROUP BY gesture
-- ORDER BY avg_pct DESC;

-- Uso diario — últimos 7 días
-- SELECT time_bucket('1 day', time) AS day,
--        ROUND(SUM(hours)::numeric, 1) AS total_hours
-- FROM usage_sessions
-- WHERE kit_id = 'LJ-0042'
--   AND time > NOW() - INTERVAL '7 days'
-- GROUP BY day
-- ORDER BY day;

-- EMG promedio por hora (downsampled de 50ms a 1 minuto)
-- SELECT time_bucket('1 minute', time) AS minute,
--        AVG(channel_1) as ch1_avg,
--        AVG(channel_2) as ch2_avg
-- FROM emg_readings
-- WHERE kit_id = 'LJ-0042'
--   AND time > NOW() - INTERVAL '1 hour'
-- GROUP BY minute
-- ORDER BY minute;
