"""
GRPO (Goods Receipt PO) Models
Import models from main models.py to avoid duplication
"""
from models import GRPODocument, GRPOItem, User

class Status:
    """GRPO status constants"""
    DRAFT = 'draft'
    SUBMITTED = 'submitted'
    QC_APPROVED = 'qc_approved'
    POSTED = 'posted'
    REJECTED = 'rejected'

class QCStatus:
    """QC status constants"""
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'