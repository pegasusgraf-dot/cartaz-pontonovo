from __future__ import annotations

import hmac
import os
import sys
import tempfile
from pathlib import Path

from flask import redirect, render_template, request, session, url_for

# Vercel's deployment bundle is read-only. The poster generator writes temporary
# uploads and PDFs, so redirect only its working directories to /tmp before
# importing the existing Flask application.
ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = Path(tempfile.gettempdir()) / "cartaz-vercel"
RUNTIME_DIRS = {"uploads", "cartazes_gerados", "temp_zip"}
_original_makedirs = os.makedirs


def _vercel_makedirs(path, mode=0o777, exist_ok=False, *, dir_fd=None):
    """Redirect generated files to /tmp and preserve os.makedirs' signature."""
    candidate = Path(path)
    if candidate.name in RUNTIME_DIRS and candidate.parent.resolve() == ROOT:
        candidate = RUNTIME_ROOT / candidate.name
    elif candidate.name == "fundos_prontos" and candidate.parent.resolve() == ROOT:
        # Preset backgrounds are deployed read-only; there is no need to create
        # or modify this directory in the serverless environment.
        return None
    if dir_fd is None:
        return _original_makedirs(candidate, mode=mode, exist_ok=True)
    return _original_makedirs(candidate, mode=mode, dir_fd=dir_fd, exist_ok=True)


os.makedirs = _vercel_makedirs
try:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # The Flask application file in this repository is app.py.
    import app as poster_app
finally:
    os.makedirs = _original_makedirs

# Keep all generated data outside the deployment bundle.
for directory in RUNTIME_DIRS:
    _original_makedirs(RUNTIME_ROOT / directory, exist_ok=True)
poster_app.PASTA_UPLOAD = str(RUNTIME_ROOT / "uploads")
poster_app.PASTA_SAIDA = str(RUNTIME_ROOT / "cartazes_gerados")
poster_app.PASTA_TEMP = str(RUNTIME_ROOT / "temp_zip")

app = poster_app.app
app.config.update(
    SECRET_KEY=os.getenv("CARTAZ_SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or "development-only-change-me",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


def _credentials():
    return os.getenv("CARTAZ_LOGIN", "").strip(), os.getenv("CARTAZ_PASSWORD", "")


def _configured():
    login, password = _credentials()
    secret = os.getenv("CARTAZ_SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
    return bool(login and password and secret)


@app.before_request
def require_login():
    if request.endpoint in {"cartaz_login", "cartaz_logout", "static"}:
        return None
    if not _configured():
        if request.path == "/":
            return render_template("login.html", error="Configure CARTAZ_LOGIN, CARTAZ_PASSWORD e CARTAZ_SECRET_KEY nas variáveis de ambiente da Vercel."), 503
        return "Configure as variáveis CARTAZ_LOGIN, CARTAZ_PASSWORD e CARTAZ_SECRET_KEY na Vercel.", 503
    if session.get("cartaz_authenticated"):
        return None
    if request.path == "/" and request.method == "GET":
        return redirect(url_for("cartaz_login"))
    return "Sessão expirada. Entre novamente para continuar.", 401


@app.route("/login", methods=["GET", "POST"], endpoint="cartaz_login")
def login():
    if session.get("cartaz_authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if not _configured():
            error = "O acesso ainda não foi configurado pelo administrador."
        else:
            expected_login, expected_password = _credentials()
            supplied_login = request.form.get("login", "")
            supplied_password = request.form.get("password", "")
            login_ok = hmac.compare_digest(supplied_login.encode(), expected_login.encode())
            password_ok = hmac.compare_digest(supplied_password.encode(), expected_password.encode())
            if login_ok and password_ok:
                session.clear()
                session.permanent = True
                session["cartaz_authenticated"] = True
                return redirect(url_for("index"))
            error = "Login ou senha incorretos. Confira seus dados e tente novamente."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"], endpoint="cartaz_logout")
def logout():
    session.clear()
    return redirect(url_for("cartaz_login"))
