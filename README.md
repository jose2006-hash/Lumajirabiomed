# LUMAJIRA — Plataforma Biónica

## Stack

| Capa | Tecnología | Por qué |
|---|---|---|
| Frontend | HTML/JS (este repo) → React (v2) | Rápido de iterar |
| Backend | FastAPI (Python) | Nativo con OpenAI SDK y ML |
| DB principal | PostgreSQL | Pacientes, clínicas, historial |
| DB telemetría | TimescaleDB | Series de tiempo del hardware (EMG) |
| Broker IoT | MQTT (Mosquitto) | Protocolo nativo del ESP32 |
| Tiempo real | Redis + WebSocket | Dashboard live sin polling |
| IA | OpenAI GPT-4o | Análisis clínico y alertas |
| Auth | JWT + bcrypt | Simple y seguro |

## Estructura

```
lumajira/
├── frontend/
│   ├── dashboard/          ← Lo que ve el protesista (PC)
│   └── patient-app/        ← Lo que ve el paciente (móvil)
├── backend/
│   ├── api/
│   │   ├── routes/         ← Endpoints REST
│   │   ├── models/         ← Schemas Pydantic
│   │   └── services/       ← Lógica de negocio
│   ├── core/               ← Config, auth, seguridad
│   ├── hardware/           ← Bridge MQTT ↔ API
│   ├── ai/                 ← Integración OpenAI
│   └── db/                 ← Modelos SQLAlchemy + migraciones
├── hardware/
│   └── firmware/           ← Código ESP32 (referencia)
└── docs/                   ← Diagramas y esquemas
```

## Levantar en local

```bash
# 1. Clonar y entrar
git clone https://github.com/tu-org/lumajira
cd lumajira

# 2. Backend
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # llenar OPENAI_API_KEY, DB_URL, etc.
alembic upgrade head   # crear tablas
uvicorn main:app --reload

# 3. MQTT broker (Docker)
docker run -d -p 1883:1883 -p 9001:9001 eclipse-mosquitto

# 4. Redis
docker run -d -p 6379:6379 redis:alpine

# 5. Frontend
# Abrir frontend/dashboard/index.html en navegador
# O servir: python -m http.server 3000
```
