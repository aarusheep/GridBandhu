import json

def get_substation_ht_mappings():
    # Junction array with 6 HT line connections for SUB-22-11-KN-01
    return [
        {"substation_id": "SUB-22-11-KN-01", "ht_line_id": "HT-KN-F01"},
        {"substation_id": "SUB-22-11-KN-01", "ht_line_id": "HT-KN-F02"},
        {"substation_id": "SUB-22-11-KN-01", "ht_line_id": "HT-KN-F03"},
        {"substation_id": "SUB-22-11-KN-01", "ht_line_id": "HT-KN-F04"},
        {"substation_id": "SUB-22-11-KN-01", "ht_line_id": "HT-KN-F05"},
        {"substation_id": "SUB-22-11-KN-01", "ht_line_id": "HT-KN-F06"}
    ]

if __name__ == "__main__":
    ht_mapping_data = get_substation_ht_mappings()
    with open("substation_ht_lines.json", "w") as f:
        json.dump(ht_mapping_data, f, indent=4)
    print("Updated substation_ht_lines.json with 6 total HT connections.")