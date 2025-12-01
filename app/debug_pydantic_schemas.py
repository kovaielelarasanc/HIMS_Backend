# backend/app/debug_pydantic_schemas.py
from importlib import import_module

SCHEMA_MODULES = [
    "opd",
    "patient",
    "ipd",
    "billing",
    "pharmacy",
    "lis",
    "ris",
    "template",
    "credit",
]


def check_schema(mod_name: str):
    try:
        import_module(f"app.schemas.{mod_name}")
        print(f"[OK] app.schemas.{mod_name}")
    except RecursionError as e:
        print(f"[RECURSION] app.schemas.{mod_name}: {e}")
    except Exception as e:
        print(f"[ERROR] app.schemas.{mod_name}: {e}")


if __name__ == "__main__":
    for name in SCHEMA_MODULES:
        check_schema(name)
