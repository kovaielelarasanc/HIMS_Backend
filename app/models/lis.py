from __future__ import annotations
from datetime import datetime
from sqlalchemy import (Column, Integer, String, DateTime, Boolean, ForeignKey,
                        Numeric, Text, UniqueConstraint, Index)
from sqlalchemy.orm import relationship
from app.db.base import Base

# -------------------------
# Laboratory (LIS)
# -------------------------
# Reuses OPD master: app.models.opd.LabTest (code maps to NABL code).


class LisOrder(Base):
    """
    LIS order container (one order per draw/batch).
    Context-aware so it can be linked from OPD Visit or IPD Admission,
    but does not hard-depend on those tables.
    """
    __tablename__ = "lis_orders"
    __table_args__ = (Index("ix_lis_orders_patient_ctx", "patient_id",
                            "context_type", "context_id"), )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer,
                        ForeignKey("patients.id"),
                        nullable=False,
                        index=True)
    context_type = Column(String(10), nullable=True)  # opd | ipd
    context_id = Column(Integer, nullable=True)
    ordering_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    priority = Column(String(20), default="routine")  # routine | stat
    status = Column(
        String(20), default="ordered"
    )  # draft/ordered/collected/in_progress/validated/reported/cancelled
    
    billing_invoice_id = Column(Integer, nullable=True, index=True)
    billing_status = Column(String(20), default="not_billed")  # not_billed|billed|cancelled
    
    collected_at = Column(DateTime, nullable=True)
    reported_at = Column(DateTime, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("LisOrderItem",
                         back_populates="order",
                         cascade="all, delete-orphan")


class LisResultLine(Base):
    """
    Per-order result row linked to LabService (department + sub-department analyte).

    This is what you use for the “panel” style report like Haematology / CBC etc.
    """
    __tablename__ = "lis_result_lines"
    __table_args__ = (
        Index("ix_lis_res_order", "order_id"),
        Index("ix_lis_res_service", "service_id"),
        UniqueConstraint(
            "order_id",
            "service_id",
            name="uq_lis_result_order_service",
        ),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    order_id = Column(Integer, nullable=False, index=True)
    service_id = Column(
        Integer,
        ForeignKey("lab_services.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Snapshot fields (so old reports remain same even if master changes)
    department_id = Column(Integer,
                           ForeignKey("lab_departments.id"),
                           nullable=True)
    sub_department_id = Column(Integer,
                               ForeignKey("lab_departments.id"),
                               nullable=True)

    service_name = Column(String(255), nullable=False)
    unit = Column(String(64), nullable=True)
    normal_range = Column(String(255), nullable=True)

    result_value = Column(String(255), nullable=True)
    flag = Column(String(4), nullable=True)  # H / L / N / CRIT / etc.
    comments = Column(Text, nullable=True)

    entered_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    validated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reported_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    order = relationship(
        "LisOrder",
        backref="result_lines",
        primaryjoin="LisResultLine.order_id == LisOrder.id",
        foreign_keys=[order_id],
    )
    service = relationship("LabService", backref="result_lines")

    department = relationship(
        "LabDepartment",
        foreign_keys=[department_id],
        backref="result_lines_main",
    )
    sub_department = relationship(
        "LabDepartment",
        foreign_keys=[sub_department_id],
        backref="result_lines_sub",
    )


class LisOrderItem(Base):
    """
    Individual test line item.
    Links to OPD LabTest master (NABL mapping via LabTest.code).
    """
    __tablename__ = "lis_order_items"
    __table_args__ = (
        Index("ix_lis_items_order", "order_id"),
        Index("ix_lis_items_status", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer,
                      ForeignKey("lis_orders.id", ondelete="CASCADE"),
                      nullable=False)
    test_id = Column(Integer,
                     ForeignKey("lab_tests.id"),
                     nullable=False,
                     index=True)

    test_name = Column(String(200), nullable=False)
    test_code = Column(String(40),
                       nullable=False)  # NABL code (from LabTest.code)
    unit = Column(String(32), nullable=True)
    normal_range = Column(String(128), nullable=True)
    specimen_type = Column(String(64), nullable=True)

    sample_barcode = Column(String(64), nullable=True)
    status = Column(
        String(20), default="ordered"
    )  # ordered/collected/in_progress/validated/reported/cancelled

    result_value = Column(String(255), nullable=True)
    is_critical = Column(Boolean, default=False)
    result_at = Column(DateTime, nullable=True)

    validated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reported_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("LisOrder", back_populates="items")
    attachments = relationship("LisAttachment",
                               back_populates="item",
                               cascade="all, delete-orphan")


class LisAttachment(Base):
    __tablename__ = "lis_attachments"
    __table_args__ = (Index("ix_lis_att_item", "order_item_id"), )

    id = Column(Integer, primary_key=True, index=True)
    order_item_id = Column(Integer,
                           ForeignKey("lis_order_items.id",
                                      ondelete="CASCADE"),
                           nullable=False)
    file_url = Column(String(500), nullable=False)
    note = Column(String(255), default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("LisOrderItem", back_populates="attachments")


class LabDepartment(Base):
    """
    Lab Department / Sub-department master.

    Examples:
      - Biochemistry (parent)
      - Haematology (parent)
      - Clinical Pathology (parent)
      - Cardiac Markers (child of Biochemistry)
    """
    __tablename__ = "lab_departments"
    __table_args__ = (
        UniqueConstraint("name",
                         "parent_id",
                         name="uq_lab_dept_name_per_parent"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(50), nullable=True, index=True)
    description = Column(Text, nullable=True)

    # NULL = top level (department)
    # non-NULL = sub department
    parent_id = Column(Integer,
                       ForeignKey("lab_departments.id"),
                       nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        nullable=False,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    parent = relationship("LabDepartment",
                          remote_side=[id],
                          backref="children")
    services = relationship("LabService", back_populates="department")


class LabService(Base):
    """
    Department-wise service (test) master.

    - Unit, Normal Range are optional:
      if blank, we store "-" at DB level.
    """
    __tablename__ = "lab_services"
    __table_args__ = (
        UniqueConstraint("department_id",
                         "name",
                         name="uq_lab_service_name_per_dept"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, index=True)

    department_id = Column(Integer,
                           ForeignKey("lab_departments.id"),
                           nullable=False,
                           index=True)

    name = Column(String(255), nullable=False)
    code = Column(String(50), nullable=True, index=True)

    unit = Column(String(64), nullable=True)  # UI sends "" -> "-"
    normal_range = Column(String(255), nullable=True)  # UI sends "" -> "-"

    # Advanced fields (can be empty now, useful later)
    sample_type = Column(String(128), nullable=True)  # Serum / Plasma / Urine
    method = Column(String(128), nullable=True)  # Immunoassay / Colorimetric
    comments_template = Column(Text, nullable=True)  # auto comments on report

    is_active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime,
                        nullable=False,
                        default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    department = relationship("LabDepartment", back_populates="services")
