/**
 * hardware/firmware/lumajira_kit.ino
 * 
 * Código de referencia para el ESP32-S3 del Kit Biónico Lumajira.
 * 
 * FLUJO:
 *   1. Leer sensores EMG (cada 50ms)
 *   2. Clasificar gesto con TFLite Micro
 *   3. Actuar motores
 *   4. Publicar datos en MQTT
 *   5. Escuchar comandos del servidor (parámetros, OTA)
 */

#include <WiFi.h>
#include <PubSubClient.h>       // MQTT
#include <ArduinoJson.h>
#include <TensorFlowLite_ESP32.h>

// ─── CONFIGURACIÓN ──────────────────────────────────────────────
const char* WIFI_SSID     = "TU_WIFI";
const char* WIFI_PASSWORD = "TU_PASSWORD";
const char* MQTT_SERVER   = "api.lumajira.com";   // o IP local en desarrollo
const int   MQTT_PORT     = 1883;
const char* MQTT_USER     = "kit_user";
const char* MQTT_PASS     = "kit_secret";

// ID único del kit — grabado en flash en fábrica
const char* KIT_ID = "LJ-0042";

// Topics MQTT
char TOPIC_EMG[50];       // lumajira/kit/LJ-0042/emg
char TOPIC_GESTURE[50];   // lumajira/kit/LJ-0042/gesture
char TOPIC_BATTERY[50];   // lumajira/kit/LJ-0042/battery
char TOPIC_STATUS[50];    // lumajira/kit/LJ-0042/status
char TOPIC_CMD_PARAMS[50]; // lumajira/kit/LJ-0042/commands/params
char TOPIC_CMD_OTA[50];    // lumajira/kit/LJ-0042/commands/ota

// ─── PINES ──────────────────────────────────────────────────────
const int EMG_PIN_CH1 = 34;   // Canal 1 — flexor (ADC1)
const int EMG_PIN_CH2 = 35;   // Canal 2 — extensor (ADC1)
const int EMG_PIN_CH3 = 36;   // Canal 3 — referencia (ADC1)
const int LED_PIN     = 2;    // LED de estado

// ─── PARÁMETROS (ajustables remotamente vía MQTT) ───────────────
float emg_sensitivity     = 7.0;
float activation_threshold = 4.0;
float motor_speed         = 6.0;
float channel2_gain       = 5.0;

// ─── VARIABLES ──────────────────────────────────────────────────
WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);

unsigned long lastEMGRead    = 0;
unsigned long lastBattery    = 0;
const int EMG_INTERVAL       = 50;    // ms — 20 lecturas/seg
const int BATTERY_INTERVAL   = 300000; // 5 minutos

String lastGesture = "";
float  lastConfidence = 0.0;


// ════════════════════════════════════════════════════════════════
// SETUP
// ════════════════════════════════════════════════════════════════

void setup() {
    Serial.begin(115200);
    
    // Construir topics con el KIT_ID
    snprintf(TOPIC_EMG,       50, "lumajira/kit/%s/emg",            KIT_ID);
    snprintf(TOPIC_GESTURE,   50, "lumajira/kit/%s/gesture",        KIT_ID);
    snprintf(TOPIC_BATTERY,   50, "lumajira/kit/%s/battery",        KIT_ID);
    snprintf(TOPIC_STATUS,    50, "lumajira/kit/%s/status",         KIT_ID);
    snprintf(TOPIC_CMD_PARAMS,50, "lumajira/kit/%s/commands/params",KIT_ID);
    snprintf(TOPIC_CMD_OTA,   50, "lumajira/kit/%s/commands/ota",   KIT_ID);
    
    pinMode(LED_PIN, OUTPUT);
    connectWiFi();
    
    mqtt.setServer(MQTT_SERVER, MQTT_PORT);
    mqtt.setCallback(onMQTTMessage);
    connectMQTT();
    
    // TODO: initTFLite() — cargar modelo desde SPIFFS
    
    Serial.println("Kit Lumajira listo ✓");
}


// ════════════════════════════════════════════════════════════════
// LOOP PRINCIPAL
// ════════════════════════════════════════════════════════════════

void loop() {
    if (!mqtt.connected()) connectMQTT();
    mqtt.loop();
    
    unsigned long now = millis();
    
    // Leer EMG cada 50ms
    if (now - lastEMGRead >= EMG_INTERVAL) {
        lastEMGRead = now;
        readAndPublishEMG();
    }
    
    // Publicar batería cada 5 minutos
    if (now - lastBattery >= BATTERY_INTERVAL) {
        lastBattery = now;
        publishBattery();
    }
}


// ════════════════════════════════════════════════════════════════
// EMG + CLASIFICACIÓN
// ════════════════════════════════════════════════════════════════

void readAndPublishEMG() {
    // Leer ADC
    float ch1 = analogRead(EMG_PIN_CH1) / 4095.0 * emg_sensitivity;
    float ch2 = analogRead(EMG_PIN_CH2) / 4095.0 * channel2_gain;
    float ch3 = analogRead(EMG_PIN_CH3) / 4095.0;  // referencia
    
    // Publicar señal cruda en MQTT
    StaticJsonDocument<100> doc;
    doc["ts"]  = millis();
    doc["ch1"] = round(ch1 * 100) / 100.0;
    doc["ch2"] = round(ch2 * 100) / 100.0;
    doc["ch3"] = round(ch3 * 100) / 100.0;
    
    char payload[100];
    serializeJson(doc, payload);
    mqtt.publish(TOPIC_EMG, payload);
    
    // Clasificar gesto si la señal supera el umbral
    if (ch1 > activation_threshold || ch2 > activation_threshold) {
        classifyGesture(ch1, ch2, ch3);
    }
}

void classifyGesture(float ch1, float ch2, float ch3) {
    // TODO: invocar modelo TFLite
    // Por ahora: lógica simple de ejemplo
    
    String gesture = "";
    float confidence = 0.0;
    unsigned long t0 = millis();
    
    if (ch1 > ch2 * 1.5) {
        gesture = "pinza";
        confidence = ch1 / emg_sensitivity;
    } else if (ch2 > ch1 * 1.5) {
        gesture = "extension";
        confidence = ch2 / channel2_gain;
    } else if (ch1 > activation_threshold && ch2 > activation_threshold) {
        gesture = "agarre";
        confidence = (ch1 + ch2) / (emg_sensitivity + channel2_gain);
    }
    
    if (gesture == "") return;
    
    // Evitar publicar el mismo gesto repetido (debounce)
    if (gesture == lastGesture && confidence < 0.05) return;
    lastGesture = gesture;
    lastConfidence = confidence;
    
    unsigned long latency = millis() - t0;
    
    // Publicar evento de gesto
    StaticJsonDocument<150> doc;
    doc["ts"]         = millis();
    doc["gesture"]    = gesture;
    doc["confidence"] = round(confidence * 100) / 100.0;
    doc["latency_ms"] = latency;
    
    char payload[150];
    serializeJson(doc, payload);
    mqtt.publish(TOPIC_GESTURE, payload, false);
    
    // Actuar motores
    actuateMotors(gesture);
}


// ════════════════════════════════════════════════════════════════
// RECIBIR COMANDOS DEL SERVIDOR
// ════════════════════════════════════════════════════════════════

void onMQTTMessage(char* topic, byte* payload, unsigned int length) {
    String topicStr = String(topic);
    
    // Parsear JSON del payload
    StaticJsonDocument<300> doc;
    DeserializationError err = deserializeJson(doc, payload, length);
    if (err) return;
    
    // ── Actualizar parámetros ─────────────────────────────────
    if (topicStr == String(TOPIC_CMD_PARAMS)) {
        if (doc.containsKey("emg_sensitivity"))     emg_sensitivity     = doc["emg_sensitivity"];
        if (doc.containsKey("activation_threshold")) activation_threshold = doc["activation_threshold"];
        if (doc.containsKey("motor_speed"))          motor_speed         = doc["motor_speed"];
        if (doc.containsKey("channel2_gain"))        channel2_gain       = doc["channel2_gain"];
        
        Serial.println("Parámetros actualizados desde servidor:");
        Serial.printf("  EMG sensitivity: %.1f\n", emg_sensitivity);
        Serial.printf("  Channel 2 gain:  %.1f\n", channel2_gain);
        
        // Guardar en NVS (persiste en reinicios)
        // preferences.putFloat("emg_sens", emg_sensitivity);
    }
    
    // ── Actualización OTA ─────────────────────────────────────
    if (topicStr == String(TOPIC_CMD_OTA)) {
        String url     = doc["url"];
        String version = doc["version"];
        Serial.printf("OTA recibida: v%s desde %s\n", version.c_str(), url.c_str());
        
        // TODO: esp_https_ota() para descargar e instalar
        // Por ahora solo confirmar recepción
        publishOTAAck(version);
    }
}

void publishOTAAck(String version) {
    char topic[60];
    snprintf(topic, 60, "lumajira/kit/%s/ota/ack", KIT_ID);
    
    StaticJsonDocument<100> doc;
    doc["version"] = version;
    doc["success"] = true;
    
    char payload[100];
    serializeJson(doc, payload);
    mqtt.publish(topic, payload);
}


// ════════════════════════════════════════════════════════════════
// HELPERS
// ════════════════════════════════════════════════════════════════

void publishBattery() {
    // Leer voltaje del pin ADC de batería
    float voltage = analogRead(35) / 4095.0 * 4.2;
    int   pct     = map(voltage * 100, 300, 420, 0, 100);
    pct = constrain(pct, 0, 100);
    
    StaticJsonDocument<100> doc;
    doc["ts"]      = millis();
    doc["pct"]     = pct;
    doc["voltage"] = round(voltage * 100) / 100.0;
    
    char payload[100];
    serializeJson(doc, payload);
    mqtt.publish(TOPIC_BATTERY, payload);
}

void actuateMotors(String gesture) {
    // TODO: controlar PCA9685 → servos según el gesto
    int speed = (int)(motor_speed / 10.0 * 180);
    Serial.printf("Motor → %s (speed %d)\n", gesture.c_str(), speed);
}

void connectWiFi() {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("Conectando WiFi");
    while (WiFi.status() != WL_CONNECTED) {
        delay(500); Serial.print(".");
    }
    Serial.printf("\nWiFi OK: %s\n", WiFi.localIP().toString().c_str());
}

void connectMQTT() {
    while (!mqtt.connected()) {
        String clientId = "lumajira-kit-" + String(KIT_ID);
        
        if (mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS,
                         TOPIC_STATUS, 0, true, "{\"status\":\"offline\"}")) {
            
            // Publicar estado online
            mqtt.publish(TOPIC_STATUS, "{\"status\":\"online\"}", true);
            
            // Suscribirse a comandos
            mqtt.subscribe(TOPIC_CMD_PARAMS);
            mqtt.subscribe(TOPIC_CMD_OTA);
            
            Serial.println("MQTT conectado ✓");
            digitalWrite(LED_PIN, HIGH);
        } else {
            Serial.printf("MQTT error: %d. Reintentando...\n", mqtt.state());
            delay(3000);
        }
    }
}
