/**
 * ws_client.js
 * Conecta el dashboard al WebSocket del backend para datos en tiempo real.
 * Importar en index.html: <script src="ws_client.js"></script>
 */

class LumajiraWS {
    constructor(kitId) {
        this.kitId = kitId;
        this.ws = null;
        this.onEMG = null;
        this.onGesture = null;
        this.onBattery = null;
        this.onStatus = null;
        this.connect();
    }

    connect() {
        const url = `ws://localhost:8000/ws/kit/${this.kitId}`;
        this.ws = new WebSocket(url);

        this.ws.onopen = () => {
            console.log(`WS conectado: kit ${this.kitId}`);
        };

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            switch (data.type) {
                case "emg":     if (this.onEMG)     this.onEMG(data);     break;
                case "gesture": if (this.onGesture) this.onGesture(data); break;
                case "battery": if (this.onBattery) this.onBattery(data); break;
                case "status":  if (this.onStatus)  this.onStatus(data);  break;
            }
        };

        this.ws.onclose = () => {
            console.log("WS desconectado. Reconectando en 3s...");
            setTimeout(() => this.connect(), 3000);
        };
    }

    disconnect() {
        if (this.ws) this.ws.close();
    }
}

// Uso:
// const kit = new LumajiraWS("LJ-0042");
// kit.onGesture = (d) => console.log(`Gesto: ${d.gesture} (${d.confidence*100}%)`);
// kit.onBattery = (d) => updateBatteryUI(d.pct);
