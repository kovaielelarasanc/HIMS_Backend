# FILE: app/models/billing.py
from __future__ import annotations

import enum
from enum import Enum as PyEnum
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy import JSON
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    Numeric,
    ForeignKey,
    Enum,
    Boolean,
    UniqueConstraint,
    Index,
    JSON,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

Money = Numeric(14, 2)
Qty = Numeric(14, 4)
Rate = Numeric(14, 2)
Pct = Numeric(5, 2)

MYSQL_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


# ============================================================
# Enums
# ============================================================
class EncounterType(str, enum.Enum):
    OP = "OP"
    IP = "IP"
    OT = "OT"
    ER = "ER"


class BillingCaseStatus(str, enum.Enum):
    OPEN = "OPEN"
    READY_FOR_POST = "READY_FOR_POST"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class PayerMode(str, enum.Enum):
    SELF = "SELF"
    INSURANCE = "INSURANCE"
    CORPORATE = "CORPORATE"
    MIXED = "MIXED"


class InvoiceType(str, enum.Enum):
    PATIENT = "PATIENT"
    INSURER = "INSURER"
    PHARMACY = "PHARMACY"
    PACKAGE = "PACKAGE"
    ADJUSTMENT = "ADJUSTMENT"


class DocStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    POSTED = "POSTED"
    VOID = "VOID"


class PayerType(str, enum.Enum):
    PATIENT = "PATIENT"
    INSURER = "INSURER"
    CORPORATE = "CORPORATE"
    TPA = "TPA"


class ServiceGroup(str, enum.Enum):
    CONSULT = "CONSULT"
    LAB = "LAB"
    RAD = "RAD"
    PHARM = "PHARM"
    OT = "OT"
    ROOM = "ROOM"
    PROC = "PROC"
    NURSING = "NURSING"
    SURGEON = "SURGEON"
    ANESTHESIA= "ANESTHESIA"
    OT_DOCTOR = "OT_DOCTOR"
    MISC = "MISC"
    OPD = "OPD"
    IPD = "IPD"
    GENERAL = "GENERAL"
    BLOOD = "BLOOD"
    DIET = "DIET"



class CoverageFlag(str, enum.Enum):
    YES = "YES"
    NO = "NO"
    PARTIAL = "PARTIAL"


class PayMode(str, enum.Enum):
    CASH = "CASH"
    CARD = "CARD"
    UPI = "UPI"
    BANK = "BANK"
    WALLET = "WALLET"


class AdvanceType(str, enum.Enum):
    ADVANCE = "ADVANCE"
    REFUND = "REFUND"
    ADJUSTMENT = "ADJUSTMENT"


class TariffType(str, enum.Enum):
    GENERAL = "GENERAL"
    INSURANCE = "INSURANCE"
    CORPORATE = "CORPORATE"


class DiscountScope(str, enum.Enum):
    LINE = "LINE"
    INVOICE = "INVOICE"


class InsurancePayerKind(str, enum.Enum):
    INSURANCE = "INSURANCE"
    TPA = "TPA"
    CORPORATE = "CORPORATE"


class InsuranceStatus(str, enum.Enum):
    INITIATED = "INITIATED"
    PREAUTH_SUBMITTED = "PREAUTH_SUBMITTED"
    PREAUTH_APPROVED = "PREAUTH_APPROVED"
    PREAUTH_PARTIAL = "PREAUTH_PARTIAL"
    PREAUTH_REJECTED = "PREAUTH_REJECTED"
    CLAIM_SUBMITTED = "CLAIM_SUBMITTED"
    QUERY = "QUERY"
    SETTLED = "SETTLED"
    DENIED = "DENIED"
    CLOSED = "CLOSED"


class PreauthStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ClaimStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_QUERY = "UNDER_QUERY"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    SETTLED = "SETTLED"
    CLOSED = "CLOSED"


class NoteType(str, enum.Enum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class NumberDocType(str, enum.Enum):
    CASE = "CASE"
    INVOICE = "INVOICE"
    NOTE = "NOTE"
    RECEIPT = "RECEIPT"


class NumberResetPeriod(str, enum.Enum):
    NONE = "NONE"
    YEAR = "YEAR"
    MONTH = "MONTH"


# -----------------------------
# Edit Request Status
# -----------------------------
class InvoiceEditRequestStatus(str, PyEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PaymentDirection(str, enum.Enum):
    IN = "IN"  # money received
    OUT = "OUT"  # money paid back (refund)


class PaymentKind(str, enum.Enum):
    RECEIPT = "RECEIPT"  # normal payment
    ADVANCE_ADJUSTMENT = "ADVANCE_ADJUSTMENT"  # applying advance to invoices
    REFUND = "REFUND"  # refund
    WRITE_OFF = "WRITE_OFF"  # optional future use


class ReceiptStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    VOID = "VOID"


# ============================================================
# Number Series (for Case / Invoice / Note / Receipt)
# ============================================================
class BillingNumberSeries(Base):
    """
    DB-level counter row.

    Tenant DB only => NO tenant_id.

    Use service layer:
      SELECT ... FOR UPDATE on this row -> increment next_number -> commit.

    Period logic:
      - reset_period NONE : last_period_key can be NULL, no reset
      - YEAR              : last_period_key = "2026"
      - MONTH             : last_period_key = "2026-01"
    """
    __tablename__ = "billing_number_series"
    __table_args__ = (
        UniqueConstraint(
            "doc_type",
            "reset_period",
            "prefix",
            name="uq_billing_number_series",
        ),
        Index("idx_billing_number_series_doc", "doc_type"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    doc_type = Column(Enum(NumberDocType), nullable=False)

    # prefix can be ORG + encounter + ddmmyyyy, keep length wide
    prefix = Column(String(64), nullable=False, default="")
    reset_period = Column(
        Enum(NumberResetPeriod),
        nullable=False,
        default=NumberResetPeriod.YEAR,
    )

    padding = Column(Integer, nullable=False, default=6)
    next_number = Column(BigInteger, nullable=False, default=1)

    last_period_key = Column(String(16), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())
    created_at = Column(DateTime, nullable=False, server_default=func.now())


# ============================================================
# Masters
# ============================================================
class BillingRevenueHead(Base):
    __tablename__ = "billing_revenue_heads"
    __table_args__ = MYSQL_ARGS

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    description = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())


class BillingCostCenter(Base):
    __tablename__ = "billing_cost_centers"
    __table_args__ = MYSQL_ARGS

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    department_id = Column(Integer,
                           nullable=True)  # optional external link later
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())


class BillingTariffPlan(Base):
    __tablename__ = "billing_tariff_plans"
    __table_args__ = MYSQL_ARGS

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)

    type = Column(Enum(TariffType), nullable=False, default=TariffType.GENERAL)

    # insurer/tpa/corporate id (if you map later)
    payer_id = Column(Integer, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)

    rates = relationship(
        "BillingTariffRate",
        back_populates="tariff_plan",
        cascade="all, delete-orphan",
    )

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())


class BillingTariffRate(Base):
    __tablename__ = "billing_tariff_rates"
    __table_args__ = (
        UniqueConstraint("tariff_plan_id",
                         "item_type",
                         "item_id",
                         name="uq_billing_tariff_rates_plan_item"),
        Index("idx_billing_tariff_rates_item", "item_type", "item_id"),
        Index("idx_billing_tariff_rates_plan", "tariff_plan_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    tariff_plan_id = Column(
        Integer,
        ForeignKey("billing_tariff_plans.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # LAB_TEST / RAD_TEST / DRUG / OT_PROC / ROOM_TARIFF etc
    item_type = Column(String(32), nullable=False)
    item_id = Column(Integer, nullable=False)

    rate = Column(Money, nullable=False, default=0)
    gst_rate = Column(Pct, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)

    tariff_plan = relationship("BillingTariffPlan", back_populates="rates")

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())


class BillingDiscountRule(Base):
    __tablename__ = "billing_discount_rules"
    __table_args__ = MYSQL_ARGS

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)

    scope = Column(Enum(DiscountScope),
                   nullable=False,
                   default=DiscountScope.INVOICE)
    max_percent = Column(Pct, nullable=False, default=0)
    max_amount = Column(Money, nullable=False, default=0)
    requires_approval = Column(Boolean, nullable=False, default=False)
    approval_min_amount = Column(Money, nullable=False, default=0)

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())


# ============================================================
# Core: Case / Links / Invoice / Lines
# ============================================================
class BillingCase(Base):
    __tablename__ = "billing_cases"
    __table_args__ = (
        UniqueConstraint("encounter_type",
                         "encounter_id",
                         name="uq_billing_cases_encounter"),
        Index("idx_billing_cases_status", "status"),
        Index("idx_billing_cases_patient", "patient_id"),
        Index("idx_billing_cases_encounter", "encounter_type", "encounter_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    patient_id = Column(
        Integer,
        ForeignKey("patients.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    encounter_type = Column(Enum(EncounterType), nullable=False)
    encounter_id = Column(BigInteger, nullable=False)

    case_number = Column(String(32), nullable=False, unique=True, index=True)
    status = Column(Enum(BillingCaseStatus),
                    nullable=False,
                    default=BillingCaseStatus.OPEN)

    payer_mode = Column(Enum(PayerMode),
                        nullable=False,
                        default=PayerMode.SELF)

    tariff_plan_id = Column(
        Integer,
        ForeignKey("billing_tariff_plans.id", ondelete="RESTRICT"),
        nullable=True,
    )

    notes = Column(Text, nullable=True)
    default_payer_type = Column(
        String(20), nullable=True)  # PAYER | TPA | CREDIT_PLAN | PATIENT
    default_payer_id = Column(Integer, nullable=True)

    default_tpa_id = Column(Integer, nullable=True)
    default_credit_plan_id = Column(Integer, nullable=True)

    referral_user_id = Column(Integer, nullable=True)
    referral_notes = Column(Text, nullable=True)

    created_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    updated_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    # Relationships
    patient = relationship("Patient", foreign_keys=[patient_id])

    created_by_user = relationship("User", foreign_keys=[created_by])
    updated_by_user = relationship("User", foreign_keys=[updated_by])

    tariff_plan = relationship("BillingTariffPlan",
                               foreign_keys=[tariff_plan_id])

    invoices = relationship("BillingInvoice",
                            back_populates="billing_case",
                            cascade="all, delete-orphan")
    payments = relationship("BillingPayment",
                            back_populates="billing_case",
                            cascade="all, delete-orphan")
    advances = relationship("BillingAdvance",
                            back_populates="billing_case",
                            cascade="all, delete-orphan")

    insurance = relationship(
        "BillingInsuranceCase",
        back_populates="billing_case",
        uselist=False,
        cascade="all, delete-orphan",
    )
    links = relationship("BillingCaseLink",
                         back_populates="billing_case",
                         cascade="all, delete-orphan")


class BillingCaseLink(Base):
    """
    Flexible links to connect BillingCase with:
      - OPD appointment_id
      - OPD visit_id
      - IPD admission_id
      - OT case_id
      - LIS order id, RIS order id, pharmacy prescription id, etc.
    """
    __tablename__ = "billing_case_links"
    __table_args__ = (
        UniqueConstraint("billing_case_id",
                         "entity_type",
                         "entity_id",
                         name="uq_billing_case_links"),
        Index("idx_billing_case_links_entity", "entity_type", "entity_id"),
        Index("idx_billing_case_links_case", "billing_case_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # "APPOINTMENT"/"VISIT"/"ADMISSION"/"OT_CASE"/"LIS_ORDER"/"RIS_ORDER"/"PHARM_RX"
    entity_type = Column(String(32), nullable=False)
    entity_id = Column(BigInteger, nullable=False)

    created_at = Column(DateTime, nullable=False, server_default=func.now())

    billing_case = relationship("BillingCase", back_populates="links")


class BillingInvoice(Base):
    __tablename__ = "billing_invoices"
    __table_args__ = (
        Index("idx_billing_invoices_case", "billing_case_id"),
        Index("idx_billing_invoices_status", "status"),
        Index("idx_billing_invoices_type", "invoice_type"),
        Index("idx_billing_invoices_module", "module"),
        Index("idx_billing_invoices_payer", "payer_type", "payer_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    invoice_number = Column(String(32),
                            nullable=False,
                            unique=True,
                            index=True)

    # LAB / RIS / PHARM / OT / ROOM / OPD / IPD / MISC / etc.
    module = Column(String(16), nullable=True, index=True)

    invoice_type = Column(Enum(InvoiceType),
                          nullable=False,
                          default=InvoiceType.PATIENT)
    status = Column(Enum(DocStatus), nullable=False, default=DocStatus.DRAFT)

    payer_type = Column(Enum(PayerType),
                        nullable=False,
                        default=PayerType.PATIENT)
    payer_id = Column(Integer, nullable=True)

    currency = Column(String(3), nullable=False, default="INR")

    sub_total = Column(Money, nullable=False, default=0)
    discount_total = Column(Money, nullable=False, default=0)
    tax_total = Column(Money, nullable=False, default=0)
    round_off = Column(Money, nullable=False, default=0)
    grand_total = Column(Money, nullable=False, default=0)

    approved_by = Column(Integer,
                         ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)
    approved_at = Column(DateTime, nullable=True)

    posted_by = Column(Integer,
                       ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True)
    posted_at = Column(DateTime, nullable=True)

    voided_by = Column(Integer,
                       ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True)
    voided_at = Column(DateTime, nullable=True)
    void_reason = Column(String(255), nullable=True)

    created_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    updated_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    billing_case = relationship("BillingCase", back_populates="invoices")

    lines = relationship("BillingInvoiceLine",
                         back_populates="invoice",
                         cascade="all, delete-orphan")
    payments = relationship("BillingPayment",
                            back_populates="invoice",
                            cascade="all, delete-orphan")
    notes = relationship("BillingNote",
                         back_populates="ref_invoice",
                         cascade="all, delete-orphan")

    service_date = Column(DateTime, nullable=True)  # date shown in print
    meta_json = Column(JSON, nullable=True)

    pharmacy_sales = relationship(
        "PharmacySale",
        back_populates="billing_invoice",
        lazy="selectin",
    )
    payment_allocations = relationship(
        "BillingPaymentAllocation",
        back_populates="invoice",
        lazy="selectin",
    )

    created_by_user = relationship("User", foreign_keys=[created_by])
    updated_by_user = relationship("User", foreign_keys=[updated_by])
    approved_by_user = relationship("User", foreign_keys=[approved_by])
    posted_by_user = relationship("User", foreign_keys=[posted_by])
    voided_by_user = relationship("User", foreign_keys=[voided_by])


class BillingInvoiceLine(Base):
    __tablename__ = "billing_invoice_lines"
    __table_args__ = (
        UniqueConstraint(
            "billing_case_id",
            "source_module",
            "source_ref_id",
            "source_line_key",
            name="uq_billing_lines_idempotent",
        ),
        Index("idx_billing_lines_invoice", "invoice_id"),
        Index("idx_billing_lines_source", "source_module", "source_ref_id"),
        Index("idx_billing_lines_item", "item_type", "item_id"),
        Index("idx_billing_lines_rev", "revenue_head_id"),
        Index("idx_billing_lines_cc", "cost_center_id"),
        Index("idx_billing_lines_doctor", "doctor_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    invoice_id = Column(
        BigInteger,
        ForeignKey("billing_invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    service_group = Column(Enum(ServiceGroup),
                           nullable=False,
                           default=ServiceGroup.MISC)

    item_type = Column(String(32), nullable=True)
    item_id = Column(Integer, nullable=True)
    item_code = Column(String(64), nullable=True)

    description = Column(String(255), nullable=False)

    qty = Column(Qty, nullable=False, default=1)
    unit_price = Column(Rate, nullable=False, default=0)

    discount_percent = Column(Pct, nullable=False, default=0)
    discount_amount = Column(Money, nullable=False, default=0)

    gst_rate = Column(Pct, nullable=False, default=0)
    tax_amount = Column(Money, nullable=False, default=0)

    line_total = Column(Money, nullable=False, default=0)
    net_amount = Column(Money, nullable=False, default=0)

    revenue_head_id = Column(Integer,
                             ForeignKey("billing_revenue_heads.id",
                                        ondelete="RESTRICT"),
                             nullable=True)
    cost_center_id = Column(Integer,
                            ForeignKey("billing_cost_centers.id",
                                       ondelete="RESTRICT"),
                            nullable=True)

    doctor_id = Column(Integer,
                       ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True,
                       index=True)

    # AUTO sync references
    source_module = Column(String(16), nullable=True)
    source_ref_id = Column(
        BigInteger,
        nullable=True)  # order_id / rx_id / ot_case_id / admission_id etc
    source_line_key = Column(
        String(64), nullable=True
    )  # stable per line: test_id / drug_id / "ROOM:YYYY-MM-DD"

    # insurance
    is_covered = Column(Enum(CoverageFlag),
                        nullable=False,
                        default=CoverageFlag.NO)
    approved_amount = Column(Money, nullable=False, default=0)
    patient_pay_amount = Column(Money, nullable=False, default=0)
    requires_preauth = Column(Boolean, nullable=False, default=False)
    insurer_pay_amount = Column(Money, nullable=False, default=0)  # NEW

    # manual add
    is_manual = Column(Boolean, nullable=False, default=False)
    manual_reason = Column(String(255), nullable=True)

    created_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    invoice = relationship("BillingInvoice", back_populates="lines")

    revenue_head = relationship("BillingRevenueHead",
                                foreign_keys=[revenue_head_id])
    cost_center = relationship("BillingCostCenter",
                               foreign_keys=[cost_center_id])

    doctor = relationship("User", foreign_keys=[doctor_id])
    created_by_user = relationship("User", foreign_keys=[created_by])
    service_date = Column(DateTime, nullable=True)

    # per-line meta (pharmacy batch_id/expiry/hsn_sac etc.)
    meta_json = Column(MutableDict.as_mutable(JSON), nullable=True)


# ============================================================
# Payments / Advances
# ============================================================
class BillingPayment(Base):
    __tablename__ = "billing_payments"
    __table_args__ = (
        Index("idx_billing_payments_case", "billing_case_id"),
        Index("idx_billing_payments_invoice", "invoice_id"),
        Index("idx_billing_payments_payer", "payer_type", "payer_id"),
        Index("idx_billing_payments_mode", "mode"),
        Index("idx_billing_payments_received_at", "received_at"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    billing_case_id = Column(
        BigInteger,  # ✅ FIX
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    invoice_id = Column(
        BigInteger,
        ForeignKey("billing_invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    payer_type = Column(Enum(PayerType),
                        nullable=False,
                        default=PayerType.PATIENT)
    payer_id = Column(Integer, nullable=True)

    mode = Column(Enum(PayMode), nullable=False, default=PayMode.CASH)
    amount = Column(Money, nullable=False, default=0)

    txn_ref = Column(String(64), nullable=True)
    received_at = Column(DateTime, nullable=False, server_default=func.now())

    received_by = Column(Integer,
                         ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)
    notes = Column(String(255), nullable=True)

    # ✅ keep only ONE receipt_number
    receipt_number = Column(String(50), nullable=True, unique=True, index=True)

    kind = Column(Enum(PaymentKind),
                  nullable=False,
                  default=PaymentKind.RECEIPT)
    direction = Column(Enum(PaymentDirection),
                       nullable=False,
                       default=PaymentDirection.IN)
    status = Column(Enum(ReceiptStatus),
                    nullable=False,
                    default=ReceiptStatus.ACTIVE)

    meta_json = Column(JSON, nullable=True)

    voided_by = Column(Integer,
                       ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True)
    voided_at = Column(DateTime, nullable=True)
    void_reason = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())

    billing_case = relationship("BillingCase", back_populates="payments")
    invoice = relationship("BillingInvoice", back_populates="payments")
    received_by_user = relationship("User", foreign_keys=[received_by])

    allocations = relationship(
        "BillingPaymentAllocation",
        back_populates="payment",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    voided_by_user = relationship("User", foreign_keys=[voided_by])


class BillingAdvance(Base):
    __tablename__ = "billing_advances"
    __table_args__ = (
        Index("idx_billing_advances_case", "billing_case_id"),
        Index("idx_billing_advances_type", "entry_type"),
        Index("idx_billing_advances_entry_at", "entry_at"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    entry_type = Column(Enum(AdvanceType),
                        nullable=False,
                        default=AdvanceType.ADVANCE)
    mode = Column(Enum(PayMode), nullable=False, default=PayMode.CASH)
    amount = Column(Money, nullable=False, default=0)

    txn_ref = Column(String(64), nullable=True)
    entry_at = Column(DateTime, nullable=False, server_default=func.now())

    entry_by = Column(Integer,
                      ForeignKey("users.id", ondelete="SET NULL"),
                      nullable=True)

    remarks = Column(String(255), nullable=True)

    billing_case = relationship("BillingCase", back_populates="advances")
    entry_by_user = relationship("User", foreign_keys=[entry_by])
    applications = relationship(
        "BillingAdvanceApplication",
        primaryjoin="BillingAdvance.id==BillingAdvanceApplication.advance_id",
        viewonly=True,
    )


class BillingPaymentAllocation(Base):
    __tablename__ = "billing_payment_allocations"
    __table_args__ = (
        Index("idx_bpa_case", "billing_case_id"),
        Index("idx_bpa_payment", "payment_id"),
        Index("idx_bpa_invoice", "invoice_id"),
        Index("idx_bpa_bucket", "payer_bucket"),
        Index("idx_bpa_tenant", "tenant_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, nullable=True, index=True)

    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    payment_id = Column(
        BigInteger,
        ForeignKey("billing_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    invoice_id = Column(
        BigInteger,
        ForeignKey("billing_invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ✅ this MUST exist because DB has NOT NULL payer_bucket
    payer_bucket = Column(
        Enum(PayerType),  # PATIENT/INSURER/CORPORATE matches DB
        nullable=False,
        default=PayerType.PATIENT,
    )

    amount = Column(Money, nullable=False, default=0)

    # ✅ audit columns (add in DB below)
    status = Column(Enum(ReceiptStatus),
                    nullable=False,
                    default=ReceiptStatus.ACTIVE)
    allocated_at = Column(DateTime, nullable=False, server_default=func.now())
    allocated_by = Column(Integer, nullable=True)

    voided_at = Column(DateTime, nullable=True)
    voided_by = Column(Integer, nullable=True)
    void_reason = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    payment = relationship("BillingPayment",
                           back_populates="allocations",
                           lazy="selectin")
    invoice = relationship("BillingInvoice",
                           back_populates="payment_allocations",
                           lazy="selectin")
class BillingAdvanceApplication(Base):
    """
    Tracks how advances are consumed when applying to invoices.
    (Advance wallet -> applied as ADVANCE_ADJUSTMENT payment)
    """
    __tablename__ = "billing_advance_applications"
    __table_args__ = (
        Index("idx_baa_case", "billing_case_id"),
        Index("idx_baa_advance", "advance_id"),
        Index("idx_baa_payment", "payment_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    advance_id = Column(
        BigInteger,
        ForeignKey("billing_advances.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    payment_id = Column(
        BigInteger,
        ForeignKey("billing_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    amount = Column(Money, nullable=False, default=0)

    created_at = Column(DateTime, nullable=False, server_default=func.now())


# ============================================================
# Insurance
# ============================================================
class BillingInsuranceCase(Base):
    __tablename__ = "billing_insurance_cases"
    __table_args__ = (
        UniqueConstraint("billing_case_id",
                         name="uq_billing_ins_case_per_case"),
        Index("idx_billing_ins_status", "status"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    payer_kind = Column(Enum(InsurancePayerKind),
                        nullable=False,
                        default=InsurancePayerKind.INSURANCE)

    insurance_company_id = Column(Integer, nullable=True)
    tpa_id = Column(Integer, nullable=True)
    corporate_id = Column(Integer, nullable=True)

    policy_no = Column(String(64), nullable=True)
    member_id = Column(String(64), nullable=True)
    plan_name = Column(String(120), nullable=True)

    status = Column(Enum(InsuranceStatus),
                    nullable=False,
                    default=InsuranceStatus.INITIATED)

    approved_limit = Column(Money, nullable=False, default=0)
    approved_at = Column(DateTime, nullable=True)

    created_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    billing_case = relationship("BillingCase", back_populates="insurance")

    created_by_user = relationship("User", foreign_keys=[created_by])

    preauth_requests = relationship(
        "BillingPreauthRequest",
        back_populates="insurance_case",
        cascade="all, delete-orphan",
    )
    claims = relationship(
        "BillingClaim",
        back_populates="insurance_case",
        cascade="all, delete-orphan",
    )


class BillingPreauthRequest(Base):
    __tablename__ = "billing_preauth_requests"
    __table_args__ = (
        Index("idx_billing_preauth_case", "insurance_case_id"),
        Index("idx_billing_preauth_status", "status"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    insurance_case_id = Column(
        BigInteger,
        ForeignKey("billing_insurance_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    requested_amount = Column(Money, nullable=False, default=0)
    approved_amount = Column(Money, nullable=False, default=0)

    status = Column(Enum(PreauthStatus),
                    nullable=False,
                    default=PreauthStatus.DRAFT)

    submitted_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)

    remarks = Column(String(255), nullable=True)
    attachments_json = Column(JSON, nullable=True)

    created_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    insurance_case = relationship("BillingInsuranceCase",
                                  back_populates="preauth_requests")
    created_by_user = relationship("User", foreign_keys=[created_by])


class BillingClaim(Base):
    __tablename__ = "billing_claims"
    __table_args__ = (
        Index("idx_billing_claims_case", "insurance_case_id"),
        Index("idx_billing_claims_status", "status"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    insurance_case_id = Column(
        BigInteger,
        ForeignKey("billing_insurance_cases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    claim_amount = Column(Money, nullable=False, default=0)
    approved_amount = Column(Money, nullable=False, default=0)
    settled_amount = Column(Money, nullable=False, default=0)

    status = Column(Enum(ClaimStatus),
                    nullable=False,
                    default=ClaimStatus.DRAFT)

    submitted_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)

    remarks = Column(String(255), nullable=True)
    attachments_json = Column(JSON, nullable=True)

    created_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    insurance_case = relationship("BillingInsuranceCase",
                                  back_populates="claims")
    created_by_user = relationship("User", foreign_keys=[created_by])


# ============================================================
# Credit / Debit Notes
# ============================================================
class BillingNote(Base):
    __tablename__ = "billing_notes"
    __table_args__ = (
        Index("idx_billing_notes_invoice", "ref_invoice_id"),
        Index("idx_billing_notes_status", "status"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    ref_invoice_id = Column(
        BigInteger,
        ForeignKey("billing_invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    note_number = Column(String(32), nullable=False, unique=True, index=True)
    note_type = Column(Enum(NoteType), nullable=False)

    status = Column(Enum(DocStatus), nullable=False, default=DocStatus.DRAFT)

    sub_total = Column(Money, nullable=False, default=0)
    tax_total = Column(Money, nullable=False, default=0)
    grand_total = Column(Money, nullable=False, default=0)

    reason = Column(String(255), nullable=False)

    approved_by = Column(Integer,
                         ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)
    approved_at = Column(DateTime, nullable=True)

    posted_by = Column(Integer,
                       ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True)
    posted_at = Column(DateTime, nullable=True)

    created_by = Column(Integer,
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime,
                        nullable=False,
                        server_default=func.now(),
                        onupdate=func.now())

    ref_invoice = relationship("BillingInvoice", back_populates="notes")

    lines = relationship("BillingNoteLine",
                         back_populates="note",
                         cascade="all, delete-orphan")

    created_by_user = relationship("User", foreign_keys=[created_by])
    approved_by_user = relationship("User", foreign_keys=[approved_by])
    posted_by_user = relationship("User", foreign_keys=[posted_by])


class BillingNoteLine(Base):
    __tablename__ = "billing_note_lines"
    __table_args__ = (
        Index("idx_billing_note_lines_note", "note_id"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    note_id = Column(
        BigInteger,
        ForeignKey("billing_notes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    ref_invoice_line_id = Column(
        BigInteger,
        ForeignKey("billing_invoice_lines.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    description = Column(String(255), nullable=False)

    qty = Column(Qty, nullable=False, default=1)
    unit_price = Column(Rate, nullable=False, default=0)

    gst_rate = Column(Pct, nullable=False, default=0)
    tax_amount = Column(Money, nullable=False, default=0)

    line_total = Column(Money, nullable=False, default=0)
    net_amount = Column(Money, nullable=False, default=0)

    revenue_head_id = Column(Integer,
                             ForeignKey("billing_revenue_heads.id",
                                        ondelete="RESTRICT"),
                             nullable=True)
    cost_center_id = Column(Integer,
                            ForeignKey("billing_cost_centers.id",
                                       ondelete="RESTRICT"),
                            nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())

    note = relationship("BillingNote", back_populates="lines")

    revenue_head = relationship("BillingRevenueHead",
                                foreign_keys=[revenue_head_id])
    cost_center = relationship("BillingCostCenter",
                               foreign_keys=[cost_center_id])


# -----------------------------
# Billing Invoice Edit Requests
# -----------------------------
class BillingInvoiceEditRequest(Base):
    __tablename__ = "billing_invoice_edit_requests"
    __table_args__ = (
        Index("ix_bier_invoice_status", "invoice_id", "status"),
        Index("ix_bier_status_requested_at", "status", "requested_at"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    invoice_id = Column(
        BigInteger,
        ForeignKey("billing_invoices.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    billing_case_id = Column(
        BigInteger,
        ForeignKey("billing_cases.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    status = Column(String(20),
                    nullable=False,
                    default=InvoiceEditRequestStatus.PENDING.value)
    reason = Column(String(255), nullable=False, default="")

    requested_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    requested_at = Column(DateTime, server_default=func.now(), nullable=False)

    reviewed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    reviewed_at = Column(DateTime, nullable=True)
    decision_notes = Column(String(255), nullable=False, default="")

    unlock_hours = Column(Integer, nullable=False, default=24)
    expires_at = Column(DateTime, nullable=True)

    applied = Column(Boolean, nullable=False, default=False)

    invoice = relationship("BillingInvoice", foreign_keys=[invoice_id])
    case = relationship("BillingCase", foreign_keys=[billing_case_id])
    requested_by_user = relationship("User",
                                     foreign_keys=[requested_by_user_id])
    reviewed_by_user = relationship("User", foreign_keys=[reviewed_by_user_id])


# ============================================================
# Audit Log
# ============================================================
class BillingAuditLog(Base):
    __tablename__ = "billing_audit_logs"
    __table_args__ = (
        Index("idx_billing_audit_entity", "entity_type", "entity_id"),
        Index("idx_billing_audit_user", "user_id"),
        Index("idx_billing_audit_created", "created_at"),
        MYSQL_ARGS,
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    entity_type = Column(String(32), nullable=False)
    entity_id = Column(BigInteger, nullable=False)

    action = Column(String(32), nullable=False)

    old_json = Column(JSON, nullable=True)
    new_json = Column(JSON, nullable=True)
    reason = Column(String(255), nullable=True)

    user_id = Column(Integer,
                     ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
