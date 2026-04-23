"""
db/models.py
Modelos SQLAlchemy para Lumajira.

ARQUITECTURA DE BASE DE DATOS:
┌─────────────────────────────────────────────────────────────┐
│  PostgreSQL (datos clínicos)    TimescaleDB (telemetría)     │
│  ─────────────────────────────  ──────────────────────────  │
│  clinics          kits          emg_readings  (hypertable)  │
│  prosthetists     sessions      gesture_events               │
│  patients         alerts        battery_logs                 │
│  notes            ota_logs      usage_hours                  │
└─────────────────────────────────────────────────────────────┘
     ↑ datos que cambian poco          ↑ datos que llegan
     (escribe 1x, lee mucho)             cada 50ms del ESP32
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, Boolean,
    DateTime, ForeignKey, Text, JSON, Enum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, DeclarativeBase
import uuid
import enum


class Base(DeclarativeBase):
    pass


def gen_uuid():
    return str(uuid.uuid4())


# ══════════════════════════════════════════════
# CLÍNICA Y USUARIOS
# ══════════════════════════════════════════════

class Clinic(Base):
    """Una clínica ortopédica que usa la licencia."""
    __tablename__ = "clinics"

    id           = Column(String, primary_key=True, default=gen_uuid)
    name         = Column(String(200), nullable=False)
    city         = Column(String(100))
    country      = Column(String(3), default="PER")  # ISO 3166
    license_active = Column(Boolean, default=True)
    license_since  = Column(DateTime, default=datetime.utcnow)
    created_at   = Column(DateTime, default=datetime.utcnow)

    prosthetists = relationship("Prosthetist", back_populates="clinic")
    patients     = relationship("Patient", back_populates="clinic")


class Prosthetist(Base):
    """El protesista que usa el dashboard."""
    __tablename__ = "prosthetists"

    id          = Column(String, primary_key=True, default=gen_uuid)
    clinic_id   = Column(String, ForeignKey("clinics.id"), nullable=False)
    name        = Column(String(200), nullable=False)
    email       = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow)

    clinic      = relationship("Clinic", back_populates="prosthetists")
    patients    = relationship("Patient", back_populates="prosthetist")
    notes       = relationship("ClinicalNote", back_populates="prosthetist")


# ══════════════════════════════════════════════
# PACIENTE
# ══════════════════════════════════════════════

class Patient(Base):
    """El paciente amputado que usa el kit biónico."""
    __tablename__ = "patients"

    id              = Column(String, primary_key=True, default=gen_uuid)
    clinic_id       = Column(String, ForeignKey("clinics.id"), nullable=False)
    prosthetist_id  = Column(String, ForeignKey("prosthetists.id"), nullable=False)
    name            = Column(String(200), nullable=False)
    age             = Column(Integer)
    occupation      = Column(String(200))
    city            = Column(String(100))
    amputation_type = Column(String(50))  # "transradial", "transhumeral", etc.
    dominant_side   = Column(String(10))  # "right", "left"
    rehab_week      = Column(Integer, default=1)
    rehab_target_weeks = Column(Integer, default=8)
    active          = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    clinic          = relationship("Clinic", back_populates="patients")
    prosthetist     = relationship("Prosthetist", back_populates="patients")
    kit             = relationship("Kit", back_populates="patient", uselist=False)
    notes           = relationship("ClinicalNote", back_populates="patient")
    alerts          = relationship("Alert", back_populates="patient")


# ══════════════════════════════════════════════
# KIT ELECTRÓNICO (HARDWARE)
# ══════════════════════════════════════════════

class KitStatus(str, enum.Enum):
    ACTIVE    = "active"
    INACTIVE  = "inactive"
    CHARGING  = "charging"
    OFFLINE   = "offline"
    UPDATING  = "updating"


class Kit(Base):
    """
    Representa un kit físico ESP32.
    Este es el puente entre la base de datos y el hardware.
    Cada kit tiene un topic MQTT único: lumajira/kit/{kit_id}/...
    """
    __tablename__ = "kits"

    id              = Column(String, primary_key=True, default=gen_uuid)
    serial          = Column(String(20), unique=True, nullable=False)  # ej: "LJ-0042"
    patient_id      = Column(String, ForeignKey("patients.id"), nullable=True)
    firmware_version = Column(String(20), default="2.3.1")
    status          = Column(Enum(KitStatus), default=KitStatus.OFFLINE)
    battery_pct     = Column(Integer, default=100)
    last_seen       = Column(DateTime)

    # Parámetros ajustables remotamente (se envían vía OTA/MQTT)
    emg_sensitivity = Column(Float, default=7.0)   # 1-10
    activation_threshold = Column(Float, default=4.0)
    motor_speed     = Column(Float, default=6.0)
    channel2_gain   = Column(Float, default=5.0)   # canal rotación

    created_at      = Column(DateTime, default=datetime.utcnow)

    patient         = relationship("Patient", back_populates="kit")
    ota_logs        = relationship("OTALog", back_populates="kit")


class OTALog(Base):
    """Historial de actualizaciones de firmware enviadas al kit."""
    __tablename__ = "ota_logs"

    id           = Column(String, primary_key=True, default=gen_uuid)
    kit_id       = Column(String, ForeignKey("kits.id"), nullable=False)
    version      = Column(String(20), nullable=False)
    changes      = Column(Text)   # qué cambió en esta versión
    sent_at      = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime)  # cuando el kit confirmó la actualización
    success      = Column(Boolean)

    kit          = relationship("Kit", back_populates="ota_logs")


# ══════════════════════════════════════════════
# TELEMETRÍA (TimescaleDB)
# Estas tablas van en la DB de timescale, no en PostgreSQL normal.
# Se crean con CREATE EXTENSION timescaledb; y SELECT create_hypertable(...)
# ══════════════════════════════════════════════

class EMGReading(Base):
    """
    Lectura EMG cruda del ESP32. Llegan cada 50ms (20 lecturas/seg).
    
    TIMESCALEDB HYPERTABLE — particionada por tiempo (1 chunk = 1 día).
    NO guardar todo en PostgreSQL normal: 20 lecturas/seg × 12 kits = 
    240 inserts/seg → TimescaleDB los maneja, Postgres normal no.
    
    Topic MQTT: lumajira/kit/{kit_id}/emg
    Payload:    {"ts": 1234567890, "ch1": 0.82, "ch2": 0.34, "ch3": 0.12}
    """
    __tablename__ = "emg_readings"

    time        = Column(DateTime, primary_key=True, default=datetime.utcnow)
    kit_id      = Column(String, primary_key=True)
    channel_1   = Column(Float)   # flexor superficial
    channel_2   = Column(Float)   # extensor
    channel_3   = Column(Float)   # referencia (ground)
    # channel_4 si el hardware lo soporta


class GestureEvent(Base):
    """
    Cada vez que el ESP32 clasifica un gesto exitoso.
    
    Topic MQTT: lumajira/kit/{kit_id}/gesture
    Payload:    {"ts": ..., "gesture": "pinza", "confidence": 0.91, "latency_ms": 98}
    """
    __tablename__ = "gesture_events"

    time        = Column(DateTime, primary_key=True, default=datetime.utcnow)
    kit_id      = Column(String, primary_key=True)
    gesture     = Column(String(50))       # "pinza", "agarre", "extension", "rotacion"
    confidence  = Column(Float)            # 0.0 - 1.0
    latency_ms  = Column(Integer)          # ms desde contracción hasta actuación
    correct     = Column(Boolean)          # null = desconocido, True/False en ejercicio guiado


class UsageSession(Base):
    """
    Sesión de uso del kit (encendido → apagado).
    
    Topic MQTT: lumajira/kit/{kit_id}/session
    Payload:    {"start": ..., "end": ..., "hours": 6.2}
    """
    __tablename__ = "usage_sessions"

    time        = Column(DateTime, primary_key=True, default=datetime.utcnow)
    kit_id      = Column(String, primary_key=True)
    start_time  = Column(DateTime)
    end_time    = Column(DateTime)
    hours       = Column(Float)
    avg_confidence = Column(Float)
    total_gestures = Column(Integer)


class BatteryLog(Base):
    """
    Nivel de batería cada 5 minutos.
    
    Topic MQTT: lumajira/kit/{kit_id}/battery
    Payload:    {"ts": ..., "pct": 74, "voltage": 3.7}
    """
    __tablename__ = "battery_logs"

    time        = Column(DateTime, primary_key=True, default=datetime.utcnow)
    kit_id      = Column(String, primary_key=True)
    pct         = Column(Integer)
    voltage     = Column(Float)


# ══════════════════════════════════════════════
# CLÍNICA / NOTAS / ALERTAS
# ══════════════════════════════════════════════

class ClinicalNote(Base):
    """Notas que escribe el protesista en el expediente del paciente."""
    __tablename__ = "clinical_notes"

    id              = Column(String, primary_key=True, default=gen_uuid)
    patient_id      = Column(String, ForeignKey("patients.id"), nullable=False)
    prosthetist_id  = Column(String, ForeignKey("prosthetists.id"), nullable=False)
    text            = Column(Text, nullable=False)
    ai_summary      = Column(Text)   # Resumen generado por OpenAI
    created_at      = Column(DateTime, default=datetime.utcnow)

    patient         = relationship("Patient", back_populates="notes")
    prosthetist     = relationship("Prosthetist", back_populates="notes")


class AlertSeverity(str, enum.Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


class Alert(Base):
    """
    Alerta generada por la IA cuando detecta algo fuera de lo normal.
    Puede ser generada por reglas (precisión < 50%) o por OpenAI.
    """
    __tablename__ = "alerts"

    id          = Column(String, primary_key=True, default=gen_uuid)
    patient_id  = Column(String, ForeignKey("patients.id"), nullable=False)
    kit_id      = Column(String, nullable=False)
    severity    = Column(Enum(AlertSeverity), default=AlertSeverity.MEDIUM)
    type        = Column(String(50))    # "low_precision", "no_usage", "battery", "firmware"
    message     = Column(Text)
    resolved    = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.utcnow)

    patient     = relationship("Patient", back_populates="alerts")
