# app/core/config.py
import os
from typing import List
from pydantic import BaseModel
from dotenv import load_dotenv
from urllib.parse import quote_plus
from pathlib import Path

load_dotenv()


def _split_csv(value: str) -> List[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]

MEDIA_URL: str = "/media"

class Settings(BaseModel):
    PROJECT_NAME: str = os.getenv("PROJECT_NAME", "NABH HIMS & EMR")
    PROVIDER_TENANT_CODE: str = "NUTRYAH" 
    API_V1_STR: str = os.getenv("API_V1_STR", "/api")
    SITE_URL: str = os.getenv("SITE_URL", "http://127.0.0.1:8000")

    # CORS (env takes priority)
    BACKEND_CORS_ORIGINS: List[str] = _split_csv(
        os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ))

    # ---------- MySQL (shared creds) ----------
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "nutryah_user")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "Nutryah@123")
    # This now defaults to MASTER DB name
    MYSQL_DB: str = os.getenv("MYSQL_DB", "nabh_hims_emr")
    DB_DRIVER: str = os.getenv("DB_DRIVER", "pymysql")

    # Central master DB (Tenant Management)
    MASTER_MYSQL_DB: str = os.getenv("MASTER_MYSQL_DB", MYSQL_DB)

    MASTER_SQLALCHEMY_DATABASE_URI: str = (
        f"mysql+{DB_DRIVER}://{quote_plus(MYSQL_USER)}:{quote_plus(MYSQL_PASSWORD)}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{MASTER_MYSQL_DB}")

    # Backward compatible alias (old code used this)
    SQLALCHEMY_DATABASE_URI: str = MASTER_SQLALCHEMY_DATABASE_URI

    # Tenant DB naming
    TENANT_DB_NAME_PREFIX: str = os.getenv("TENANT_DB_NAME_PREFIX",
                                           "nabh_hims_")

    def make_tenant_db_uri(self, db_name: str) -> str:
        return (
            f"mysql+{self.DB_DRIVER}://{quote_plus(self.MYSQL_USER)}:{quote_plus(self.MYSQL_PASSWORD)}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{db_name}")

    # ---------- Security ----------
    JWT_SECRET: str = os.getenv("JWT_SECRET", "change-this")
    JWT_ALG: str = os.getenv("JWT_ALG", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
        os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "2440"))
    REFRESH_TOKEN_EXPIRE_MINUTES: int = int(
        os.getenv("REFRESH_TOKEN_EXPIRE_MINUTES", str(7 * 24 * 60)))
    BCRYPT_ROUNDS: int = int(os.getenv("BCRYPT_ROUNDS", "12"))

    # ---------- Email (Office 365 defaults) ----------
    SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.office365.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "no-reply@nutryah.com")

    # ---------- Admin ----------
    ADMIN_ALL_ACCESS: bool = os.getenv(
        "ADMIN_ALL_ACCESS", "false").lower() in {"1", "true", "yes"}

    # ---------- File storage ----------
    STORAGE_DIR: str = os.getenv("STORAGE_DIR", "./media")
    MEDIA_URL: str = os.getenv("MEDIA_URL", "/files")

    # ---------- Billing flags ----------
    BILLING_AUTOCREATE: bool = os.getenv(
        "BILLING_AUTOCREATE", "false").lower() in {"1", "true", "yes"}
    BILLING_AUTOFINALIZE_OPD: bool = os.getenv(
        "BILLING_AUTOFINALIZE_OPD", "false").lower() in {"1", "true", "yes"}
    BILLING_DEFAULT_TAX: float = float(
        os.getenv("BILLING_DEFAULT_TAX", "0") or 0.0)


settings = Settings()

Path(settings.STORAGE_DIR).resolve().mkdir(parents=True, exist_ok=True)
