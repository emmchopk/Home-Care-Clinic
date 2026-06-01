from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeSerializer

SECRET_KEY = "clinic-secret-key-v1"
SESSION_KEY = "admin_session"
serializer = URLSafeSerializer(SECRET_KEY, salt="admin-auth")


def set_admin_session(response, admin_id: int, username: str):
    token = serializer.dumps({"admin_id": admin_id, "username": username})
    response.set_cookie(
        key=SESSION_KEY,
        value=token,
        httponly=True,
        samesite="lax"
    )


def clear_admin_session(response):
    response.delete_cookie(SESSION_KEY)


def get_current_admin(request: Request):
    token = request.cookies.get(SESSION_KEY)
    if not token:
        return None
    try:
        return serializer.loads(token)
    except Exception:
        return None


def require_admin(request: Request):
    admin = get_current_admin(request)
    if not admin:
        return RedirectResponse(url="/admin/login", status_code=303)
    return admin