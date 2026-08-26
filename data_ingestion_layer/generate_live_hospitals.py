import json
import time
import random
from datetime import datetime, timezone

# Static facility configurations
hospitals_base = [
    {
        "facility_id": "HOSP-MULUND-001",
        "facility_type": "Hospital",
        "name": "Manisha Universal Multispeciality Hospital, Mulund",
        "location": {"latitude": 19.18622, "longitude": 72.947019},
        "backup_power_kw": 150.0,
        "connected_lt_line_id": "LT-MULUND-W-101",
        "connected_dtc_id": "DTC-MULUND-45401",
        "criticality_tier": "High",
        "base_load_kw": 110.0
    },
    {
        "facility_id": "HOSP-MULUND-002",
        "facility_type": "Hospital",
        "name": "Modi Hospital Maternity & Surgical Nursing Home",
        "location": {"latitude": 19.188909, "longitude": 72.945879},
        "backup_power_kw": 75.0,
        "connected_lt_line_id": "LT-MULUND-W-102",
        "connected_dtc_id": "DTC-MULUND-45402",
        "criticality_tier": "High",
        "base_load_kw": 55.0
    },
    {
        "facility_id": "HOSP-MULUND-003",
        "facility_type": "Hospital",
        "name": "Dr. Thakur's Shree Hospital",
        "location": {"latitude": 19.189527, "longitude": 72.945694},
        "backup_power_kw": 50.0,
        "connected_lt_line_id": "LT-MULUND-W-103",
        "connected_dtc_id": "DTC-MULUND-45403",
        "criticality_tier": "Medium",
        "base_load_kw": 35.0
    }
]

def generate_telemetry_step():
    telemetry_data = []
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    for hosp in hospitals_base:
        # Simulate dynamic 1-minute load variation (Gaussian fluctuations)
        load_jitter = random.uniform(-0.08, 0.08)
        current_load = round(hosp["base_load_kw"] * (1 + load_jitter), 2)
        
        # 1-minute average utilized power based on instantaneous load
        avg_utilized = round(current_load * random.uniform(0.97, 1.03), 2)
        
        # Operational grid status (95% Operational, 5% Diesel Generator Backup Active)
        status = "OPERATIONAL_GRID" if random.random() > 0.05 else "GRID_FAULT_BACKUP_ACTIVE"
        
        record = {
            "facility_id": hosp["facility_id"],
            "facility_type": hosp["facility_type"],
            "name": hosp["name"],
            "location": hosp["location"],
            "backup_power_kw": hosp["backup_power_kw"],
            "connected_lt_line_id": hosp["connected_lt_line_id"],
            "connected_dtc_id": hosp["connected_dtc_id"],
            "criticality_tier": hosp["criticality_tier"],
            "current_load_kw": current_load,
            "avg_utilised_power_1min": avg_utilized,
            "status": status,
            "last_updated": current_time
        }
        telemetry_data.append(record)
        
    return telemetry_data

# Continuous 1-minute updating loop
if __name__ == "__main__":
    print("Starting 1-minute real-time JSON telemetry engine...")
    OUTPUT_FILE = "hospitals_live_telemetry.json"
    
    while True:
        data = generate_telemetry_step()
        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=4)
            
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Updated {OUTPUT_FILE} with fresh 1-minute telemetry.")
        time.sleep(60)