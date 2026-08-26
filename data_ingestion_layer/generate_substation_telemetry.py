import json
import time
import random
from datetime import datetime, timezone

# Static configuration for 22/11 Kisan Nagar Substation
substation_info = {
    "substation_id": "SUB-22-11-KN-01",
    "name": "22/11 KISAN NAGAR",
    "location": {
        "latitude": 19.1889874,
        "longitude": 72.9426074
    },
    "capacity_kw": 25000.0 # 25 MW Transformer Rated Capacity
}

def generate_substation_snapshot():
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Simulate fluctuating load demand (between 45% and 80% of capacity)
    load_factor = random.uniform(0.45, 0.80)
    current_load = round(substation_info["capacity_kw"] * load_factor, 2)
    
    # Determine operational status
    if current_load >= (substation_info["capacity_kw"] * 0.90):
        status = "OVERLOAD_WARNING"
    elif random.random() < 0.02:
        status = "FEEDS_TRIPPED"
    else:
        status = "OPERATIONAL_NORMAL"

    payload = {
        "substation_id": substation_info["substation_id"],
        "name": substation_info["name"],
        "location": substation_info["location"],
        "capacity_kw": substation_info["capacity_kw"],
        "current_load_kw": current_load,
        "status": status,
        "last_updated": current_time
    }
    
    return [payload]

if __name__ == "__main__":
    print("Starting Substation 1-Minute Live Telemetry Stream...")
    OUTPUT_FILE = "substation_live_telemetry.json"
    
    while True:
        data = generate_substation_snapshot()
        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=4)
            
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Updated {OUTPUT_FILE} with new 1-minute reading.")
        time.sleep(60)