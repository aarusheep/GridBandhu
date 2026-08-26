import json
import time
import random
from datetime import datetime, timezone

# Static configuration matching image schema and geographical coordinates
dtc_info = {
    "dtc_id": "DTC-KN-0101",
    "location": {
        "latitude": 21.5782596,
        "longitude": 74.5798877
    },
    "connected_ht_line_id": "HT-KN-F01",
    "capacity_kw": 250.0  # Distribution Transformer rated capacity (250 kW)
}

def generate_dtc_snapshot():
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Dynamically generate load and percentage metrics
    loading_percentage = round(random.uniform(35.0, 92.0), 2)
    current_load_kw = round((dtc_info["capacity_kw"] * loading_percentage) / 100.0, 2)
    
    # Operational status evaluation
    if loading_percentage >= 90.0:
        status = "CRITICAL_OVERLOAD"
    elif loading_percentage >= 75.0:
        status = "HIGH_LOAD"
    elif random.random() < 0.02:
        status = "FUSE_TRIPPED"
    else:
        status = "OPERATIONAL_NORMAL"

    payload = {
        "dtc_id": dtc_info["dtc_id"],
        "location": dtc_info["location"],
        "connected_ht_line_id": dtc_info["connected_ht_line_id"],
        "capacity_kw": dtc_info["capacity_kw"],
        "current_load_kw": current_load_kw,
        "loading_percentage": loading_percentage,
        "status": status,
        "last_updated": current_time
    }
    
    return [payload]

if __name__ == "__main__":
    print("Starting DTC 1-Minute Live Telemetry Stream...")
    OUTPUT_FILE = "dtc_live_telemetry.json"
    
    while True:
        data = generate_dtc_snapshot()
        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=4)
            
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Updated {OUTPUT_FILE} with 1-minute DTC telemetry.")
        time.sleep(60)