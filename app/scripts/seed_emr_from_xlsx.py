# FILE: app/scripts/seed_emr_from_xlsx.py
from __future__ import annotations

import argparse
import json
import re

import pandas as pd
from sqlalchemy.orm import Session
from app.db.session import create_tenant_session

from app.api.deps import get_db  # if you have session factory, adjust
from app.models.emr_all import EmrDepartment, EmrRecordType, EmrTemplate, EmrTemplateVersion, EmrTemplateStatus


DEFAULT_TYPES = [
    ("CASE_SHEET", "Case Sheet", "Clinical", 10),
    ("OPD_NOTE", "OPD Consultation", "Clinical", 20),
    ("PROGRESS_NOTE", "Progress Note", "Clinical", 30),
    ("DISCHARGE_SUMMARY", "Discharge Summary", "Docs", 40),
    ("CONSENT", "Consent", "Legal", 50),
    ("LAB_RESULT", "Lab Result", "Diagnostics", 60),
    ("RADIOLOGY_REPORT", "Radiology Report", "Diagnostics", 70),
    ("PRESCRIPTION", "Prescription", "Pharmacy", 80),
]

def norm_code(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    # keep letters/numbers/spaces, convert to underscores then upper
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace(" ", "_").upper()

def split_sections(s: str):
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[,\n;•]+", str(s)) if p.strip()]
    # remove duplicates preserving order
    out = []
    seen = set()
    for p in parts:
        k = p.lower()
        if k not in seen:
            out.append(p)
            seen.add(k)
    return out[:60]

def seed(db: Session, xlsx_path: str):
    df = pd.read_excel(xlsx_path)
    df.columns = ["department", "template_name", "area", "who_fills", "mandatory_sections"]
    df = df.iloc[1:].reset_index(drop=True)

    # remove heading rows
    def ok_dept(x):
        if x is None:
            return False
        s = str(x).strip()
        if not s:
            return False
        if s.lower() in ("department",):
            return False
        if "case sheets" in s.lower():
            return False
        return True

    df = df[df["department"].apply(ok_dept)].copy()

    # seed record types
    for code, label, cat, order in DEFAULT_TYPES:
        ex = db.query(EmrRecordType).filter(EmrRecordType.code == code).one_or_none()
        if not ex:
            db.add(EmrRecordType(code=code, label=label, category=cat, is_active=True, display_order=order))
    db.flush()

    # seed departments from excel
    deps = sorted(set([str(x).strip() for x in df["department"].dropna().unique()]))
    for i, name in enumerate(deps, start=1):
        code = norm_code(name)
        ex = db.query(EmrDepartment).filter(EmrDepartment.code == code).one_or_none()
        if not ex:
            db.add(EmrDepartment(code=code, name=name, is_active=True, display_order=100 + i))
    db.flush()

    # seed templates (dept + CASE_SHEET + template_name)
    created = 0
    for _, row in df.iterrows():
        dept_name = str(row["department"]).strip()
        dept_code = norm_code(dept_name)
        tpl_name = str(row["template_name"]).strip()
        if not tpl_name or tpl_name.lower() == "template name":
            continue

        # base: CASE_SHEET (you can later reclassify)
        record_type_code = "CASE_SHEET"

        # unique constraint: dept_code + record_type_code + name
        ex = db.query(EmrTemplate).filter(
            EmrTemplate.dept_code == dept_code,
            EmrTemplate.record_type_code == record_type_code,
            EmrTemplate.name == tpl_name,
        ).one_or_none()

        if ex:
            continue

        sections = split_sections(row.get("mandatory_sections", ""))

        t = EmrTemplate(
            dept_code=dept_code,
            record_type_code=record_type_code,
            name=tpl_name,
            description=f"Area: {row.get('area','')} | Who: {row.get('who_fills','')}".strip(),
            restricted=False,
            premium=False,
            is_default=False,
            status=EmrTemplateStatus.DRAFT,
        )
        db.add(t)
        db.flush()

        v = EmrTemplateVersion(
            template_id=int(t.id),
            version_no=1,
            changelog="Seed from XLSX",
            sections_json=json.dumps(sections, ensure_ascii=False),
            schema_json=json.dumps({"blocks": []}, ensure_ascii=False),
        )
        db.add(v)
        db.flush()

        t.active_version_id = int(v.id)
        created += 1

    db.commit()
    print(f"Seed completed. New templates created: {created}")
    print(f"Departments loaded: {len(deps)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-uri", dest="db_uri", default=None, help="Tenant DB URI (mysql+pymysql://...)")
    args = ap.parse_args()

    # You might have a custom session maker; adjust this part.
    # If get_db is generator, handle accordingly:
    db = None
    gen = None

    if args.db_uri:
        db = create_tenant_session(args.db_uri)
    else:
        # CLI-safe: override fastapi Header defaults
        gen = get_db(authorization=None, token=None)
        db = next(gen)
    try:
        seed(db, args.xlsx)
    finally:
        try:
            if gen:
                gen.close()
        except Exception:
            pass
        try:
            if db:
                db.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
