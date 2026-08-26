"""Role-based permissions for API dependencies."""

from fastapi import Depends, HTTPException, status

from .auth import authenticate


ROLES = {
    "admin": {"view", "approve_recommendation", "reject_recommendation", "manage_users", "manage_system"},
    "operator": {"view", "approve_recommendation", "reject_recommendation"},
    "viewer": {"view"},
}


def require_permission(permission: str):
    def dependency(user: dict = Depends(authenticate)) -> dict:
        if permission not in ROLES.get(user.get("role"), set()):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission}")
        return user

    return dependency
