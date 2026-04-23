"""
hardware/mqtt_bridge.py

Este es el archivo más importante para la conexión con el ESP32.

FLUJO COMPLETO:
  ESP32 (campo)
      │
      │ publica via WiFi
      ▼
  MQTT Broker (Mosquitto)          ← puerto 1883
      │
      │ este archivo escucha
      ▼
  mqtt_bridge.py
      │
      ├──→ TimescaleDB              ← datos crudos EMG, gestos, batería
      ├──→ Redis pub/sub            ← notifica al dashboard en tiempo real
      └──→ OpenAI (si alerta)       ← analiza anomalías

TOPICS MQTT que escucha este bridge:
  lumajira/kit/+/emg        ← señal EMG cruda (cada 50ms)
  lumajira/kit/+/gesture    ← gesto detectado
  lumajira/kit/+/session    ← inicio/fin de sesión de uso
  lumajira/kit/+/battery    ← nivel de batería (cada 5 min)
  lumajira/kit/+/status     ← conexión / desconexión del kit
  lumajira/kit/+/ota/ack    ← confirmación de actualización recibida
"""

import json
import asyncio
import logging
from datetime import datetime
from typing import Optional

import paho.mqtt.client as mqtt
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..db.session import get_db_session, get_timescale_session
from ..db.models import Kit, KitStatus, EMGReading, GestureEvent, UsageSession, BatteryLog, Alert
from ..ai.analyzer import check_for_anomaly

logger = logging.getLogger(__name__)


class MQTTBridge:
    """
    Puente entre el hardware ESP32 y la base de datos.
    
    Por qué MQTT y no HTTP directo:
    - El ESP32 tiene recursos limitados (RAM, CPU)
    - MQTT usa 10x menos ancho de banda que REST
    - Maneja reconexión automática si se cae el WiFi
    - Es el estándar de la industria para IoT
    """

    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.redis: Optional[aioredis.Redis] = None
        self._setup_callbacks()

    def _setup_callbacks(self):
        self.client.on_connect    = self._on_connect
        self.client.on_message    = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info("MQTT bridge conectado al broker")
            # Suscribirse a todos los kits ( + = wildcard de un nivel)
            topics = [
                "lumajira/kit/+/emg",
                "lumajira/kit/+/gesture",
                "lumajira/kit/+/session",
                "lumajira/kit/+/battery",
                "lumajira/kit/+/status",
                "lumajira/kit/+/ota/ack",
            ]
            for topic in topics:
                client.subscribe(topic, qos=1)
                logger.info(f"  ← suscrito a: {topic}")
        else:
            logger.error(f"MQTT error de conexión: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        logger.warning(f"MQTT desconectado (code={reason_code}). Reconectando...")

    def _on_message(self, client, userdata, msg):
        """Recibe cada mensaje del ESP32 y lo enruta."""
        try:
            # Extraer kit_id del topic: lumajira/kit/LJ-0042/emg → "LJ-0042"
            parts = msg.topic.split("/")
            kit_id = parts[2]
            msg_type = parts[3]  # emg, gesture, session, battery, status
            payload = json.loads(msg.payload.decode())

            # Enrutar al handler correcto
            asyncio.create_task(self._route(kit_id, msg_type, payload))

        except Exception as e:
            logger.error(f"Error procesando mensaje MQTT [{msg.topic}]: {e}")

    async def _route(self, kit_id: str, msg_type: str, payload: dict):
        handlers = {
            "emg":     self._handle_emg,
            "gesture": self._handle_gesture,
            "session": self._handle_session,
            "battery": self._handle_battery,
            "status":  self._handle_status,
            "ota":     self._handle_ota_ack,
        }
        handler = handlers.get(msg_type)
        if handler:
            await handler(kit_id, payload)

    # ──────────────────────────────────────────
    # HANDLERS
    # ──────────────────────────────────────────

    async def _handle_emg(self, kit_id: str, payload: dict):
        """
        Lectura EMG cruda. Llega cada 50ms.
        
        Payload del ESP32:
        {"ts": 1234567890, "ch1": 0.82, "ch2": 0.34, "ch3": 0.12}
        
        IMPORTANTE: NO hacer queries pesados aquí.
        Solo insertar en TimescaleDB (batch inserts) y publicar en Redis.
        """
        reading = EMGReading(
            time=datetime.fromtimestamp(payload["ts"]),
            kit_id=kit_id,
            channel_1=payload.get("ch1"),
            channel_2=payload.get("ch2"),
            channel_3=payload.get("ch3"),
        )

        async with get_timescale_session() as session:
            session.add(reading)
            await session.commit()

        # Publicar en Redis para el dashboard en tiempo real
        # El frontend escucha via WebSocket, que lee de Redis
        if self.redis:
            await self.redis.publish(
                f"kit:{kit_id}:emg",
                json.dumps({"ch1": payload.get("ch1"), "ch2": payload.get("ch2")})
            )

    async def _handle_gesture(self, kit_id: str, payload: dict):
        """
        Gesto detectado por la IA del ESP32.
        
        Payload: {"ts": ..., "gesture": "pinza", "confidence": 0.91, "latency_ms": 98}
        
        Aquí SÍ hacemos análisis porque es un evento importante, no datos crudos.
        """
        event = GestureEvent(
            time=datetime.fromtimestamp(payload["ts"]),
            kit_id=kit_id,
            gesture=payload.get("gesture"),
            confidence=payload.get("confidence"),
            latency_ms=payload.get("latency_ms"),
        )

        async with get_timescale_session() as session:
            session.add(event)
            await session.commit()

        # Publicar al dashboard
        if self.redis:
            await self.redis.publish(
                f"kit:{kit_id}:gesture",
                json.dumps(payload)
            )

        # Verificar si la confianza cayó por debajo del umbral → alerta
        if payload.get("confidence", 1.0) < 0.50:
            await check_for_anomaly(kit_id, "low_confidence", payload)

    async def _handle_session(self, kit_id: str, payload: dict):
        """
        Inicio o fin de sesión de uso.
        Payload: {"start": ..., "end": ..., "hours": 6.2, "total_gestures": 340}
        """
        session_record = UsageSession(
            time=datetime.utcnow(),
            kit_id=kit_id,
            start_time=datetime.fromtimestamp(payload["start"]),
            end_time=datetime.fromtimestamp(payload["end"]) if payload.get("end") else None,
            hours=payload.get("hours"),
            total_gestures=payload.get("total_gestures"),
        )
        async with get_timescale_session() as db:
            db.add(session_record)
            await db.commit()

    async def _handle_battery(self, kit_id: str, payload: dict):
        """
        Nivel de batería cada 5 minutos.
        Payload: {"ts": ..., "pct": 74, "voltage": 3.71}
        """
        log = BatteryLog(
            time=datetime.fromtimestamp(payload["ts"]),
            kit_id=kit_id,
            pct=payload.get("pct"),
            voltage=payload.get("voltage"),
        )
        async with get_timescale_session() as db:
            db.add(log)
            await db.commit()

        # Alerta si batería baja
        if payload.get("pct", 100) < 15:
            await check_for_anomaly(kit_id, "low_battery", payload)

        # Actualizar estado del kit en PostgreSQL
        async with get_db_session() as db:
            kit = await db.get(Kit, kit_id)
            if kit:
                kit.battery_pct = payload.get("pct")
                kit.last_seen = datetime.utcnow()
                await db.commit()

    async def _handle_status(self, kit_id: str, payload: dict):
        """
        El kit se conectó o desconectó.
        Payload: {"status": "online"} o {"status": "offline"}
        """
        status_map = {
            "online":   KitStatus.ACTIVE,
            "offline":  KitStatus.OFFLINE,
            "charging": KitStatus.CHARGING,
        }
        new_status = status_map.get(payload.get("status"), KitStatus.OFFLINE)

        async with get_db_session() as db:
            kit = await db.get(Kit, kit_id)
            if kit:
                kit.status = new_status
                kit.last_seen = datetime.utcnow()
                await db.commit()

        logger.info(f"Kit {kit_id}: {new_status}")

        # Notificar al dashboard
        if self.redis:
            await self.redis.publish(f"kit:{kit_id}:status", json.dumps(payload))

    async def _handle_ota_ack(self, kit_id: str, payload: dict):
        """
        El kit confirmó que recibió la actualización de firmware.
        Payload: {"version": "2.3.1", "success": true}
        """
        logger.info(f"Kit {kit_id} confirmó OTA: {payload}")

    # ──────────────────────────────────────────
    # ENVIAR COMANDOS AL KIT (MQTT → ESP32)
    # ──────────────────────────────────────────

    def send_params(self, kit_id: str, params: dict):
        """
        Enviar nuevos parámetros al kit. El protesista mueve un slider
        en el dashboard → esto llega al ESP32 por MQTT.
        
        Topic: lumajira/kit/{kit_id}/commands/params
        Payload: {"emg_sensitivity": 8, "motor_speed": 6, ...}
        """
        topic = f"lumajira/kit/{kit_id}/commands/params"
        self.client.publish(topic, json.dumps(params), qos=1)
        logger.info(f"Parámetros enviados a {kit_id}: {params}")

    def send_ota(self, kit_id: str, firmware_url: str, version: str):
        """
        Iniciar actualización OTA en el kit.
        El ESP32 descargará el firmware desde la URL y se reiniciará.
        
        Topic: lumajira/kit/{kit_id}/commands/ota
        Payload: {"url": "https://...", "version": "2.4.0", "checksum": "sha256:..."}
        """
        topic = f"lumajira/kit/{kit_id}/commands/ota"
        payload = {"url": firmware_url, "version": version}
        self.client.publish(topic, json.dumps(payload), qos=2)  # QoS 2 = exactly once
        logger.info(f"OTA enviado a {kit_id}: v{version}")

    def send_ota_all(self, firmware_url: str, version: str):
        """
        Actualizar TODOS los kits activos de una vez.
        Topic: lumajira/kit/broadcast/commands/ota
        """
        topic = "lumajira/kit/broadcast/commands/ota"
        payload = {"url": firmware_url, "version": version}
        self.client.publish(topic, json.dumps(payload), qos=2)
        logger.info(f"OTA broadcast enviado: v{version}")

    # ──────────────────────────────────────────
    # INICIAR EL BRIDGE
    # ──────────────────────────────────────────

    async def start(self):
        self.redis = await aioredis.from_url(settings.REDIS_URL)
        self.client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
        self.client.connect(settings.MQTT_BROKER_HOST, settings.MQTT_BROKER_PORT, keepalive=60)
        self.client.loop_start()
        logger.info("MQTT Bridge iniciado ✓")


# Instancia global
mqtt_bridge = MQTTBridge()
