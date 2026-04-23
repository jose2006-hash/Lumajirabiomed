"""
api/routes/patients.py
Endpoints REST para el dashboard del protesista.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from sqlalchemy import select, func

from ...db.models import Patient, Kit, GestureEvent, UsageSession, Alert
from ...db.session import get_db_session, get_timescale_session
from ...hardware.mqtt_bridge import mqtt_bridge
from ...ai.analyzer import generate_weekly_summary
from ..models.schemas import KitParamsUpdate, NoteCreate

router = APIRouter(prefix="/api/patients", tags=["patients"])


@router.get("/")
async def list_patients(db: AsyncSession = Depends(get_db_session)):
    """Lista todos los pacientes activos de la clínica."""
    result = await db.execute(select(Patient).where(Patient.active == True))
    patients = result.scalars().all()
    return patients


@router.get("/{patient_id}/dashboard")
async def patient_dashboard(patient_id: str, db: AsyncSession = Depends(get_db_session)):
    """
    Todos los datos que necesita el dashboard para mostrar
    el expediente completo de un paciente.
    Una sola llamada = toda la pantalla.
    """
    patient = await db.get(Patient, patient_id)
    if not patient:
        raise HTTPException(404, "Paciente no encontrado")

    kit = patient.kit
    if not kit:
        return {"patient": patient, "kit": None, "gestures": [], "usage": [], "alerts": []}

    # Estadísticas de gestos (última semana)
    week_ago = datetime.utcnow() - timedelta(days=7)
    async with get_timescale_session() as ts_db:
        gesture_stats = await ts_db.execute(
            select(
                GestureEvent.gesture,
                func.count(GestureEvent.gesture).label("total"),
                func.avg(GestureEvent.confidence).label("avg_confidence")
            )
            .where(GestureEvent.kit_id == kit.id)
            .where(GestureEvent.time >= week_ago)
            .group_by(GestureEvent.gesture)
        )
        gestures = [
            {
                "gesture": r.gesture,
                "total": r.total,
                "avg_confidence": round((r.avg_confidence or 0) * 100, 1)
            }
            for r in gesture_stats
        ]

        # Uso diario (últimos 7 días)
        usage_stats = await ts_db.execute(
            select(
                func.date_trunc("day", UsageSession.time).label("day"),
                func.sum(UsageSession.hours).label("total_hours")
            )
            .where(UsageSession.kit_id == kit.id)
            .where(UsageSession.time >= week_ago)
            .group_by("day")
            .order_by("day")
        )
        usage = [{"day": str(r.day.date()), "hours": round(r.total_hours or 0, 1)} for r in usage_stats]

    # Alertas activas
    alerts_result = await db.execute(
        select(Alert)
        .where(Alert.patient_id == patient_id)
        .where(Alert.resolved == False)
        .order_by(Alert.created_at.desc())
        .limit(5)
    )
    alerts = alerts_result.scalars().all()

    return {
        "patient": patient,
        "kit": kit,
        "gestures": gestures,
        "usage": usage,
        "alerts": alerts,
    }


@router.patch("/{patient_id}/kit/params")
async def update_kit_params(
    patient_id: str,
    params: KitParamsUpdate,
    db: AsyncSession = Depends(get_db_session)
):
    """
    Actualizar parámetros del kit remotamente.
    El protesista mueve un slider → esto envía el comando al ESP32 via MQTT.
    """
    patient = await db.get(Patient, patient_id)
    if not patient or not patient.kit:
        raise HTTPException(404, "Kit no encontrado")

    kit = patient.kit
    kit_id = kit.id

    # Actualizar en DB
    updates = params.dict(exclude_unset=True)
    for key, value in updates.items():
        setattr(kit, key, value)
    await db.commit()

    # Enviar al hardware via MQTT ← AQUÍ ES DONDE LLEGA AL ESP32
    mqtt_bridge.send_params(kit_id, updates)

    return {"ok": True, "sent_to_kit": kit_id, "params": updates}


@router.post("/{patient_id}/kit/ota")
async def trigger_ota(
    patient_id: str,
    version: str,
    db: AsyncSession = Depends(get_db_session)
):
    """Enviar actualización de firmware al kit del paciente."""
    patient = await db.get(Patient, patient_id)
    if not patient or not patient.kit:
        raise HTTPException(404)

    firmware_url = f"https://firmware.lumajira.com/releases/{version}/kit.bin"
    mqtt_bridge.send_ota(patient.kit.id, firmware_url, version)

    return {"ok": True, "kit": patient.kit.id, "version": version}


@router.get("/{patient_id}/ai-summary")
async def ai_summary(patient_id: str):
    """Generar resumen semanal con OpenAI."""
    summary = await generate_weekly_summary(patient_id)
    return {"summary": summary}
