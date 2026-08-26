from state.schema import get_all_feeders, get_total_supply
from milp.solver import solve

feeders = get_all_feeders()
supply = get_total_supply()
print(f"Feeders: {len(feeders)}, Supply: {supply} MW\n")

result = solve(feeders, supply)
print(f"Status: {result['status']} | Solved in {result['solve_time_ms']}ms\n")

for a in result["allocations"]:
    print(f"  {a['feeder_id']} {a['load_type']}: {a['served_mw']} MW ({a['fraction']:.0%}) {a['status']}")