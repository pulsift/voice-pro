"""CRM tools for voice agents - bookings, contacts, appointments."""

import asyncio
import json
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.cache import cache_invalidate
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.contact import Contact
from app.services.fulfilment_webhook import schedule_fulfilment_webhook

logger = structlog.get_logger()
MAX_BOOKING_ATTEMPTS = 2
MAX_12_HOUR = 12
MAX_MINUTE = 59


class CRMTools:
    """Internal CRM tools for voice agents.

    Provides tools for:
    - Looking up customers by phone/email/name
    - Creating new contacts
    - Checking appointment availability
    - Booking appointments
    - Viewing upcoming appointments
    - Canceling appointments
    """

    def __init__(
        self,
        db: AsyncSession,
        user_id: int,
        workspace_id: uuid.UUID | None = None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        """Initialize CRM tools.

        Args:
            db: Database session
            user_id: User ID (agent owner) - integer matching Contact.user_id
            workspace_id: Workspace UUID for scoping contacts
            variables: Per-call lead data (leadName, leadEmail, tzName, company, ...) used
                       to fill the Cal.com attendee so the agent never has to ask for it.
        """
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.variables = variables or {}
        self.logger = logger.bind(
            component="crm_tools", user_id=user_id, workspace_id=str(workspace_id)
        )
        self._offered_slots: list[dict[str, str]] = []
        self._selected_slot_id: str | None = None
        self._selected_start: str | None = None
        self._normalized_timezone: str | None = None
        self._user_turn = 0
        self._offer_user_turn = 0
        self._latest_user_utterance = ""
        self._selection_user_turn = 0
        self._booking_attempts: list[dict[str, Any]] = []
        self._booking_completed: dict[str, Any] | None = None

    def _calcom_enabled(self) -> bool:
        """True when Cal.com is configured to back booking (else internal calendar)."""
        return bool(settings.CALCOM_API_KEY and settings.CALCOM_EVENT_TYPE_ID)

    def observe_user_utterance(self, text: str) -> None:
        """Observe one completed user transcript for transcript-bound slot selection."""
        self._user_turn += 1
        self._latest_user_utterance = text.strip()

    def get_booking_attempts(self) -> list[dict[str, Any]]:
        """Return a safe copy for later CallRecord persistence."""
        return deepcopy(self._booking_attempts)

    def _replace_offered_slots(self, slots: list[dict[str, str]], timezone: str) -> None:
        self._offered_slots = [
            {
                "slot_id": f"slot_{index}",
                "start": slot["start"],
                "label": slot["label"],
                "timezone": timezone,
            }
            for index, slot in enumerate(slots, start=1)
        ]
        self._normalized_timezone = timezone
        self._selected_slot_id = None
        self._selected_start = None
        self._selection_user_turn = 0
        self._offer_user_turn = self._user_turn
        self._booking_attempts.append(
            {
                "operation": "availability",
                "attempt": len(self._booking_attempts) + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "category": "offered" if slots else "empty",
                "timezone": timezone,
                "turn": self._user_turn,
                "slot_ids": [slot["slot_id"] for slot in self._offered_slots],
            }
        )

    @staticmethod
    def _canonical_start(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    def _utterance_slot_candidates(self) -> set[str]:
        """Conservatively infer the offered slot(s) named by the latest utterance."""
        from zoneinfo import ZoneInfo

        text = " ".join(self._latest_user_utterance.lower().split())
        ordinal_candidates: set[str] = set()
        if re.search(r"\b(first|earlier)\b", text) and self._offered_slots:
            ordinal_candidates.add(self._offered_slots[0]["slot_id"])
        if (
            re.search(r"\b(second|later)\b", text)
            and len(self._offered_slots) >= MAX_BOOKING_ATTEMPTS
        ):
            ordinal_candidates.add(self._offered_slots[1]["slot_id"])
        if ordinal_candidates:
            return ordinal_candidates

        time_matches: set[tuple[int, int]] = set()
        for match in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text):
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if 1 <= hour <= MAX_12_HOUR and minute <= MAX_MINUTE:
                hour = hour % MAX_12_HOUR + (MAX_12_HOUR if match.group(3) == "pm" else 0)
                time_matches.add((hour, minute))
        for match in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text):
            time_matches.add((int(match.group(1)), int(match.group(2))))
        word_hours = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
        }
        for word, hour in word_hours.items():
            if re.search(rf"\b{word}\b", text):
                # Spoken bare hours have no AM/PM. Match both halves of the day;
                # the offered-slot set must still reduce this to exactly one slot.
                time_matches.add((hour % MAX_12_HOUR, 0))
                time_matches.add((hour % MAX_12_HOUR + MAX_12_HOUR, 0))

        day_names = {
            name
            for name in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
            if re.search(rf"\b{name}\b", text)
        }
        if not time_matches and not day_names:
            return set()

        zone = ZoneInfo(self._normalized_timezone or "UTC")
        candidates: set[str] = set()
        for slot in self._offered_slots:
            start = self._canonical_start(slot["start"])
            if start is None:
                continue
            local_start = start.astimezone(zone)
            time_ok = not time_matches or (local_start.hour, local_start.minute) in time_matches
            day_ok = not day_names or local_start.strftime("%A").lower() in day_names
            if time_ok and day_ok:
                candidates.add(slot["slot_id"])
        return candidates

    async def select_slot(self, slot_id: str) -> dict[str, Any]:
        """Pin one offered slot only when the latest post-offer transcript agrees."""
        if not self._offered_slots:
            return {"success": False, "error": "slots_not_offered"}
        if self._user_turn <= max(self._offer_user_turn, self._selection_user_turn):
            return {
                "success": False,
                "error": "selection_not_heard",
                "message": "Ask whether they want the first time or the second.",
            }
        offered = {slot["slot_id"]: slot for slot in self._offered_slots}
        candidates = self._utterance_slot_candidates()
        if slot_id not in offered or candidates != {slot_id}:
            return {
                "success": False,
                "error": "ambiguous_slot_selection",
                "message": "Ask whether they want the first time or the second.",
            }
        selected = offered[slot_id]
        self._selected_slot_id = slot_id
        self._selected_start = selected["start"]
        self._selection_user_turn = self._user_turn
        self._booking_attempts.append(
            {
                "operation": "select",
                "attempt": len(self._booking_attempts) + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "category": "selected",
                "timezone": self._normalized_timezone,
                "turn": self._user_turn,
                "slot_id": slot_id,
                "selected_start": selected["start"],
            }
        )
        return {
            "success": True,
            "slot_id": slot_id,
            "start": selected["start"],
            "when": selected["label"],
        }

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        """Get OpenAI function calling tool definitions.

        Returns:
            List of tool definitions for GPT Realtime API (uses nested function format)
        """
        return [
            {
                "type": "function",
                "name": "search_customer",
                "description": "Search for a customer by phone number, email, or name",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Phone number, email, or name to search for",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "type": "function",
                "name": "create_contact",
                "description": "Create a new contact/customer in the CRM. REQUIRED: first_name and phone_number. OPTIONAL: last_name, email, company_name. Do NOT ask for optional fields unless the customer volunteers the information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "first_name": {
                            "type": "string",
                            "description": "REQUIRED. Customer's first name. Cannot be empty.",
                        },
                        "phone_number": {
                            "type": "string",
                            "description": "REQUIRED. Customer's phone number (7-20 digits). Format: digits only or E.164 format.",
                        },
                        "last_name": {
                            "type": "string",
                            "description": "OPTIONAL. Customer's last name. Only collect if volunteered.",
                        },
                        "email": {
                            "type": "string",
                            "description": "OPTIONAL. Customer's email address. Only collect if volunteered.",
                        },
                        "company_name": {
                            "type": "string",
                            "description": "OPTIONAL. Company or organization name. Only collect if volunteered.",
                        },
                    },
                    "required": ["first_name", "phone_number"],
                },
            },
            {
                "type": "function",
                "name": "check_availability",
                "description": "Get the next available appointment slots (already within business hours, on upcoming weekdays). Returns ready-to-offer openings - just offer two of them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "time_zone": {
                            "type": "string",
                            "description": "The lead's IANA timezone as they stated it (e.g. Europe/Stockholm, America/New_York). Slots are returned in this timezone.",
                        },
                        "date": {
                            "type": "string",
                            "description": "Optional preferred date (YYYY-MM-DD) if the lead asked for one.",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Desired appointment duration in minutes (default 30)",
                        },
                    },
                    "required": [],
                },
            },
            {
                "type": "function",
                "name": "select_slot",
                "description": "Select one of the latest offered slots after the lead clearly chooses it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slot_id": {
                            "type": "string",
                            "description": "Opaque slot ID returned by check_availability, such as slot_1 or slot_2.",
                        },
                    },
                    "required": ["slot_id"],
                },
            },
            {
                "type": "function",
                "name": "book_appointment",
                "description": (
                    "Book the selected appointment after select_slot succeeds and the ICP fit check "
                    "is captured. The attendee name and email on file are filled automatically; pass "
                    "email only if the lead volunteers a correction. Pass the exact selected start."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scheduled_at": {
                            "type": "string",
                            "description": "Chosen appointment start time in ISO 8601 format - use the exact 'start' value returned by check_availability.",
                        },
                        "email": {
                            "type": "string",
                            "description": "Optional corrected email volunteered by the lead. Otherwise the email on file is used silently.",
                        },
                        "icp": {
                            "type": "object",
                            "description": "REQUIRED. Quick fit-check captured on the call, in 2-3 brief questions.",
                            "properties": {
                                "offer_types": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "What they install/sell, e.g. ['rooftop C&I', 'carport', 'storage', 'resi-expanding', 'finance-PPA'].",
                                },
                                "min_kw": {
                                    "type": "number",
                                    "description": "Minimum project size in kW they'll take on.",
                                },
                                "states": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Target states / service area.",
                                },
                            },
                        },
                        "time_zone": {
                            "type": "string",
                            "description": "The lead's IANA timezone (e.g. Europe/Stockholm). Optional.",
                        },
                        "notes": {
                            "type": "string",
                            "description": "Notes for the team: write 'AUDIT: yes' or 'AUDIT: no' plus any context about their business.",
                        },
                        "contact_phone": {
                            "type": "string",
                            "description": "Optional - only used by the internal calendar fallback.",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Duration in minutes (default 30)",
                        },
                        "service_type": {
                            "type": "string",
                            "description": "Type of service/appointment",
                        },
                    },
                    "required": ["scheduled_at", "icp"],
                },
            },
            {
                "type": "function",
                "name": "list_appointments",
                "description": "List upcoming appointments, optionally filtered by date or contact",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_phone": {
                            "type": "string",
                            "description": "Filter by customer phone number",
                        },
                        "start_date": {
                            "type": "string",
                            "description": "Start date in YYYY-MM-DD format",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date in YYYY-MM-DD format",
                        },
                        "status": {
                            "type": "string",
                            "description": "Filter by status (scheduled, completed, cancelled, no_show)",
                        },
                    },
                    "required": [],
                },
            },
            {
                "type": "function",
                "name": "cancel_appointment",
                "description": "Cancel an existing appointment",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "integer",
                            "description": "Appointment ID to cancel",
                        },
                        "reason": {"type": "string", "description": "Cancellation reason"},
                    },
                    "required": ["appointment_id"],
                },
            },
            {
                "type": "function",
                "name": "reschedule_appointment",
                "description": "Reschedule an existing appointment to a new time",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "integer",
                            "description": "Appointment ID to reschedule",
                        },
                        "new_scheduled_at": {
                            "type": "string",
                            "description": "New appointment time in ISO 8601 format",
                        },
                    },
                    "required": ["appointment_id", "new_scheduled_at"],
                },
            },
        ]

    async def search_customer(self, query: str) -> dict[str, Any]:
        """Search for a customer by phone, email, or name.

        Args:
            query: Search query

        Returns:
            Customer information or error
        """
        try:
            # Search by phone, email, or name - filtered by workspace_id for proper scoping
            # Falls back to user_id if workspace_id not available (backward compatibility)
            # Also search full name (first + last) for queries like "John Smith"
            full_name = func.concat(Contact.first_name, " ", func.coalesce(Contact.last_name, ""))

            # Build base query with search conditions
            search_conditions = (
                (Contact.phone_number.ilike(f"%{query}%"))
                | (Contact.email.ilike(f"%{query}%"))
                | (Contact.first_name.ilike(f"%{query}%"))
                | (Contact.last_name.ilike(f"%{query}%"))
                | (full_name.ilike(f"%{query}%"))
            )

            # Scope by workspace if available, otherwise by user
            if self.workspace_id:
                stmt = select(Contact).where(
                    Contact.workspace_id == self.workspace_id,
                    search_conditions,
                )
            else:
                stmt = select(Contact).where(
                    Contact.user_id == self.user_id,
                    search_conditions,
                )

            result = await self.db.execute(stmt)
            contacts = list(result.scalars().all())

            if not contacts:
                return {
                    "success": True,
                    "found": False,
                    "message": f"No customer found matching '{query}'",
                }

            # Return first match (or all if multiple)
            customer_data = [
                {
                    "id": c.id,
                    "name": f"{c.first_name} {c.last_name or ''}".strip(),
                    "phone": c.phone_number,
                    "email": c.email,
                    "company": c.company_name,
                    "status": c.status,
                }
                for c in contacts[:3]  # Limit to 3 results
            ]

            return {
                "success": True,
                "found": True,
                "count": len(customer_data),
                "customers": customer_data,
            }

        except Exception as e:
            self.logger.exception("search_customer_failed", query=query, error=str(e))
            return {"success": False, "error": str(e)}

    async def create_contact(
        self,
        first_name: str,
        phone_number: str,
        last_name: str | None = None,
        email: str | None = None,
        company_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a new contact.

        Args:
            first_name: First name
            phone_number: Phone number
            last_name: Last name
            email: Email
            company_name: Company

        Returns:
            Created contact info
        """
        try:
            contact = Contact(
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                first_name=first_name,
                last_name=last_name,
                phone_number=phone_number,
                email=email,
                company_name=company_name,
                status="new",
            )

            self.db.add(contact)
            await self.db.commit()
            await self.db.refresh(contact)

            # Invalidate CRM caches so new contacts appear immediately in the UI
            try:
                await cache_invalidate(f"crm:contacts:list:{self.user_id}:*")
                await cache_invalidate("crm:stats:*")
                self.logger.debug("invalidated_crm_cache_after_create_contact")
            except Exception:
                self.logger.exception("failed_to_invalidate_cache_after_create_contact")

            return {
                "success": True,
                "contact_id": contact.id,
                "message": f"Created contact for {first_name} {last_name or ''}",
            }

        except Exception as e:
            self.logger.exception("create_contact_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def check_availability(  # noqa: PLR0912
        self,
        date: str | None = None,
        duration_minutes: int = 30,  # noqa: ARG002
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """Check available time slots.

        When Cal.com is configured, returns the next business-hours openings on
        upcoming weekdays in the lead's timezone (single source of truth, no
        double-book). Otherwise falls back to the internal calendar for `date`.

        Args:
            date: Optional preferred date (YYYY-MM-DD) - internal fallback only
            duration_minutes: Desired duration (reserved for future use)
            time_zone: The lead's IANA timezone for returned slots

        Returns:
            Available time slots
        """
        # --- Cal.com path (preferred) ---
        if self._calcom_enabled():
            from app.services.calcom_client import get_business_slots, normalize_timezone

            lead_tz = normalize_timezone(
                spoken=time_zone,
                fallback=self.variables.get("tzName"),
                team_default=settings.BOOKING_TEAM_TIMEZONE,
            )
            if lead_tz is None:
                self._offered_slots = []
                self._selected_slot_id = None
                self._selected_start = None
                self._normalized_timezone = None
                return {
                    "success": False,
                    "error": "timezone_unresolved",
                    "message": "Ask for their city once before checking the calendar.",
                }
            try:
                slots = await get_business_slots(lead_tz=lead_tz)
                self._replace_offered_slots(slots, lead_tz)
                if not slots:
                    return {
                        "success": True,
                        "slots": [],
                        "message": "No open business-hours slots in the next two weeks - ask the lead for a preferred day.",
                    }
                return {
                    "success": True,
                    "timezone": lead_tz,
                    "slots": [
                        {"slot_id": s["slot_id"], "when": s["label"], "start": s["start"]}
                        for s in self._offered_slots
                    ],
                    "message": "Offer these times, hear a clear choice, then call select_slot with its slot_id.",
                }
            except Exception as e:
                self._offered_slots = []
                self._selected_slot_id = None
                self._selected_start = None
                self.logger.exception("calcom_check_availability_failed", error=str(e))
                return {"success": False, "error": "calendar_unavailable"}

        # --- Internal calendar fallback ---
        try:
            # Default to tomorrow if no date given
            if date:
                target_date = datetime.strptime(date, "%Y-%m-%d").date()
            else:
                target_date = (datetime.now() + timedelta(days=1)).date()

            # Get existing appointments for that day - filtered by workspace or user
            base_stmt = (
                select(Appointment)
                .join(Contact)
                .where(
                    Appointment.scheduled_at >= datetime.combine(target_date, datetime.min.time()),
                    Appointment.scheduled_at < datetime.combine(target_date, datetime.max.time()),
                    Appointment.status == "scheduled",
                )
            )

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            result = await self.db.execute(stmt)
            booked_appointments = list(result.scalars().all())

            # Simple availability: 9 AM to 5 PM, hourly slots
            available_slots = []
            for hour in range(9, 17):  # 9 AM to 5 PM
                slot_time = datetime.combine(target_date, datetime.min.time()).replace(hour=hour)

                # Check if slot conflicts with existing appointments
                is_available = True
                for apt in booked_appointments:
                    if apt.scheduled_at.hour == hour:
                        is_available = False
                        break

                if is_available:
                    available_slots.append(slot_time.isoformat())

            return {
                "success": True,
                "date": date,
                "available_slots": available_slots,
                "total_available": len(available_slots),
            }

        except Exception as e:
            self.logger.exception("check_availability_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def book_appointment(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        scheduled_at: str,
        email: str | None = None,
        icp: dict[str, Any] | str | None = None,
        contact_phone: str | None = None,
        duration_minutes: int = 30,
        service_type: str | None = None,
        notes: str | None = None,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """Book an appointment.

        When Cal.com is configured, a current transcript-bound selected slot is
        mandatory. ICP comes from this call; email uses a volunteered correction or
        silently falls back to the seeded address on file.
        Otherwise falls back to the internal calendar (phone-based).

        Args:
            scheduled_at: ISO 8601 datetime (use the 'start' from check_availability)
            email: Optional corrected email volunteered on the call
            icp: Quick ICP fit-check captured on the call (Cal.com path: required)
            contact_phone: Customer phone (internal fallback only)
            duration_minutes: Duration
            service_type: Service type
            notes: Notes for the team (e.g. "AUDIT: yes" + context)
            time_zone: Lead's IANA timezone

        Returns:
            Booking confirmation
        """
        # --- Cal.com path (preferred) ---
        if self._calcom_enabled():
            del (
                time_zone
            )  # Deliberately ignored: booking must reuse the stored normalized timezone.
            if self._booking_completed is not None:
                return deepcopy(self._booking_completed)
            if not self._offered_slots:
                return {"success": False, "error": "slots_not_offered"}
            if not self._selected_start or not self._selected_slot_id:
                return {"success": False, "error": "slot_not_selected"}
            supplied_start = self._canonical_start(scheduled_at)
            selected_start = self._canonical_start(self._selected_start)
            if supplied_start is None or selected_start is None or supplied_start != selected_start:
                return {"success": False, "error": "slot_mismatch"}

            name = (self.variables.get("leadName") or "").strip() or "Guest"
            attendee_email = (email or "").strip() or str(
                self.variables.get("leadEmail") or ""
            ).strip()
            lead_tz = self._normalized_timezone or "UTC"

            if (
                not attendee_email
                or "{{" in attendee_email
                or "}}" in attendee_email
                or attendee_email.lower() in {"none", "null", "n/a", "unknown"}
            ):
                self.logger.warning("calcom_book_missing_email")
                return {
                    "success": False,
                    "error": "missing_email",
                    "message": "Ask for an email once, then call book_appointment again with it.",
                }
            if not icp:
                self.logger.warning("calcom_book_missing_icp")
                return {
                    "success": False,
                    "error": "missing_icp",
                    "message": "Ask the lead the quick fit questions (what they install, minimum project size in kW, target states) before booking, then call book_appointment again with icp filled in.",
                }

            icp_str = icp if isinstance(icp, str) else json.dumps(icp, ensure_ascii=False)
            full_notes = notes or ""
            if service_type:
                full_notes = f"{service_type}. {full_notes}".strip()
            full_notes = f"{full_notes}\nICP: {icp_str}".strip()
            try:
                from app.services.calcom_client import create_booking, find_existing_booking

                booking_result: dict[str, Any] = {}
                selected_attempts = [
                    attempt
                    for attempt in self._booking_attempts
                    if attempt.get("operation") == "create"
                    and attempt.get("selected_start") == self._selected_start
                ]
                prior_count = len(selected_attempts)
                if selected_attempts:
                    last_category = selected_attempts[-1].get("category")
                    if last_category == "rejected":
                        return {
                            "success": False,
                            "error": "booking_rejected",
                            "status_code": selected_attempts[-1].get("status_code"),
                        }
                    if last_category == "transient":
                        booking_result = await find_existing_booking(
                            start_iso=self._selected_start,
                            email=attendee_email,
                        )
                        self._booking_attempts.append(
                            {
                                "operation": "reconcile",
                                "attempt": len(self._booking_attempts) + 1,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "selected_start": self._selected_start,
                                "timezone": lead_tz,
                                "category": booking_result.get("category"),
                                "status_code": booking_result.get("status_code"),
                                "uid": booking_result.get("uid")
                                if booking_result.get("success")
                                else None,
                                "raw_body": str(booking_result.get("raw_body") or "")[:1000],
                            }
                        )
                        if booking_result.get("success"):
                            prior_count = MAX_BOOKING_ATTEMPTS
                        elif booking_result.get("category") == "reconcile_unavailable":
                            return {
                                "success": False,
                                "error": "booking_outcome_unknown",
                                "message": "The calendar response is uncertain. Do not try that time again; tell them the team will confirm by email.",
                            }
                        elif prior_count >= MAX_BOOKING_ATTEMPTS:
                            return {
                                "success": False,
                                "error": "booking_failed",
                                "message": "The calendar hiccuped - tell them you'll email to lock it in, then call end_call.",
                            }

                # Reconcile before the first POST as well as after an unknown POST.
                # This closes the process/session boundary: a repeated tool call after
                # reconnect or redeploy cannot create a second booking for the same
                # attendee and exact start.
                if prior_count == 0 and not booking_result:
                    booking_result = await find_existing_booking(
                        start_iso=self._selected_start,
                        email=attendee_email,
                    )
                    self._booking_attempts.append(
                        {
                            "operation": "reconcile",
                            "attempt": len(self._booking_attempts) + 1,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "selected_start": self._selected_start,
                            "timezone": lead_tz,
                            "category": booking_result.get("category"),
                            "status_code": booking_result.get("status_code"),
                            "uid": booking_result.get("uid")
                            if booking_result.get("success")
                            else None,
                            "raw_body": str(booking_result.get("raw_body") or "")[:1000],
                        }
                    )
                    if booking_result.get("success"):
                        prior_count = MAX_BOOKING_ATTEMPTS
                    elif booking_result.get("category") == "reconcile_unavailable":
                        return {
                            "success": False,
                            "error": "booking_outcome_unknown",
                            "message": "The calendar cannot safely verify this time. Do not retry it; tell them the team will confirm by email.",
                        }

                remaining_attempts = MAX_BOOKING_ATTEMPTS - prior_count
                for local_attempt in range(remaining_attempts):
                    booking_result = await create_booking(
                        start_iso=self._selected_start,
                        name=name,
                        email=attendee_email,
                        lead_tz=lead_tz,
                        notes=full_notes or None,
                    )
                    self._booking_attempts.append(
                        {
                            "operation": "create",
                            "attempt": len(self._booking_attempts) + 1,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "selected_start": self._selected_start,
                            "timezone": lead_tz,
                            "category": booking_result.get("category", "rejected"),
                            "status_code": booking_result.get("status_code"),
                            "uid": booking_result.get("uid")
                            if booking_result.get("success")
                            else None,
                            "raw_body": str(booking_result.get("raw_body") or "")[:1000],
                        }
                    )
                    if booking_result.get("category") != "transient":
                        break
                    reconciliation = await find_existing_booking(
                        start_iso=self._selected_start,
                        email=attendee_email,
                    )
                    self._booking_attempts.append(
                        {
                            "operation": "reconcile",
                            "attempt": len(self._booking_attempts) + 1,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "selected_start": self._selected_start,
                            "timezone": lead_tz,
                            "category": reconciliation.get("category"),
                            "status_code": reconciliation.get("status_code"),
                            "uid": reconciliation.get("uid")
                            if reconciliation.get("success")
                            else None,
                            "raw_body": str(reconciliation.get("raw_body") or "")[:1000],
                        }
                    )
                    if reconciliation.get("success"):
                        booking_result = reconciliation
                        break
                    if reconciliation.get("category") == "reconcile_unavailable":
                        booking_result = reconciliation
                        break
                    if local_attempt < remaining_attempts - 1:
                        await asyncio.sleep(0.1)
            except Exception as e:
                self.logger.exception("calcom_book_failed", error=str(e))
                return {"success": False, "error": "booking_failed"}
            if booking_result.get("success"):
                booking_id = booking_result.get("uid")
                schedule_fulfilment_webhook(
                    {
                        "booking_id": booking_id,
                        "name": name,
                        "company": self.variables.get("company"),
                        "email": attendee_email,
                        "phone": self.variables.get("leadPhone") or self.variables.get("phone"),
                        "icp": icp if isinstance(icp, dict) else icp_str,
                        "campaign_id": self.variables.get("campaign_id")
                        or self.variables.get("campaignId"),
                        "conversation_id": self.variables.get("conversation_id")
                        or self.variables.get("conversationId"),
                    }
                )
                self._selected_slot_id = None
                self._selected_start = None
                self._booking_completed = {
                    "success": True,
                    "message": (
                        "Booked - the invite is on its way to the lead. Now: confirm the time "
                        "back to them ONCE in a short line, give ONE warm goodbye, then call end_call."
                    ),
                    "uid": booking_id,
                }
                return deepcopy(self._booking_completed)
            if booking_result.get("category") == "conflict":
                from app.services.calcom_client import get_business_slots

                try:
                    fresh_slots = await get_business_slots(lead_tz=lead_tz)
                except Exception as e:
                    self._replace_offered_slots([], lead_tz)
                    self.logger.exception("calcom_conflict_refresh_failed", error=str(e))
                    return {"success": False, "error": "calendar_unavailable"}
                self._replace_offered_slots(fresh_slots, lead_tz)
                if not fresh_slots:
                    return {
                        "success": False,
                        "error": "calendar_unavailable",
                        "message": "The calendar has no current openings. End without booking.",
                    }
                return {
                    "success": False,
                    "error": "slot_conflict",
                    "slots": [
                        {"slot_id": s["slot_id"], "when": s["label"], "start": s["start"]}
                        for s in self._offered_slots
                    ],
                    "message": "That time was just taken. Offer these fresh times without choosing one automatically.",
                }
            if booking_result.get("category") == "rejected":
                return {
                    "success": False,
                    "error": "booking_rejected",
                    "status_code": booking_result.get("status_code"),
                }
            if booking_result.get("category") == "reconcile_unavailable":
                return {
                    "success": False,
                    "error": "booking_outcome_unknown",
                    "message": "The calendar response is uncertain. Do not try that time again; tell them the team will confirm by email.",
                }
            return {
                "success": False,
                "error": "booking_failed",
                "message": "The calendar hiccuped - tell them you'll email to lock it in, then call end_call.",
            }

        # --- Internal calendar fallback (phone-based) ---
        if not contact_phone:
            return {"success": False, "error": "contact_phone required for internal booking"}
        try:
            # Find contact - filtered by workspace or user for security
            if self.workspace_id:
                stmt = select(Contact).where(
                    Contact.workspace_id == self.workspace_id,
                    Contact.phone_number == contact_phone,
                )
            else:
                stmt = select(Contact).where(
                    Contact.user_id == self.user_id,
                    Contact.phone_number == contact_phone,
                )
            result = await self.db.execute(stmt)
            contact = result.scalar_one_or_none()

            if not contact:
                return {
                    "success": False,
                    "error": f"No contact found with phone {contact_phone}. Please create contact first.",
                }

            # Parse datetime and handle timezone
            appointment_time = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))

            # If datetime is naive (no timezone), interpret it in workspace timezone
            if appointment_time.tzinfo is None and self.workspace_id:
                from zoneinfo import ZoneInfo

                from app.models.workspace import Workspace

                # Get workspace timezone
                ws_result = await self.db.execute(
                    select(Workspace).where(Workspace.id == self.workspace_id)
                )
                workspace = ws_result.scalar_one_or_none()
                if workspace and workspace.settings:
                    tz_name = workspace.settings.get("timezone", "UTC")
                    try:
                        tz = ZoneInfo(tz_name)
                        # Interpret the naive datetime as being in workspace timezone
                        appointment_time = appointment_time.replace(tzinfo=tz)
                        self.logger.info(
                            "interpreted_naive_datetime",
                            original=scheduled_at,
                            timezone=tz_name,
                            result=appointment_time.isoformat(),
                        )
                    except Exception as tz_error:
                        self.logger.warning(
                            "timezone_conversion_failed",
                            timezone=tz_name,
                            error=str(tz_error),
                        )

            # Create appointment (inherit workspace_id from contact)
            appointment = Appointment(
                contact_id=contact.id,
                workspace_id=contact.workspace_id,
                scheduled_at=appointment_time,
                duration_minutes=duration_minutes,
                service_type=service_type,
                notes=notes,
                status="scheduled",
            )

            self.db.add(appointment)
            await self.db.commit()
            await self.db.refresh(appointment)

            # Invalidate CRM stats cache after booking
            try:
                await cache_invalidate("crm:stats:*")
                self.logger.debug("invalidated_crm_cache_after_book_appointment")
            except Exception:
                self.logger.exception("failed_to_invalidate_cache_after_book_appointment")

            return {
                "success": True,
                "appointment_id": appointment.id,
                "customer_name": f"{contact.first_name} {contact.last_name or ''}",
                "scheduled_at": appointment.scheduled_at.isoformat(),
                "duration_minutes": appointment.duration_minutes,
                "message": f"Appointment booked for {contact.first_name} on {appointment.scheduled_at.strftime('%B %d at %I:%M %p')}",
            }

        except Exception as e:
            self.logger.exception("book_appointment_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def list_appointments(
        self,
        contact_phone: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List appointments with optional filters.

        Args:
            contact_phone: Filter by phone
            start_date: Start date filter
            end_date: End date filter
            status: Status filter

        Returns:
            List of appointments
        """
        try:
            # Use selectinload to eagerly load contacts in a single query (fixes N+1)
            # Filter by workspace or user for security
            base_stmt = select(Appointment).join(Contact).options(selectinload(Appointment.contact))

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            # Apply filters
            if contact_phone:
                stmt = stmt.where(Contact.phone_number == contact_phone)

            if start_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                stmt = stmt.where(Appointment.scheduled_at >= start_dt)

            if end_date:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                stmt = stmt.where(Appointment.scheduled_at <= end_dt)

            if status:
                stmt = stmt.where(Appointment.status == status)
            else:
                stmt = stmt.where(Appointment.status == "scheduled")

            stmt = stmt.order_by(Appointment.scheduled_at)

            result = await self.db.execute(stmt)
            appointments = list(result.scalars().all())

            # Contact is already loaded via selectinload - no additional queries needed
            appointment_list = [
                {
                    "id": apt.id,
                    "customer_name": f"{apt.contact.first_name} {apt.contact.last_name or ''}",
                    "phone": apt.contact.phone_number,
                    "scheduled_at": apt.scheduled_at.isoformat(),
                    "duration_minutes": apt.duration_minutes,
                    "service_type": apt.service_type,
                    "status": apt.status,
                }
                for apt in appointments
            ]

            return {
                "success": True,
                "total": len(appointment_list),
                "appointments": appointment_list,
            }

        except Exception as e:
            self.logger.exception("list_appointments_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def cancel_appointment(
        self, appointment_id: int, reason: str | None = None
    ) -> dict[str, Any]:
        """Cancel an appointment.

        Args:
            appointment_id: Appointment ID
            reason: Cancellation reason

        Returns:
            Cancellation confirmation
        """
        try:
            # Verify appointment belongs to user's workspace/contact
            base_stmt = select(Appointment).join(Contact).where(Appointment.id == appointment_id)

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            result = await self.db.execute(stmt)
            appointment = result.scalar_one_or_none()

            if not appointment:
                return {
                    "success": False,
                    "error": f"Appointment {appointment_id} not found",
                }

            # Update status
            appointment.status = "cancelled"
            if reason:
                appointment.notes = (
                    f"{appointment.notes}\n\nCancellation reason: {reason}"
                    if appointment.notes
                    else f"Cancellation reason: {reason}"
                )

            await self.db.commit()

            return {
                "success": True,
                "appointment_id": appointment_id,
                "message": f"Appointment on {appointment.scheduled_at.strftime('%B %d at %I:%M %p')} has been cancelled",
            }

        except Exception as e:
            self.logger.exception("cancel_appointment_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def reschedule_appointment(
        self, appointment_id: int, new_scheduled_at: str
    ) -> dict[str, Any]:
        """Reschedule an appointment.

        Args:
            appointment_id: Appointment ID
            new_scheduled_at: New datetime in ISO 8601 format

        Returns:
            Reschedule confirmation
        """
        try:
            # Verify appointment belongs to user's workspace/contact
            base_stmt = select(Appointment).join(Contact).where(Appointment.id == appointment_id)

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            result = await self.db.execute(stmt)
            appointment = result.scalar_one_or_none()

            if not appointment:
                return {
                    "success": False,
                    "error": f"Appointment {appointment_id} not found",
                }

            # Parse new datetime
            new_time = datetime.fromisoformat(new_scheduled_at.replace("Z", "+00:00"))

            old_time = appointment.scheduled_at
            appointment.scheduled_at = new_time

            await self.db.commit()

            return {
                "success": True,
                "appointment_id": appointment_id,
                "old_time": old_time.strftime("%B %d at %I:%M %p"),
                "new_time": new_time.strftime("%B %d at %I:%M %p"),
                "message": f"Appointment rescheduled from {old_time.strftime('%B %d at %I:%M %p')} to {new_time.strftime('%B %d at %I:%M %p')}",
            }

        except Exception as e:
            self.logger.exception("reschedule_appointment_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def execute_tool(  # noqa: PLR0911
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a CRM tool by name.

        Args:
            tool_name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result
        """
        if tool_name == "search_customer":
            return await self.search_customer(**arguments)
        if tool_name == "create_contact":
            return await self.create_contact(**arguments)
        if tool_name == "check_availability":
            return await self.check_availability(**arguments)
        if tool_name == "select_slot":
            return await self.select_slot(**arguments)
        if tool_name == "book_appointment":
            return await self.book_appointment(**arguments)
        if tool_name == "list_appointments":
            return await self.list_appointments(**arguments)
        if tool_name == "cancel_appointment":
            return await self.cancel_appointment(**arguments)
        if tool_name == "reschedule_appointment":
            return await self.reschedule_appointment(**arguments)
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
