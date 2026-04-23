"""
ai/analyzer.py
Integración con OpenAI GPT-4o para análisis clínico.

Tres usos principales:
1. Detectar anomalías en los datos del kit (reglas + IA)
2. Generar resúmenes del progreso del paciente para el protesista
3. Sugerir ajustes de parámetros cuando la precisión baja
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from openai import AsyncOpenAI

from ..core.config import settings
from ..db.session import get_db_session, get_timescale_session
from ..db.models import Alert, AlertSeverity, GestureEvent, Patient, Kit

logger = logging.getLogger(__name__)
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def check_for_anomaly(kit_id: str, anomaly_type: str, payload: dict):
    """
    Llamado desde el MQTT bridge cuando detecta algo inusual.
    Primero aplica reglas simples, luego consulta a OpenAI si es necesario.
    """

    # ── REGLAS SIMPLES (sin IA, más rápido) ──────────────────────
    if anomaly_type == "low_battery" and payload.get("pct", 100) < 15:
        await create_alert(
            kit_id=kit_id,
            severity=AlertSeverity.LOW,
            type="battery",
            message=f"Batería al {payload.get('pct')}%. El paciente debe cargar el kit."
        )
        return

    if anomaly_type == "low_confidence" and payload.get("confidence", 1.0) < 0.40:
        # Confianza muy baja → pedir análisis a OpenAI
        await analyze_precision_drop(kit_id, payload)
        return


async def analyze_precision_drop(kit_id: str, latest_gesture: dict):
    """
    Cuando la precisión baja inesperadamente, OpenAI analiza
    los últimos eventos y sugiere qué parámetro ajustar.
    """
    # Obtener últimos 50 gestos del kit para dar contexto a GPT
    async with get_timescale_session() as db:
        from sqlalchemy import select, desc
        result = await db.execute(
            select(GestureEvent)
            .where(GestureEvent.kit_id == kit_id)
            .order_by(desc(GestureEvent.time))
            .limit(50)
        )
        recent = result.scalars().all()

    if not recent:
        return

    # Preparar contexto para GPT
    gesture_summary = {}
    for g in recent:
        name = g.gesture or "unknown"
        if name not in gesture_summary:
            gesture_summary[name] = {"count": 0, "avg_confidence": 0, "confidences": []}
        gesture_summary[name]["count"] += 1
        gesture_summary[name]["confidences"].append(g.confidence or 0)

    for name in gesture_summary:
        confs = gesture_summary[name]["confidences"]
        gesture_summary[name]["avg_confidence"] = round(sum(confs) / len(confs), 2)
        del gesture_summary[name]["confidences"]

    # Consultar a OpenAI
    prompt = f"""
Eres un asistente clínico experto en prótesis biónicas mioeléctricas.

Datos del kit {kit_id} — últimos 50 gestos:
{json.dumps(gesture_summary, indent=2)}

Gesto que generó la alerta:
{json.dumps(latest_gesture, indent=2)}

Parámetros del sistema:
- Sensores EMG: 2 canales activos + 1 referencia
- Microcontrolador: ESP32-S3 con TFLite Micro
- Parámetros ajustables: emg_sensitivity (1-10), activation_threshold (1-10), 
  motor_speed (1-10), channel2_gain (1-10)

Basándote en estos datos:
1. ¿Cuál es el gesto con peor precisión y por qué puede estar fallando?
2. ¿Qué parámetro ajustarías primero y a qué valor? (sé específico: "channel2_gain de 5 a 8")
3. ¿Hay algún patrón preocupante que el protesista deba saber?

Responde en español, de forma concisa (máximo 3 párrafos). 
El protesista verá este texto directamente en su dashboard.
"""

    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.3,
        )
        analysis = response.choices[0].message.content

        await create_alert(
            kit_id=kit_id,
            severity=AlertSeverity.MEDIUM,
            type="low_precision",
            message=analysis
        )
        logger.info(f"Análisis OpenAI generado para kit {kit_id}")

    except Exception as e:
        logger.error(f"Error consultando OpenAI: {e}")
        # Fallback sin IA
        await create_alert(
            kit_id=kit_id,
            severity=AlertSeverity.MEDIUM,
            type="low_precision",
            message=f"Precisión cayó a {latest_gesture.get('confidence', 0)*100:.0f}%. Revisar calibración del canal 2."
        )


async def generate_weekly_summary(patient_id: str) -> str:
    """
    Genera un resumen semanal del paciente para el protesista.
    Se llama automáticamente cada viernes.
    
    Retorna: texto listo para mostrar en el dashboard o enviar por email.
    """
    async with get_db_session() as db:
        patient = await db.get(Patient, patient_id)
        if not patient or not patient.kit:
            return ""

    kit_id = patient.kit.id
    week_ago = datetime.utcnow() - timedelta(days=7)

    # Obtener estadísticas de la semana
    async with get_timescale_session() as db:
        from sqlalchemy import select, func
        result = await db.execute(
            select(
                GestureEvent.gesture,
                func.count(GestureEvent.gesture).label("count"),
                func.avg(GestureEvent.confidence).label("avg_conf")
            )
            .where(GestureEvent.kit_id == kit_id)
            .where(GestureEvent.time >= week_ago)
            .group_by(GestureEvent.gesture)
        )
        stats = [{"gesture": r.gesture, "count": r.count, "avg_conf": round(r.avg_conf or 0, 2)} for r in result]

    prompt = f"""
Eres un asistente de rehabilitación para prótesis biónicas.

Paciente: {patient.name}, {patient.age} años, {patient.occupation}
Semana de rehabilitación: {patient.rehab_week} de {patient.rehab_target_weeks}
Tipo de amputación: {patient.amputation_type}

Estadísticas de esta semana:
{json.dumps(stats, indent=2, ensure_ascii=False)}

Escribe un resumen semanal breve (2-3 párrafos) para el protesista Dr. Ríos, que incluya:
1. Progreso general del paciente esta semana
2. Gestos que van bien y cuál necesita más trabajo
3. Recomendación concreta para la próxima semana

Tono: profesional pero accesible. En español.
"""

    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.4,
    )
    return response.choices[0].message.content


async def suggest_exercises(patient_id: str) -> list[dict]:
    """
    Sugiere los ejercicios del día basándose en el rendimiento reciente.
    Retorna lista de ejercicios con nombre, repeticiones y duración.
    """
    # ... similar pattern: obtener datos, llamar a OpenAI, parsear JSON response
    pass


# ── HELPER ────────────────────────────────────────────────────────

async def create_alert(kit_id: str, severity: AlertSeverity, type: str, message: str):
    async with get_db_session() as db:
        from sqlalchemy import select
        kit = await db.get(Kit, kit_id)
        if not kit or not kit.patient_id:
            return

        alert = Alert(
            patient_id=kit.patient_id,
            kit_id=kit_id,
            severity=severity,
            type=type,
            message=message,
        )
        db.add(alert)
        await db.commit()
        logger.info(f"Alerta creada [{severity}]: {type} — kit {kit_id}")
