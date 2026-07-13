"""Call control tools for voice agents.

Provides function calling tools that allow the AI agent to control the call:
- End/hangup the call
- Transfer to another number or agent
- Send DTMF tones (for IVR navigation)
"""

from typing import Any

import structlog

logger = structlog.get_logger()


class CallControlTools:
    """Call control tools for voice agents.

    These tools allow the AI to control call flow. The actual execution
    is handled by the realtime session which has access to the telephony
    connection.

    Tools return a special "action" field that signals the realtime
    session to perform the telephony action.
    """

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        """Get OpenAI function calling tool definitions for call control.

        Returns:
            List of tool definitions in OpenAI function calling format
        """
        return [
            {
                "type": "function",
                "name": "wait_for_user",
                "description": (
                    "Call this when the latest audio does not need a spoken response, "
                    "such as silence, background noise, hold music, TV audio, side "
                    "conversation, or speech not addressed to you. Do not respond "
                    "conversationally after calling this tool - just wait for the "
                    "caller's next clear utterance."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "type": "function",
                "name": "end_call",
                "description": (
                    "End the current phone call. Use this when the conversation is complete, "
                    "the caller wants to hang up, or you've said goodbye. Always say a brief "
                    "farewell before calling this function."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": (
                                "Brief reason for ending the call (e.g., 'conversation_complete', "
                                "'caller_requested', 'no_response', 'transferred')"
                            ),
                        },
                    },
                    "required": ["reason"],
                },
            },
            {
                "type": "function",
                "name": "transfer_call",
                "description": (
                    "Transfer the call to another phone number or department. Use this when "
                    "the caller needs to speak with a human agent, a specialist, or another "
                    "department. Inform the caller before transferring."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": (
                                "Phone number to transfer to (E.164 format like +1234567890) "
                                "or a department identifier"
                            ),
                        },
                        "announce": {
                            "type": "string",
                            "description": (
                                "Optional message to announce to the destination before connecting "
                                "(e.g., 'Incoming transfer from customer support')"
                            ),
                        },
                    },
                    "required": ["destination"],
                },
            },
            {
                "type": "function",
                "name": "send_dtmf",
                "description": (
                    "Send DTMF touch-tones during the call. Use this for navigating automated "
                    "phone systems (IVR), entering PINs, extension numbers, or confirmation codes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "digits": {
                            "type": "string",
                            "description": (
                                "The DTMF digits to send. Valid characters: 0-9, *, #, A-D. "
                                "Use 'w' for a 0.5s pause between digits."
                            ),
                        },
                        "duration_ms": {
                            "type": "integer",
                            "description": "Duration of each tone in milliseconds (default: 250)",
                        },
                    },
                    "required": ["digits"],
                },
            },
        ]

    @staticmethod
    def _execute_wait_for_user(_arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute wait_for_user tool - a deliberate no-op.

        Gives the model a way to produce NO spoken output when the committed
        audio was noise/silence/side conversation. The session skips the usual
        post-tool response.create for this tool.
        """
        logger.info("wait_for_user_requested")
        return {"success": True, "message": "Waiting for the caller."}

    @staticmethod
    def _execute_end_call(arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute end_call tool."""
        reason = arguments.get("reason", "conversation_complete")
        logger.info("end_call_requested", reason=reason)
        return {
            "success": True,
            "action": "end_call",
            "reason": reason,
            "message": "Call will be ended after this response.",
        }

    @staticmethod
    def _execute_transfer_call(arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute transfer_call tool."""
        destination = arguments.get("destination", "")
        announce = arguments.get("announce")

        if not destination:
            return {"success": False, "error": "Destination is required for transfer"}

        logger.info("transfer_call_requested", destination=destination, announce=announce)
        return {
            "success": True,
            "action": "transfer_call",
            "destination": destination,
            "announce": announce,
            "message": f"Transferring call to {destination}.",
        }

    @staticmethod
    def _execute_send_dtmf(arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute send_dtmf tool."""
        digits = arguments.get("digits", "")
        duration_ms = arguments.get("duration_ms", 250)

        if not digits:
            return {"success": False, "error": "Digits are required for DTMF"}

        # Validate DTMF digits
        valid_chars = set("0123456789*#ABCDabcdwW")
        if not all(c in valid_chars for c in digits):
            return {
                "success": False,
                "error": "Invalid DTMF digits. Use 0-9, *, #, A-D, or 'w' for pause.",
            }

        logger.info("send_dtmf_requested", digits=digits, duration_ms=duration_ms)
        return {
            "success": True,
            "action": "send_dtmf",
            "digits": digits.upper().replace("W", "w"),  # Normalize
            "duration_ms": duration_ms,
            "message": f"Sending DTMF tones: {digits}",
        }

    @staticmethod
    async def execute_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a call control tool.

        These tools don't perform the action directly - they return a special
        response that signals the realtime session to perform the telephony action.

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments

        Returns:
            Dict with action type and parameters for the realtime session to handle
        """
        handlers = {
            "wait_for_user": CallControlTools._execute_wait_for_user,
            "end_call": CallControlTools._execute_end_call,
            "transfer_call": CallControlTools._execute_transfer_call,
            "send_dtmf": CallControlTools._execute_send_dtmf,
        }

        handler = handlers.get(tool_name)
        if handler:
            return handler(arguments)

        return {"success": False, "error": f"Unknown call control tool: {tool_name}"}
