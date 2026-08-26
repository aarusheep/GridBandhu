import json
import time
import random
from datetime import datetime, timezone

# Static HT Pole attributes matching the schema specification
ht_poles_static = [
    {
        "pole_id": "HT-POLE-KN-001",
        "location": {"latitude": 19.188950, "longitude": 72.942700},
        "connected_ht_line_from": "HT-KN-F01",
        "connected_ht_line_to": "HT-KN-F02",
        "base_flow_kw": 1250.0
    },
    {
        "pole_id": "HT-POLE-KN-002",
        "location": {"latitude": 19.188820, "longitude": 72.943100},
        "connected_ht_line_from": "HT-KN-F02",
        "connected_ht_line_to": "HT-KN-F03",
        "base_flow_kw": 1180.0
    },
    {
        "pole_id": "HT-POLE-KN-003",
        "location": {"latitude": 19.188700, "longitude": 72.943500},
        "connected_ht_line_from": "HT-KN-F03",
        "connected_ht_line_to": "HT-KN-F04",
        "base_flow_kw": 950.0
    },
    {
        "pole_id": "HT-POLE-KN-004",
        "location": {"latitude": 19.188550, "longitude": 72.943900},
        "connected_ht_line_from": "HT-KN-F04",
        "connected_ht_line_to": "HT-KN-F05",
        "base_flow_kw": 1100.0
    },
    {
        "pole_id": "HT-POLE-KN-005",
        "location": {"latitude": 19.188400, "longitude": 72.944300},
        "connected_ht_line_from": "HT-KN-F05",
        "connected_ht_line_to": "HT-KN-F06",
        "base_flow_kw": 880.0
    }
]

def generate_poles_telemetry_step():
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    telemetry_list = []
    
    for pole in ht_poles_static:
        # Simulate dynamic 1-minute power flow fluctuations
        flow_jitter = random.uniform(-0.06, 0.06)
        current_flow = round(pole["base_flow_kw"] * (1 + flow_jitter), 2)
        
        # Determine operational status (97% Normal, 3% Isolated Fault)
        status = "ENERGIZED_NORMAL" if random.random() > 0.03 else "FAULT_ISOLATED"
        if status == "FAULT_ISOLATED":
            current_flow = 0.0
            
        record = {
            "pole_id": pole["pole_id"],
            "location": pole["location"],
            "connected_ht_line_from": pole["connected_ht_line_from"],
            "connected_ht_line_to": pole["connected_ht_line_to"],
            "current_flow_kw": current_flow,
            "status": status,
            "last_updated": current_time
        }
        telemetry_list.append(record)
        
    return telemetry_list

if __name__ == "__main__":
    print("Starting HT Poles 1-Minute Live Telemetry Stream...")
    OUTPUT_FILE = "ht_poles_live_telemetry.json"
    
    while True:
        data = generate_poles_telemetry_step()
        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=4)
            
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Updated {OUTPUT_FILE} with fresh 1-minute HT pole metrics.")
        time.sleep(60)