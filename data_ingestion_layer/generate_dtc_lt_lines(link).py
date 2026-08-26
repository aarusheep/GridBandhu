import json

def get_dtc_lt_mappings():
    # Junction array mapping DTC-KN-0101 to outgoing Low Tension lines
    return [
        {"dtc_id": "DTC-KN-0101", "lt_line_id": "LT-KN-0101-A"},
        {"dtc_id": "DTC-KN-0101", "lt_line_id": "LT-KN-0101-B"},
        {"dtc_id": "DTC-KN-0101", "lt_line_id": "LT-KN-0101-C"},
        {"dtc_id": "DTC-KN-0101", "lt_line_id": "LT-KN-0101-D"}
    ]

if __name__ == "__main__":
    mapping_data = get_dtc_lt_mappings()
    with open("dtc_lt_lines.json", "w") as f:
        json.dump(mapping_data, f, indent=4)
    print("Exported dtc_lt_lines.json successfully.")