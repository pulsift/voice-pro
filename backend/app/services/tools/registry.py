"""Tool registry for managing available tools for voice agents."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tools.call_control_tools import CallControlTools
from app.services.tools.crm_tools import CRMTools
from app.services.tools.sms_tools import TelnyxSMSTools, TwilioSMSTools


class ToolRegistry:
    """Registry of all available tools for voice agents.

    Manages:
    - Internal tools (CRM, bookings)
    - External integrations (SMS)
    - Tool execution routing
    """

    def __init__(
        self,
        db: AsyncSession,
        user_id: int,
        integrations: dict[str, dict[str, Any]] | None = None,
        workspace_id: Any | None = None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        """Initialize tool registry.

        Args:
            db: Database session
            user_id: User ID (integer matching users.id)
            integrations: Dict of integration credentials keyed by integration_id
            workspace_id: Workspace UUID for scoping CRM operations
        """
        self.db = db
        self.user_id = user_id
        self.integrations = integrations or {}
        self.workspace_id = workspace_id
        self.variables = variables or {}
        self.crm_tools = CRMTools(db, user_id, workspace_id=workspace_id, variables=self.variables)
        # Per-CALL, like crm_tools: end_call counts how many times it has been
        # asked, so the second goodbye can be refused rather than invited.
        self.call_control_tools = CallControlTools()
        self._twilio_sms_tools: TwilioSMSTools | None = None
        self._telnyx_sms_tools: TelnyxSMSTools | None = None

    def get_all_tool_definitions(
        self,
        enabled_tools: list[str],
        enabled_tool_ids: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Get tool definitions for enabled tools.

        Args:
            enabled_tools: List of enabled integration IDs (legacy)
            enabled_tool_ids: Granular tool selection {integration_id: [tool_id1, tool_id2]}

        Returns:
            List of OpenAI function calling tool definitions
        """
        tools: list[dict[str, Any]] = []

        # Helper to filter tools by enabled_tool_ids
        def filter_tools(
            integration_id: str, all_tools: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            """Filter tools based on enabled_tool_ids if provided."""
            if not enabled_tool_ids or integration_id not in enabled_tool_ids:
                # No granular filtering - return all tools (backward compatible)
                return all_tools

            allowed_tool_ids = set(enabled_tool_ids[integration_id])
            return [
                tool
                for tool in all_tools
                if tool.get("name") in allowed_tool_ids
                or tool.get("function", {}).get("name") in allowed_tool_ids
            ]

        # Call Control tools - always available if "call_control" is enabled
        if "call_control" in enabled_tools:
            call_control_tools = CallControlTools.get_tool_definitions()
            tools.extend(filter_tools("call_control", call_control_tools))

        # Internal CRM tools - always available if "crm" is enabled
        if "crm" in enabled_tools:
            crm_tools = CRMTools.get_tool_definitions()
            tools.extend(filter_tools("crm", crm_tools))

        # Internal Bookings tools - also from CRM but filtered separately
        if "bookings" in enabled_tools:
            booking_tools = CRMTools.get_tool_definitions()
            tools.extend(filter_tools("bookings", booking_tools))

        # Twilio SMS tools
        if "twilio-sms" in enabled_tools and self._get_twilio_sms_tools():
            twilio_tools = TwilioSMSTools.get_tool_definitions()
            tools.extend(filter_tools("twilio-sms", twilio_tools))

        # Telnyx SMS tools
        if "telnyx-sms" in enabled_tools and self._get_telnyx_sms_tools():
            telnyx_tools = TelnyxSMSTools.get_tool_definitions()
            tools.extend(filter_tools("telnyx-sms", telnyx_tools))

        return tools

    def observe_user_utterance(self, text: str) -> None:
        """Forward a completed caller utterance to the per-call CRM state."""
        self.crm_tools.observe_user_utterance(text)

    def observe_assistant_utterance(self, text: str) -> None:
        """Forward what the agent just said, so a reply can be read in context."""
        self.crm_tools.observe_assistant_utterance(text)

    def get_booking_attempts(self) -> list[dict[str, Any]]:
        """Return sanitized booking diagnostics from the per-call CRM state."""
        return self.crm_tools.get_booking_attempts()

    def get_fit_answers(self) -> dict[str, Any]:
        """Return the fit answers captured so far, independent of booking."""
        return self.crm_tools.get_fit_answers()

    async def wait_for_calendar_writes(self) -> int:
        """Let THIS call's detached calendar writes finish before teardown."""
        from app.services.tools.crm_tools import wait_for_calendar_writes

        return await wait_for_calendar_writes(tasks=self.crm_tools.calendar_writes)

    async def execute_tool(  # noqa: PLR0911
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a tool by routing to appropriate handler.

        Args:
            tool_name: Tool name
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        # Call Control tools
        call_control_tool_names = {
            "wait_for_user",
            "end_call",
            "transfer_call",
            "send_dtmf",
        }

        if tool_name in call_control_tool_names:
            return await self.call_control_tools.execute_tool(tool_name, arguments)

        # CRM tools
        crm_tool_names = {
            "search_customer",
            "create_contact",
            "check_availability",
            "refresh_availability",
            "select_slot",
            "record_fit_answers",
            "book_appointment",
            "list_appointments",
            "cancel_appointment",
            "reschedule_appointment",
        }

        if tool_name in crm_tool_names:
            return await self.crm_tools.execute_tool(tool_name, arguments)

        # Twilio SMS tools
        twilio_tool_names = {
            "twilio_send_sms",
            "twilio_get_message_status",
        }

        if tool_name in twilio_tool_names:
            twilio_tools = self._get_twilio_sms_tools()
            if not twilio_tools:
                return {
                    "success": False,
                    "error": "Twilio SMS integration not configured. Please add your API credentials.",
                }
            return await twilio_tools.execute_tool(tool_name, arguments)

        # Telnyx SMS tools
        telnyx_tool_names = {
            "telnyx_send_sms",
            "telnyx_get_message_status",
        }

        if tool_name in telnyx_tool_names:
            telnyx_tools = self._get_telnyx_sms_tools()
            if not telnyx_tools:
                return {
                    "success": False,
                    "error": "Telnyx SMS integration not configured. Please add your API credentials.",
                }
            return await telnyx_tools.execute_tool(tool_name, arguments)

        # Unknown tool
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    async def close(self) -> None:
        """Clean up resources."""
        if self._ghl_tools:
            await self._ghl_tools.close()
        if self._calendly_tools:
            await self._calendly_tools.close()
        if self._shopify_tools:
            await self._shopify_tools.close()
        if self._twilio_sms_tools:
            await self._twilio_sms_tools.close()
        if self._telnyx_sms_tools:
            await self._telnyx_sms_tools.close()
