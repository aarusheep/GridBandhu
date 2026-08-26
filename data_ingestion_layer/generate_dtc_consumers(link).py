import json

def get_dtc_consumer_mappings():
    return [
        {
            "dtc_id": "DTC-KN-0101",
            "consumer_id": "FAC-KN-HOSP-01",
            "consumer_type": "Critical Infrastructure (Hospital)"
        },
        {
            "dtc_id": "DTC-KN-0101",
            "consumer_id": "CONS-KN-RES-1042",
            "consumer_type": "Residential"
        },
        {
            "dtc_id": "DTC-KN-0101",
            "consumer_id": "CONS-KN-RES-1043",
            "consumer_type": "Residential"
        },
        {
            "dtc_id": "DTC-KN-0101",
            "consumer_id": "CONS-KN-COM-0215",
            "consumer_type": "Commercial"
        },
        {
            "dtc_id": "DTC-KN-0101",
            "consumer_id": "CONS-KN-COM-0216",
            "consumer_type": "Commercial"
        }
    ]

if __name__ == "__main__":
    consumer_mapping = get_dtc_consumer_mappings()
    with open("dtc_consumers.json", "w") as f:
        json.dump(consumer_mapping, f, indent=4)
    print("Exported dtc_consumers.json successfully.")