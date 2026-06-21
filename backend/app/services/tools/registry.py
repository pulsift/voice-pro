"""Tool registry for managing available tools for voice agents."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tools.calendly_tools import CalendlyTools
from app.services.tools.call_control_tools import CallControlTools
from app.services.tools.crm_tools import CRMTools
from app.services.tools.gohighlevel_tools import GoHighLevelTools
from app.services.tools.shopify_tools import ShopifyTools
from app.services.tools.sms_tools import TelnyxSMSTools, TwilioSMSTools


class ToolRegistry:
    """Registry of all available tools for voice agents.

    Manages:
    - Internal tools (CRM, bookings)
    - External integrations (GoHighLevel, Calendly, Shopify, SMS, etc.)
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
                         e.g., {"gohighlevel": {"access_token": "...", "location_id": "..."}}
            workspace_id: Workspace UUID for scoping CRM operations
        """
        self.db = db
        self.user_id = user_id
        self.integrations = integrations or {}
        self.workspace_id = workspace_id
        self.variables = variables or {}
        self.crm_tools = CRMTools(db, user_id, workspace_id=workspace_id, variables=self.variables)
        self._ghl_tools: GoHighLevelTools | None = None
        self._calendly_tools: CalendlyTools | None = None
        self._shopify_tools: ShopifyTools | None = None
        self._twilio_sms_tools: TwilioSMSTools | None = None
        self._telnyx_sms_tools: TelnyxSMSTools | None = None

    def _get_ghl_tools(self) -> GoHighLevelTools | None:
        """Get GoHighLevel tools if credentials are available."""
        if self._ghl_tools:
            return self._ghl_tools

        ghl_creds = self.integrations.get("gohighlevel")
        if ghl_creds and ghl_creds.get("access_token") and ghl_creds.get("location_id"):
            self._ghl_tools = GoHighLevelTools(
                access_token=ghl_creds["access_token"],
                location_id=ghl_creds["location_id"],
            )
            return self._ghl_tools

        return None

    def _get_calendly_tools(self) -> CalendlyTools | None:
        """Get Calendly tools if credentials are available."""
        if self._calendly_tools:
            return self._calendly_tools

        creds = self.integrations.get("calendly")
        if creds and creds.get("access_token"):
            self._calendly_tools = CalendlyTools(
                access_token=creds["access_token"],
            )
            return self._calendly_tools

        return None

    def _get_shopify_tools(self) -> ShopifyTools | None:
        """Get Shopify tools if credentials are available."""
        if self._shopify_tools:
            return self._shopify_tools

        creds = self.integrations.get("shopify")
        if creds and creds.get("access_token") and creds.get("shop_domain"):
            self._shopify_tools = ShopifyTools(
                access_token=creds["access_token"],
                shop_domain=creds["shop_domain"],
            )
            return self._shopify_tools

        return None

    def _get_twilio_sms_tools(self) -> TwilioSMSTools | None:
        """Get Twilio SMS tools if credentials are available."""
        if self._twilio_sms_tools:
            return self._twilio_sms_tools

        creds = self.integrations.get("twilio-sms")
        if (
            creds
            and creds.get("account_sid")
            and creds.get("auth_token")
            and creds.get("from_number")
        ):
            self._twilio_sms_tools = TwilioSMSTools(
                account_sid=creds["account_sid"],
                auth_token=creds["auth_token"],
                from_number=creds["from_number"],
            )
            return self._twilio_sms_tools

        return None

    def _get_telnyx_sms_tools(self) -> TelnyxSMSTools | None:
        """Get Telnyx SMS tools if credentials are available."""
        if self._telnyx_sms_tools:
            return self._telnyx_sms_tools

        creds = self.integrations.get("telnyx-sms")
        if creds and creds.get("api_key") and creds.get("from_number"):
            self._telnyx_sms_tools = TelnyxSMSTools(
                api_key=creds["api_key"],
                from_number=creds["from_number"],
                messaging_profile_id=creds.get("messaging_profile_id"),
            )
            return self._telnyx_sms_tools

        return None

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

        # GoHighLevel tools - available if "gohighlevel" is enabled and credentials exist
        if "gohighlevel" in enabled_tools and self._get_ghl_tools():
            ghl_tools = GoHighLevelTools.get_tool_definitions()
            tools.extend(filter_tools("gohighlevel", ghl_tools))

        # Calendly tools
        if "calendly" in enabled_tools and self._get_calendly_tools():
            calendly_tools = CalendlyTools.get_tool_definitions()
            tools.extend(filter_tools("calendly", calendly_tools))

        # Shopify tools
        if "shopify" in enabled_tools and self._get_shopify_tools():
            shopify_tools = ShopifyTools.get_tool_definitions()
            tools.extend(filter_tools("shopify", shopify_tools))

        # Twilio SMS tools
        if "twilio-sms" in enabled_tools and self._get_twilio_sms_tools():
            twilio_tools = TwilioSMSTools.get_tool_definitions()
            tools.extend(filter_tools("twilio-sms", twilio_tools))

        # Telnyx SMS tools
        if "telnyx-sms" in enabled_tools and self._get_telnyx_sms_tools():
            telnyx_tools = TelnyxSMSTools.get_tool_definitions()
            tools.extend(filter_tools("telnyx-sms", telnyx_tools))

        return tools

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
            "end_call",
            "transfer_call",
            "send_dtmf",
        }

        if tool_name in call_control_tool_names:
            return await CallControlTools.execute_tool(tool_name, arguments)

        # CRM tools
        crm_tool_names = {
            "search_customer",
            "create_contact",
            "check_availability",
            "book_appointment",
            "list_appointments",
            "cancel_appointment",
            "reschedule_appointment",
        }

        if tool_name in crm_tool_names:
            return await self.crm_tools.execute_tool(tool_name, arguments)

        # GoHighLevel tools
        ghl_tool_names = {
            "ghl_search_contact",
            "ghl_get_contact",
            "ghl_create_contact",
            "ghl_update_contact",
            "ghl_add_contact_tags",
            "ghl_get_calendars",
            "ghl_get_calendar_slots",
            "ghl_book_appointment",
            "ghl_get_appointments",
            "ghl_cancel_appointment",
            "ghl_get_pipelines",
            "ghl_create_opportunity",
        }

        if tool_name in ghl_tool_names:
            ghl_tools = self._get_ghl_tools()
            if not ghl_tools:
                return {
                    "success": False,
                    "error": "GoHighLevel integration not configured. Please add your API credentials.",
                }
            return await ghl_tools.execute_tool(tool_name, arguments)

        # Calendly tools
        calendly_tool_names = {
            "calendly_get_event_types",
            "calendly_get_availability",
            "calendly_create_scheduling_link",
            "calendly_list_events",
            "calendly_get_event",
            "calendly_cancel_event",
        }

        if tool_name in calendly_tool_names:
            calendly_tools = self._get_calendly_tools()
            if not calendly_tools:
                return {
                    "success": False,
                    "error": "Calendly integration not configured. Please add your API credentials.",
                }
            return await calendly_tools.execute_tool(tool_name, arguments)

        # Shopify tools
        shopify_tool_names = {
            "shopify_search_orders",
            "shopify_get_order",
            "shopify_get_order_tracking",
            "shopify_search_products",
            "shopify_check_inventory",
            "shopify_search_customers",
            "shopify_get_customer_orders",
        }

        if tool_name in shopify_tool_names:
            shopify_tools = self._get_shopify_tools()
            if not shopify_tools:
                return {
                    "success": False,
                    "error": "Shopify integration not configured. Please add your API credentials.",
                }
            return await shopify_tools.execute_tool(tool_name, arguments)

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
