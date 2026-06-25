"""SQLAlchemy models."""

from app.models.agent import Agent
from app.models.appointment import Appointment
from app.models.call_interaction import CallInteraction
from app.models.call_record import CallRecord
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.phone_number import PhoneNumber
from app.models.privacy_settings import ConsentRecord, PrivacySettings
from app.models.sms_message import SmsMessage
from app.models.user import User
from app.models.user_integration import UserIntegration
from app.models.workspace import AgentWorkspace, Workspace

__all__ = [
    "Agent",
    "AgentWorkspace",
    "Appointment",
    "CallInteraction",
    "CallRecord",
    "Campaign",
    "CampaignContact",
    "ConsentRecord",
    "Contact",
    "PhoneNumber",
    "PrivacySettings",
    "SmsMessage",
    "User",
    "UserIntegration",
    "Workspace",
]
