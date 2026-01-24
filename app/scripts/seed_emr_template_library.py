from __future__ import annotations

import os
import json
import argparse
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker, Session

# ✅ your existing models
from app.models.emr_all import EmrSectionLibrary
from app.models.emr_template_library import EmrTemplateBlock


def norm_code(s: str) -> str:
    return (s or "").strip().upper().replace(" ", "_")


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _get_session(db_uri: str) -> Session:
    engine = create_engine(
        db_uri,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return SessionLocal()


def _ensure_tables_exist(db: Session) -> None:
    insp = inspect(db.get_bind())
    missing = []
    for t in ("emr_section_library", "emr_template_blocks"):
        if not insp.has_table(t):
            missing.append(t)
    if missing:
        raise RuntimeError(
            f"Missing tables: {missing}. Run migrations / create tables before seeding."
        )


# -----------------------------
# SEED DATA: SECTION LIBRARY
# -----------------------------
def seed_sections_global() -> List[Dict[str, Any]]:
    # group helps UI filter (SOAP / NURSING / DISCHARGE / OT / OBGYN / GENERAL)
    # display_order controls list sorting
    return [
        # --- SOAP / OPD ---
        {"code": "VITALS", "label": "Vitals", "group": "SOAP", "display_order": 10},
        {"code": "CHIEF_COMPLAINT", "label": "Chief Complaint", "group": "SOAP", "display_order": 20},
        {"code": "HPI", "label": "History of Present Illness", "group": "SOAP", "display_order": 30},
        {"code": "ROS", "label": "Review of Systems", "group": "SOAP", "display_order": 40},
        {"code": "PAST_HISTORY", "label": "Past Medical / Surgical History", "group": "SOAP", "display_order": 50},
        {"code": "FAMILY_HISTORY", "label": "Family History", "group": "SOAP", "display_order": 60},
        {"code": "SOCIAL_HISTORY", "label": "Social History", "group": "SOAP", "display_order": 70},
        {"code": "ALLERGIES", "label": "Allergies", "group": "SOAP", "display_order": 80},
        {"code": "MEDICATIONS", "label": "Current Medications", "group": "SOAP", "display_order": 90},
        {"code": "EXAM", "label": "Physical Examination", "group": "SOAP", "display_order": 100},
        {"code": "SYSTEMIC_EXAM", "label": "Systemic Examination", "group": "SOAP", "display_order": 110},
        {"code": "ASSESSMENT", "label": "Assessment / Diagnosis", "group": "SOAP", "display_order": 120},
        {"code": "PLAN", "label": "Plan", "group": "SOAP", "display_order": 130},
        {"code": "INVESTIGATIONS", "label": "Investigations", "group": "SOAP", "display_order": 140},
        {"code": "PROCEDURES", "label": "Procedures / Interventions", "group": "SOAP", "display_order": 150},
        {"code": "CONSENT", "label": "Consent", "group": "GENERAL", "display_order": 160},
        {"code": "CLINICAL_NOTES", "label": "Clinical Notes", "group": "GENERAL", "display_order": 170},

        # --- NURSING ---
        {"code": "NURSING_ASSESSMENT", "label": "Nursing Assessment", "group": "NURSING", "display_order": 200},
        {"code": "PAIN_ASSESSMENT", "label": "Pain Assessment", "group": "NURSING", "display_order": 210},
        {"code": "FALL_RISK", "label": "Fall Risk", "group": "NURSING", "display_order": 220},
        {"code": "BRADEN_SCALE", "label": "Braden Scale (Pressure Ulcer Risk)", "group": "NURSING", "display_order": 230},
        {"code": "INTAKE_OUTPUT", "label": "Intake / Output", "group": "NURSING", "display_order": 240},
        {"code": "MED_ADMIN", "label": "Medication Administration", "group": "NURSING", "display_order": 250},
        {"code": "SHIFT_NOTES", "label": "Shift Notes", "group": "NURSING", "display_order": 260},
        {"code": "CARE_PLAN", "label": "Care Plan", "group": "NURSING", "display_order": 270},

        # --- DISCHARGE ---
        {"code": "DISCHARGE_DIAGNOSIS", "label": "Discharge Diagnosis", "group": "DISCHARGE", "display_order": 300},
        {"code": "HOSPITAL_COURSE", "label": "Hospital Course", "group": "DISCHARGE", "display_order": 310},
        {"code": "DISCHARGE_PROCEDURES", "label": "Procedures / Surgeries", "group": "DISCHARGE", "display_order": 320},
        {"code": "DISCHARGE_MEDICATIONS", "label": "Discharge Medications", "group": "DISCHARGE", "display_order": 330},
        {"code": "DISCHARGE_INSTRUCTIONS", "label": "Discharge Instructions", "group": "DISCHARGE", "display_order": 340},
        {"code": "FOLLOW_UP", "label": "Follow Up", "group": "DISCHARGE", "display_order": 350},

        # --- OT / SURGERY ---
        {"code": "OT_PREOP_CHECKLIST", "label": "OT Pre-op Checklist", "group": "OT", "display_order": 400},
        {"code": "OT_INTRAOP_NOTES", "label": "OT Intra-op Notes", "group": "OT", "display_order": 410},
        {"code": "OT_POSTOP_ORDERS", "label": "OT Post-op Orders", "group": "OT", "display_order": 420},
        {"code": "ANESTHESIA_RECORD", "label": "Anesthesia Record", "group": "OT", "display_order": 430},

        # --- OBGYN / ANC ---
        {"code": "ANC_PROFILE", "label": "ANC Profile", "group": "OBGYN", "display_order": 500},
        {"code": "MENSTRUAL_HISTORY", "label": "Menstrual History", "group": "OBGYN", "display_order": 510},
        {"code": "OBSTETRIC_HISTORY", "label": "Obstetric History (GTPAL)", "group": "OBGYN", "display_order": 520},
        {"code": "GYNE_HISTORY", "label": "Gynecology History", "group": "OBGYN", "display_order": 530},
        {"code": "FETAL_ASSESSMENT", "label": "Fetal Assessment", "group": "OBGYN", "display_order": 540},
        {"code": "LABOUR_NOTE", "label": "Labour / Delivery Note", "group": "OBGYN", "display_order": 550},
        {"code": "NEWBORN_DETAILS", "label": "Newborn Details", "group": "OBGYN", "display_order": 560},
    ]


# -----------------------------
# SEED DATA: BLOCK LIBRARY
# -----------------------------
def seed_blocks_global() -> List[Dict[str, Any]]:
    # each block.schema_json supports {"items":[...]}
    # fields must match validator: type in allowed, select/radio must include options[]
    return [
        {
            "code": "BLOCK_VITALS",
            "label": "Vitals (BP/Pulse/Temp/SpO2)",
            "group": "SOAP",
            "display_order": 10,
            "schema": {
                "items": [
                    {"key": "bp", "type": "text", "label": "BP (mmHg)", "placeholder": "120/80"},
                    {"key": "pulse", "type": "number", "label": "Pulse (bpm)", "min": 0, "max": 300},
                    {"key": "temp", "type": "number", "label": "Temperature (°C)", "min": 25, "max": 45},
                    {"key": "rr", "type": "number", "label": "Respiratory Rate (/min)", "min": 0, "max": 80},
                    {"key": "spo2", "type": "number", "label": "SpO₂ (%)", "min": 0, "max": 100},
                    {"key": "weight", "type": "number", "label": "Weight (kg)", "min": 0, "max": 400},
                    {"key": "height", "type": "number", "label": "Height (cm)", "min": 0, "max": 250},
                ]
            },
        },
        {
            "code": "BLOCK_SOAP_MINI",
            "label": "SOAP Mini (Subjective/Objective/Assessment/Plan)",
            "group": "SOAP",
            "display_order": 20,
            "schema": {
                "items": [
                    {"key": "subjective", "type": "textarea", "label": "Subjective"},
                    {"key": "objective", "type": "textarea", "label": "Objective"},
                    {"key": "assessment", "type": "textarea", "label": "Assessment"},
                    {"key": "plan", "type": "textarea", "label": "Plan"},
                ]
            },
        },
        {
            "code": "BLOCK_ALLERGIES",
            "label": "Allergies",
            "group": "SOAP",
            "display_order": 30,
            "schema": {
                "items": [
                    {"key": "has_allergy", "type": "radio", "label": "Any allergy?", "options": ["No", "Yes"], "required": True},
                    {"key": "allergy_details", "type": "textarea", "label": "Allergy details (drug/food/others)", "placeholder": "e.g. Penicillin rash"},
                ]
            },
        },
        {
            "code": "BLOCK_MEDICATION_LIST",
            "label": "Medication List (Table)",
            "group": "SOAP",
            "display_order": 40,
            "schema": {
                "items": [
                    {
                        "key": "med_list",
                        "type": "table",
                        "label": "Current Medications",
                        "ui": {
                            "columns": [
                                {"key": "drug", "label": "Drug"},
                                {"key": "dose", "label": "Dose"},
                                {"key": "freq", "label": "Frequency"},
                                {"key": "route", "label": "Route"},
                                {"key": "duration", "label": "Duration"},
                            ]
                        },
                    }
                ]
            },
        },
        {
            "code": "BLOCK_DIAGNOSIS_LIST",
            "label": "Diagnosis List (Table)",
            "group": "SOAP",
            "display_order": 50,
            "schema": {
                "items": [
                    {
                        "key": "dx_list",
                        "type": "table",
                        "label": "Diagnoses",
                        "ui": {"columns": [{"key": "dx", "label": "Diagnosis"}, {"key": "notes", "label": "Notes"}]},
                    }
                ]
            },
        },
        {
            "code": "BLOCK_PROCEDURES_TABLE",
            "label": "Procedures / Interventions (Table)",
            "group": "SOAP",
            "display_order": 60,
            "schema": {
                "items": [
                    {
                        "key": "procedures",
                        "type": "table",
                        "label": "Procedures",
                        "ui": {"columns": [{"key": "procedure", "label": "Procedure"}, {"key": "date", "label": "Date"}, {"key": "remarks", "label": "Remarks"}]},
                    }
                ]
            },
        },

        # --- Nursing blocks ---
        {
            "code": "BLOCK_PAIN_SCALE",
            "label": "Pain Scale (0–10)",
            "group": "NURSING",
            "display_order": 110,
            "schema": {
                "items": [
                    {"key": "pain_score", "type": "number", "label": "Pain score (0–10)", "min": 0, "max": 10},
                    {"key": "pain_location", "type": "text", "label": "Location"},
                    {"key": "pain_character", "type": "select", "label": "Character", "options": ["Dull", "Sharp", "Burning", "Throbbing", "Cramping", "Other"]},
                ]
            },
        },
        {
            "code": "BLOCK_FALL_RISK_SIMPLE",
            "label": "Fall Risk (Simple)",
            "group": "NURSING",
            "display_order": 120,
            "schema": {
                "items": [
                    {"key": "fall_risk", "type": "select", "label": "Fall risk", "options": ["Low", "Moderate", "High"], "required": True},
                    {"key": "fall_precautions", "type": "textarea", "label": "Precautions"},
                ]
            },
        },
        {
            "code": "BLOCK_BRADEN_SCALE",
            "label": "Braden Scale (6–23)",
            "group": "NURSING",
            "display_order": 130,
            "schema": {
                "items": [
                    {"key": "braden_score", "type": "number", "label": "Braden score", "min": 6, "max": 23},
                    {"key": "skin_notes", "type": "textarea", "label": "Skin notes"},
                ]
            },
        },
        {
            "code": "BLOCK_INTAKE_OUTPUT",
            "label": "Intake / Output (Table)",
            "group": "NURSING",
            "display_order": 140,
            "schema": {
                "items": [
                    {
                        "key": "intake_output",
                        "type": "table",
                        "label": "Intake / Output",
                        "ui": {"columns": [{"key": "time", "label": "Time"}, {"key": "intake", "label": "Intake (ml)"}, {"key": "output", "label": "Output (ml)"}, {"key": "route", "label": "Route/Remarks"}]},
                    }
                ]
            },
        },
        {
            "code": "BLOCK_NURSING_ASSESSMENT_BASIC",
            "label": "Nursing Assessment (Basic)",
            "group": "NURSING",
            "display_order": 150,
            "schema": {
                "items": [
                    {"key": "mental_status", "type": "select", "label": "Mental status", "options": ["Alert", "Drowsy", "Confused", "Unresponsive"]},
                    {"key": "mobility", "type": "select", "label": "Mobility", "options": ["Independent", "Assisted", "Bedridden"]},
                    {"key": "nutrition", "type": "select", "label": "Nutrition", "options": ["Adequate", "Poor", "NPO"]},
                    {"key": "nursing_notes", "type": "textarea", "label": "Notes"},
                ]
            },
        },

        # --- Discharge blocks ---
        {
            "code": "BLOCK_DISCHARGE_MEDICATIONS",
            "label": "Discharge Medications (Table)",
            "group": "DISCHARGE",
            "display_order": 210,
            "schema": {
                "items": [
                    {
                        "key": "dc_meds",
                        "type": "table",
                        "label": "Discharge Medications",
                        "ui": {"columns": [{"key": "drug", "label": "Drug"}, {"key": "dose", "label": "Dose"}, {"key": "freq", "label": "Frequency"}, {"key": "duration", "label": "Duration"}, {"key": "instructions", "label": "Instructions"}]},
                    }
                ]
            },
        },
        {
            "code": "BLOCK_DISCHARGE_INSTRUCTIONS",
            "label": "Discharge Instructions",
            "group": "DISCHARGE",
            "display_order": 220,
            "schema": {
                "items": [
                    {"key": "diet", "type": "textarea", "label": "Diet advice"},
                    {"key": "activity", "type": "textarea", "label": "Activity advice"},
                    {"key": "warning_signs", "type": "textarea", "label": "Warning signs / When to return"},
                ]
            },
        },

        # --- OT blocks ---
        {
            "code": "BLOCK_OT_WHO_CHECKLIST",
            "label": "OT Safety Checklist (WHO)",
            "group": "OT",
            "display_order": 310,
            "schema": {
                "items": [
                    {"key": "identity_confirmed", "type": "radio", "label": "Patient identity confirmed?", "options": ["No", "Yes"]},
                    {"key": "site_marked", "type": "radio", "label": "Site marked?", "options": ["No", "Yes", "Not applicable"]},
                    {"key": "allergy_checked", "type": "radio", "label": "Allergy checked?", "options": ["No", "Yes"]},
                    {"key": "antibiotic_given", "type": "radio", "label": "Prophylactic antibiotic given (if needed)?", "options": ["No", "Yes", "Not applicable"]},
                    {"key": "ot_notes", "type": "textarea", "label": "OT notes"},
                ]
            },
        },
        {
            "code": "BLOCK_ANESTHESIA_RECORD_BASIC",
            "label": "Anesthesia Record (Basic)",
            "group": "OT",
            "display_order": 320,
            "schema": {
                "items": [
                    {"key": "anesthesia_type", "type": "select", "label": "Anesthesia type", "options": ["GA", "Spinal", "Epidural", "Local", "Sedation", "Other"]},
                    {"key": "asa_grade", "type": "select", "label": "ASA Grade", "options": ["I", "II", "III", "IV", "V"]},
                    {"key": "airway", "type": "select", "label": "Airway", "options": ["Easy", "Difficult", "Not assessed"]},
                    {"key": "anesthesia_notes", "type": "textarea", "label": "Anesthesia notes"},
                ]
            },
        },

        # --- OBGYN / ANC blocks ---
        {
            "code": "BLOCK_ANC_PROFILE",
            "label": "ANC Profile (LMP/EDD/GA)",
            "group": "OBGYN",
            "display_order": 410,
            "schema": {
                "items": [
                    {"key": "pregnant", "type": "radio", "label": "Pregnant?", "options": ["No", "Yes"], "required": True},
                    {"key": "lmp", "type": "date", "label": "LMP"},
                    {"key": "edd", "type": "date", "label": "EDD"},
                    {"key": "ga_weeks", "type": "number", "label": "GA (weeks)", "min": 0, "max": 50},
                    {"key": "bp", "type": "text", "label": "BP (mmHg)", "placeholder": "120/80"},
                    {"key": "weight", "type": "number", "label": "Weight (kg)", "min": 0, "max": 250},
                    {"key": "urine_albumin", "type": "select", "label": "Urine Albumin", "options": ["Nil", "Trace", "+", "++", "+++", "++++"]},
                    {"key": "urine_sugar", "type": "select", "label": "Urine Sugar", "options": ["Nil", "Trace", "+", "++", "+++", "++++"]},
                ]
            },
        },
        {
            "code": "BLOCK_MENSTRUAL_HISTORY",
            "label": "Menstrual History",
            "group": "OBGYN",
            "display_order": 420,
            "schema": {
                "items": [
                    {"key": "menarche_age", "type": "number", "label": "Age at menarche", "min": 0, "max": 30},
                    {"key": "cycle_regular", "type": "radio", "label": "Cycle regular?", "options": ["No", "Yes"]},
                    {"key": "cycle_length_days", "type": "number", "label": "Cycle length (days)", "min": 10, "max": 60},
                    {"key": "flow", "type": "select", "label": "Flow", "options": ["Scanty", "Normal", "Heavy"]},
                    {"key": "dysmenorrhea", "type": "radio", "label": "Dysmenorrhea?", "options": ["No", "Yes"]},
                ]
            },
        },
        {
            "code": "BLOCK_OB_HISTORY_GTPAL",
            "label": "Obstetric History (GTPAL)",
            "group": "OBGYN",
            "display_order": 430,
            "schema": {
                "items": [
                    {"key": "gravida", "type": "number", "label": "Gravida (G)", "min": 0, "max": 20},
                    {"key": "term", "type": "number", "label": "Term births (T)", "min": 0, "max": 20},
                    {"key": "preterm", "type": "number", "label": "Preterm births (P)", "min": 0, "max": 20},
                    {"key": "abortions", "type": "number", "label": "Abortions (A)", "min": 0, "max": 20},
                    {"key": "living", "type": "number", "label": "Living (L)", "min": 0, "max": 20},
                    {"key": "ob_notes", "type": "textarea", "label": "OB notes"},
                ]
            },
        },
        {
            "code": "BLOCK_FETAL_ASSESSMENT",
            "label": "Fetal Assessment",
            "group": "OBGYN",
            "display_order": 440,
            "schema": {
                "items": [
                    {"key": "fhr", "type": "number", "label": "FHR (bpm)", "min": 60, "max": 220},
                    {"key": "movements", "type": "select", "label": "Fetal movements", "options": ["Present", "Reduced", "Absent"]},
                    {"key": "presentation", "type": "select", "label": "Presentation", "options": ["Cephalic", "Breech", "Transverse", "Not known"]},
                    {"key": "liquor", "type": "select", "label": "Liquor", "options": ["Adequate", "Reduced", "Increased", "Not assessed"]},
                ]
            },
        },
        {
            "code": "BLOCK_LABOUR_PROGRESS",
            "label": "Labour Progress (Basic)",
            "group": "OBGYN",
            "display_order": 450,
            "schema": {
                "items": [
                    {"key": "cervical_dilation_cm", "type": "number", "label": "Cervical dilation (cm)", "min": 0, "max": 10},
                    {"key": "effacement", "type": "select", "label": "Effacement", "options": ["0–30%", "30–60%", "60–90%", "90–100%"]},
                    {"key": "station", "type": "select", "label": "Station", "options": ["-3", "-2", "-1", "0", "+1", "+2", "+3"]},
                    {"key": "contractions", "type": "textarea", "label": "Contractions (frequency/intensity)"},
                ]
            },
        },
        {
            "code": "BLOCK_NEWBORN_APGAR",
            "label": "Newborn APGAR",
            "group": "OBGYN",
            "display_order": 460,
            "schema": {
                "items": [
                    {"key": "apgar_1min", "type": "number", "label": "APGAR 1 min", "min": 0, "max": 10},
                    {"key": "apgar_5min", "type": "number", "label": "APGAR 5 min", "min": 0, "max": 10},
                    {"key": "birth_weight", "type": "number", "label": "Birth weight (kg)", "min": 0, "max": 10},
                    {"key": "newborn_notes", "type": "textarea", "label": "Newborn notes"},
                ]
            },
        },

        # --- Common blocks ---
        {
            "code": "BLOCK_SIGNATURES",
            "label": "Signatures",
            "group": "GENERAL",
            "display_order": 900,
            "schema": {
                "items": [
                    {"key": "doctor_sign", "type": "signature", "label": "Doctor signature"},
                    {"key": "nurse_sign", "type": "signature", "label": "Nurse signature"},
                ]
            },
        },
        {
            "code": "BLOCK_ATTACHMENTS",
            "label": "Attachments",
            "group": "GENERAL",
            "display_order": 910,
            "schema": {"items": [{"key": "attachments", "type": "file", "label": "Upload files"}]},
        },
    ]


# -----------------------------
# UPSERT HELPERS (idempotent)
# -----------------------------
def upsert_sections(
    db: Session,
    *,
    rows: List[Dict[str, Any]],
    dept_code: Optional[str],
    record_type_code: Optional[str],
    update_existing: bool,
) -> Tuple[int, int]:
    created = 0
    updated = 0

    for r in rows:
        code = norm_code(r["code"])
        row = (
            db.query(EmrSectionLibrary)
            .filter(
                EmrSectionLibrary.code == code,
                EmrSectionLibrary.dept_code.is_(None) if dept_code is None else EmrSectionLibrary.dept_code == dept_code,
                EmrSectionLibrary.record_type_code.is_(None) if record_type_code is None else EmrSectionLibrary.record_type_code == record_type_code,
            )
            .one_or_none()
        )
        if not row:
            row = EmrSectionLibrary(
                code=code,
                label=str(r["label"]).strip(),
                dept_code=dept_code,
                record_type_code=record_type_code,
                group=str(r.get("group") or "GENERAL"),
                is_active=True,
                display_order=int(r.get("display_order") or 1000),
            )
            db.add(row)
            created += 1
        else:
            if update_existing:
                changed = False
                new_label = str(r["label"]).strip()
                new_group = str(r.get("group") or row.group or "GENERAL")
                new_order = int(r.get("display_order") or row.display_order or 1000)

                if row.label != new_label:
                    row.label = new_label
                    changed = True
                if (row.group or "") != new_group:
                    row.group = new_group
                    changed = True
                if int(row.display_order or 1000) != new_order:
                    row.display_order = new_order
                    changed = True
                if row.is_active is not True:
                    row.is_active = True
                    changed = True

                if changed:
                    updated += 1

    return created, updated


def upsert_blocks(
    db: Session,
    *,
    rows: List[Dict[str, Any]],
    dept_code: Optional[str],
    record_type_code: Optional[str],
    update_existing: bool,
) -> Tuple[int, int]:
    created = 0
    updated = 0

    for r in rows:
        code = norm_code(r["code"])
        row = (
            db.query(EmrTemplateBlock)
            .filter(
                EmrTemplateBlock.code == code,
                EmrTemplateBlock.dept_code.is_(None) if dept_code is None else EmrTemplateBlock.dept_code == dept_code,
                EmrTemplateBlock.record_type_code.is_(None) if record_type_code is None else EmrTemplateBlock.record_type_code == record_type_code,
            )
            .one_or_none()
        )

        schema_obj = r.get("schema") or {}
        if not isinstance(schema_obj, dict):
            schema_obj = {}

        if not row:
            row = EmrTemplateBlock(
                code=code,
                label=str(r["label"]).strip(),
                description=str(r.get("description") or "").strip() or None,
                dept_code=dept_code,
                record_type_code=record_type_code,
                group=str(r.get("group") or "GENERAL"),
                is_active=True,
                display_order=int(r.get("display_order") or 1000),
                schema_json=jdump(schema_obj),
            )
            db.add(row)
            created += 1
        else:
            if update_existing:
                changed = False
                new_label = str(r["label"]).strip()
                new_group = str(r.get("group") or row.group or "GENERAL")
                new_order = int(r.get("display_order") or row.display_order or 1000)
                new_schema = jdump(schema_obj)

                if row.label != new_label:
                    row.label = new_label
                    changed = True
                if (row.group or "") != new_group:
                    row.group = new_group
                    changed = True
                if int(row.display_order or 1000) != new_order:
                    row.display_order = new_order
                    changed = True
                if row.schema_json != new_schema:
                    row.schema_json = new_schema
                    changed = True
                if row.is_active is not True:
                    row.is_active = True
                    changed = True

                if changed:
                    updated += 1

    return created, updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant-db-uri", default=os.getenv("TENANT_DB_URI", "").strip(), help="MySQL tenant DB URI")
    ap.add_argument("--dept-code", default="", help="Optional: seed scoped to a department code (e.g., OBGYN). Default: global")
    ap.add_argument("--record-type-code", default="", help="Optional: seed scoped to a record type code (e.g., OPD_NOTE). Default: global")
    ap.add_argument("--update-existing", action="store_true", help="If set, update label/group/order/schema for existing codes")
    args = ap.parse_args()

    if not args.tenant_db_uri:
        raise SystemExit("❌ Provide --tenant-db-uri or set TENANT_DB_URI")

    dept_code = norm_code(args.dept_code) if args.dept_code.strip() else None
    record_type_code = norm_code(args.record_type_code) if args.record_type_code.strip() else None

    db = _get_session(args.tenant_db_uri)
    try:
        _ensure_tables_exist(db)

        # ---- seed sections ----
        sec_rows = seed_sections_global()
        sc, su = upsert_sections(
            db,
            rows=sec_rows,
            dept_code=dept_code,
            record_type_code=record_type_code,
            update_existing=bool(args.update_existing),
        )

        # ---- seed blocks ----
        blk_rows = seed_blocks_global()
        bc, bu = upsert_blocks(
            db,
            rows=blk_rows,
            dept_code=dept_code,
            record_type_code=record_type_code,
            update_existing=bool(args.update_existing),
        )

        db.commit()

        scope = f"scope dept={dept_code or 'GLOBAL'} type={record_type_code or 'GLOBAL'}"
        print(f"✅ EMR seed completed ({scope})")
        print(f"   Sections: created={sc}, updated={su}")
        print(f"   Blocks:   created={bc}, updated={bu}")

    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
