from fastapi import APIRouter

from backend.state.schema import get_active_faults, get_paths

router = APIRouter()


@router.get("/allocation/paths/{fault_id:path}")
def get_fault_paths(fault_id: str):
    """Get top 3 reconfiguration paths for a specific fault."""
    paths = get_paths(fault_id)
    if not paths:
        return {"paths": [], "message": "No paths computed for this fault"}
    return {"fault_id": fault_id, "paths": paths}


@router.get("/allocation/paths")
def get_all_paths():
    """Get paths for all active faults."""
    faults = get_active_faults()
    result = {}
    for fid in faults:
        result[fid] = get_paths(fid)
    return {"faults": result, "count": len(faults)}
