"""CRM tools for voice agents - bookings, contacts, appointments."""

import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.cache import cache_invalidate
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.contact import Contact

logger = structlog.get_logger()


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

    def _calcom_enabled(self) -> bool:
        """True when Cal.com is configured to back booking (else internal calendar)."""
        return bool(settings.CALCOM_API_KEY and settings.CALCOM_EVENT_TYPE_ID)

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
                "name": "book_appointment",
                "description": "Book the appointment. The attendee's name and email are filled automatically from the call - never ask for them. Pass the chosen start time (the exact 'start' value from check_availability).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scheduled_at": {
                            "type": "string",
                            "description": "Chosen appointment start time in ISO 8601 format - use the exact 'start' value returned by check_availability.",
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
                    "required": ["scheduled_at"],
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

    async def check_availability(
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
            lead_tz = time_zone or self.variables.get("tzName") or settings.BOOKING_TEAM_TIMEZONE
            try:
                from app.services.calcom_client import get_business_slots

                slots = await get_business_slots(lead_tz=lead_tz)
                if not slots:
                    return {
                        "success": True,
                        "slots": [],
                        "message": "No open business-hours slots in the next two weeks - ask the lead for a preferred day.",
                    }
                return {
                    "success": True,
                    "slots": [{"when": s["label"], "start": s["start"]} for s in slots],
                    "message": "Offer these two. Speak the 'when' naturally; pass the matching 'start' to book_appointment.",
                }
            except Exception as e:
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

    async def book_appointment(
        self,
        scheduled_at: str,
        contact_phone: str | None = None,
        duration_minutes: int = 30,
        service_type: str | None = None,
        notes: str | None = None,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """Book an appointment.

        When Cal.com is configured, books on Cal.com (the host's real calendar) with
        the attendee filled from per-call variables (leadName/leadEmail/tzName) so the
        agent never asks. Otherwise falls back to the internal calendar (phone-based).

        Args:
            scheduled_at: ISO 8601 datetime (use the 'start' from check_availability)
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
            name = (self.variables.get("leadName") or "").strip() or "Guest"
            email = (self.variables.get("leadEmail") or "").strip()
            lead_tz = time_zone or self.variables.get("tzName") or settings.BOOKING_TEAM_TIMEZONE
            if not email:
                self.logger.warning("calcom_book_no_email")
                return {"success": False, "error": "no_attendee_email"}
            full_notes = notes or ""
            if service_type:
                full_notes = f"{service_type}. {full_notes}".strip()
            try:
                from app.services.calcom_client import create_booking

                result = await create_booking(
                    start_iso=scheduled_at,
                    name=name,
                    email=email,
                    lead_tz=lead_tz,
                    notes=full_notes or None,
                )
            except Exception as e:
                self.logger.exception("calcom_book_failed", error=str(e))
                return {"success": False, "error": "booking_failed"}
            if result.get("success"):
                return {
                    "success": True,
                    "message": "Booked. The invite is on its way to the lead.",
                    "uid": result.get("uid"),
                }
            return {"success": False, "error": "booking_failed"}

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
        if tool_name == "book_appointment":
            return await self.book_appointment(**arguments)
        if tool_name == "list_appointments":
            return await self.list_appointments(**arguments)
        if tool_name == "cancel_appointment":
            return await self.cancel_appointment(**arguments)
        if tool_name == "reschedule_appointment":
            return await self.reschedule_appointment(**arguments)
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
