"""
api/routes/websocket.py
WebSocket que empuja datos en tiempo real al dashboard.

FLUJO:
  ESP32 → MQTT → mqtt_bridge.py → Redis pub/sub → este WebSocket → Dashboard
  
El dashboard abre una conexión WS cuando carga la pantalla de un paciente.
Recibe actualizaciones de EMG, gestos y batería sin hacer polling.
"""

import json
import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import redis.asyncio as aioredis
from ...core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws/kit/{kit_id}")
async def kit_stream(websocket: WebSocket, kit_id: str):
    """
    El dashboard se conecta aquí para recibir datos del kit en tiempo real.
    
    Uso desde el frontend:
        const ws = new WebSocket(`ws://api.lumajira.com/ws/kit/LJ-0042`);
        ws.onmessage = (e) => updateDashboard(JSON.parse(e.data));
    
    Mensajes que recibirá el frontend:
        {"type": "emg",     "ch1": 0.82, "ch2": 0.34}
        {"type": "gesture", "gesture": "pinza", "confidence": 0.91}
        {"type": "battery", "pct": 74}
        {"type": "status",  "status": "online"}
    """
    await websocket.accept()
    logger.info(f"WebSocket conectado para kit {kit_id}")

    redis = await aioredis.from_url(settings.REDIS_URL)
    pubsub = redis.pubsub()

    # Suscribirse a todos los canales de este kit
    await pubsub.subscribe(
        f"kit:{kit_id}:emg",
        f"kit:{kit_id}:gesture",
        f"kit:{kit_id}:battery",
        f"kit:{kit_id}:status",
    )

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                # Agregar el tipo de mensaje basado en el canal
                channel = message["channel"].decode()
                msg_type = channel.split(":")[-1]  # "emg", "gesture", etc.
                data["type"] = msg_type

                await websocket.send_json(data)

    except WebSocketDisconnect:
        logger.info(f"WebSocket desconectado: kit {kit_id}")
    except Exception as e:
        logger.error(f"Error WebSocket {kit_id}: {e}")
    finally:
        await pubsub.unsubscribe()
        await redis.close()
