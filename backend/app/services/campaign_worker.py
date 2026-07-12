"""Campaign worker service for processing outbound calling campaigns.

This background worker:
1. Polls for running campaigns
2. Checks calling hours and concurrent call limits
3. Gets pending contacts and initiates outbound calls
4. Updates campaign contact status based on call outcomes
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytz  # type: ignore[import-untyped]
import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.settings import get_user_api_keys
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.campaign import (
    Campaign,
    CampaignContact,
    CampaignContactStatus,
    CampaignStatus,
)
from app.models.contact import Contact
from app.services.telephony.telnyx_service import TelnyxService, is_unknown_telnyx_dial_outcome
from app.services.telephony.twilio_service import TwilioService

logger = structlog.get_logger()

# Worker configuration
POLL_INTERVAL_SECONDS = 5  # How often to check for work
MAX_CALLS_PER_TICK = 10  # Maximum calls to initiate per poll cycle


class CampaignDialOutcomeUnknownError(Exception):
    """Telnyx may have accepted the call; automatic redial would risk duplication."""


class CampaignWorker:
    """Background worker for processing campaign outbound calls."""

    def __init__(self, base_url: str = "http://localhost:8000"):
        """Initialize the campaign worker.

        Args:
            base_url: Base URL for webhook callbacks (e.g., ngrok URL for development)
        """
        self.base_url = base_url.rstrip("/")
        self.running = False
        self.logger = logger.bind(component="campaign_worker")
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the campaign worker background task."""
        if self.running:
            self.logger.warning("Campaign worker already running")
            return

        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info("Campaign worker started", base_url=self.base_url)

    async def stop(self) -> None:
        """Stop the campaign worker."""
        self.running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.logger.info("Campaign worker stopped")

    async def _run_loop(self) -> None:
        """Main worker loop that polls for campaigns to process."""
        while self.running:
            try:
                await self._process_campaigns()
            except Exception:
                self.logger.exception("Error in campaign worker loop")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _process_campaigns(self) -> None:
        """Process all running campaigns."""
        async with AsyncSessionLocal() as db:
            # Find all running campaigns
            result = await db.execute(
                select(Campaign)
                .options(selectinload(Campaign.agent))
                .where(Campaign.status == CampaignStatus.RUNNING.value)
            )
            campaigns = result.scalars().all()

            if not campaigns:
                return

            self.logger.debug("Processing campaigns", count=len(campaigns))

            for campaign in campaigns:
                try:
                    await self._process_campaign(campaign, db)
                except Exception:
                    self.logger.exception(
                        "Error processing campaign",
                        campaign_id=str(campaign.id),
                        campaign_name=campaign.name,
                    )

    async def _process_campaign(  # noqa: PLR0912, PLR0915
        self, campaign: Campaign, db: AsyncSession
    ) -> None:
        """Process a single campaign.

        Args:
            campaign: Campaign to process
            db: Database session
        """
        log = self.logger.bind(
            campaign_id=str(campaign.id),
            campaign_name=campaign.name,
        )

        # Check if within calling hours
        if not self._is_within_calling_hours(campaign):
            log.debug("Outside calling hours")
            return

        # Check if campaign has ended
        if campaign.scheduled_end and datetime.now(UTC) > campaign.scheduled_end:
            log.info("Campaign scheduled end reached, completing")
            campaign.status = CampaignStatus.COMPLETED.value
            campaign.completed_at = datetime.now(UTC)
            await db.commit()
            return

        # Count active calls for this campaign
        active_result = await db.execute(
            select(func.count(CampaignContact.id)).where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.status == CampaignContactStatus.CALLING.value,
            )
        )
        active_calls = active_result.scalar() or 0

        # Calculate how many more calls we can make
        available_slots = campaign.max_concurrent_calls - active_calls
        if available_slots <= 0:
            log.debug("Max concurrent calls reached", active=active_calls)
            return

        # Rate limiting: respect calls_per_minute
        # Calculate how many calls we can make this tick based on rate
        calls_per_tick = min(
            campaign.calls_per_minute * POLL_INTERVAL_SECONDS / 60,
            MAX_CALLS_PER_TICK,
            available_slots,
        )
        calls_to_make = int(max(1, calls_per_tick))

        # Get pending contacts ready for calling with row-level locking
        # to prevent race conditions when multiple campaigns process simultaneously
        now = datetime.now(UTC)
        pending_result = await db.execute(
            select(CampaignContact)
            .options(selectinload(CampaignContact.contact))
            .where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.status == CampaignContactStatus.PENDING.value,
                CampaignContact.attempts < campaign.max_attempts_per_contact,
                or_(
                    CampaignContact.next_attempt_at.is_(None),
                    CampaignContact.next_attempt_at <= now,
                ),
            )
            .order_by(
                CampaignContact.priority.desc(),
                CampaignContact.created_at,
            )
            .limit(calls_to_make)
            .with_for_update(skip_locked=True)  # Prevent duplicate calls across workers
        )
        pending_contacts = pending_result.scalars().all()

        if not pending_contacts:
            # Check if all contacts are done
            remaining_result = await db.execute(
                select(func.count(CampaignContact.id)).where(
                    CampaignContact.campaign_id == campaign.id,
                    CampaignContact.status.in_(
                        [
                            CampaignContactStatus.PENDING.value,
                            CampaignContactStatus.CALLING.value,
                        ]
                    ),
                )
            )
            remaining = remaining_result.scalar() or 0

            if remaining == 0:
                log.info("All contacts processed, completing campaign")
                campaign.status = CampaignStatus.COMPLETED.value
                campaign.completed_at = datetime.now(UTC)
                await db.commit()

            return

        # Get telephony service for this campaign's user
        telephony_service = await self._get_telephony_service(campaign, db)
        if not telephony_service:
            log.warning("No telephony service configured for campaign")
            return

        # Initiate calls for each pending contact
        for campaign_contact in pending_contacts:
            contact = campaign_contact.contact
            if not contact or not contact.phone_number:
                log.warning(
                    "Contact missing phone number",
                    contact_id=campaign_contact.contact_id,
                )
                campaign_contact.status = CampaignContactStatus.SKIPPED.value
                campaign_contact.last_call_outcome = "missing_phone"
                continue

            try:
                await self._initiate_call(
                    campaign=campaign,
                    campaign_contact=campaign_contact,
                    contact=contact,
                    telephony_service=telephony_service,
                    db=db,
                )
            except CampaignDialOutcomeUnknownError:
                log.warning(
                    "Campaign dial outcome unknown; awaiting provider callback",
                    contact_id=contact.id,
                    phone=contact.phone_number,
                )
            except Exception:
                log.exception(
                    "Failed to initiate call",
                    contact_id=contact.id,
                    phone=contact.phone_number,
                )
                campaign_contact.last_call_outcome = "initiation_failed"

                # Check if we should retry
                if campaign_contact.attempts < campaign.max_attempts_per_contact:
                    # Schedule retry
                    campaign_contact.status = CampaignContactStatus.PENDING.value
                    campaign_contact.next_attempt_at = datetime.now(UTC) + timedelta(
                        minutes=campaign.retry_delay_minutes
                    )
                    log.info(
                        "Scheduling retry after initiation failure",
                        next_attempt=campaign_contact.next_attempt_at.isoformat(),
                    )
                else:
                    campaign_contact.status = CampaignContactStatus.FAILED.value
                    campaign.contacts_failed += 1

        await db.commit()

        # Close telephony service
        if hasattr(telephony_service, "close"):
            await telephony_service.close()

    def _is_within_calling_hours(self, campaign: Campaign) -> bool:
        """Check if current time is within campaign calling hours.

        Args:
            campaign: Campaign to check

        Returns:
            True if within calling hours, False otherwise
        """
        # If no calling hours configured, always allow
        if not campaign.calling_hours_start or not campaign.calling_hours_end:
            return True

        # Get current time in campaign timezone
        tz = pytz.timezone(campaign.timezone or "UTC")
        now = datetime.now(tz)

        # Check day of week (0=Monday, 6=Sunday)
        if campaign.calling_days and now.weekday() not in campaign.calling_days:
            return False

        # Check time
        current_time = now.time()
        return campaign.calling_hours_start <= current_time <= campaign.calling_hours_end

    async def _get_telephony_service(
        self, campaign: Campaign, db: AsyncSession
    ) -> TelnyxService | TwilioService | None:
        """Get telephony service for a campaign.

        Args:
            campaign: Campaign to get service for
            db: Database session

        Returns:
            Telephony service or None if not configured
        """
        # Get user API keys for the campaign's workspace
        user_settings = await get_user_api_keys(
            campaign.user_id,
            db,
            workspace_id=campaign.workspace_id,
        )

        # Resolve creds from workspace settings, then platform env (mirrors the API
        # resolvers so the worker also works from env creds).
        telnyx_key = (user_settings.telnyx_api_key if user_settings else None) or settings.TELNYX_API_KEY
        telnyx_pub = (
            user_settings.telnyx_public_key if user_settings else None
        ) or settings.TELNYX_PUBLIC_KEY
        twilio_sid = (
            user_settings.twilio_account_sid if user_settings else None
        ) or settings.TWILIO_ACCOUNT_SID
        twilio_tok = (
            user_settings.twilio_auth_token if user_settings else None
        ) or settings.TWILIO_AUTH_TOKEN

        def _telnyx() -> TelnyxService | None:
            return TelnyxService(api_key=telnyx_key, public_key=telnyx_pub) if telnyx_key else None

        def _twilio() -> TwilioService | None:
            return (
                TwilioService(account_sid=twilio_sid, auth_token=twilio_tok)
                if (twilio_sid and twilio_tok)
                else None
            )

        # Honour the outbound-provider gate; Telnyx stays dormant unless preferred/fallback.
        preferred = (settings.TELEPHONY_OUTBOUND_PROVIDER or "twilio").lower()
        if preferred == "telnyx":
            return _telnyx() or _twilio()
        return _twilio() or _telnyx()

    async def _initiate_call(
        self,
        campaign: Campaign,
        campaign_contact: CampaignContact,
        contact: Contact,
        telephony_service: TelnyxService | TwilioService,
        db: AsyncSession,
    ) -> None:
        """Initiate an outbound call for a campaign contact.

        Args:
            campaign: Campaign
            campaign_contact: Campaign contact record
            contact: Contact to call
            telephony_service: Telephony service
            db: Database session used to precommit durable call correlation
        """
        log = self.logger.bind(
            campaign_id=str(campaign.id),
            contact_id=contact.id,
            phone=contact.phone_number,
        )

        # Build webhook URL for when call is answered
        provider = "telnyx" if isinstance(telephony_service, TelnyxService) else "twilio"
        webhook_url = (
            f"{self.base_url}/webhooks/{provider}/answer"
            f"?agent_id={campaign.agent_id}"
            f"&workspace_id={campaign.workspace_id}"
            f"&campaign_id={campaign.id}"
            f"&campaign_contact_id={campaign_contact.id}"
        )

        log.info("Initiating campaign call", webhook_url=webhook_url)

        call_record = CallRecord(
            user_id=campaign.user_id,
            workspace_id=campaign.workspace_id,
            provider=provider,
            provider_call_id=f"pending:{uuid.uuid4()}",
            agent_id=campaign.agent_id,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND.value,
            status=CallStatus.INITIATED.value,
            from_number=campaign.from_phone_number,
            to_number=contact.phone_number,
        )
        db.add(call_record)

        # Mark CALLING in the same pre-dial commit so an immediate terminal callback
        # can lock and resolve the trusted campaign contact without racing this worker.
        campaign_contact.status = CampaignContactStatus.CALLING.value
        campaign_contact.attempts += 1
        campaign_contact.last_attempt_at = datetime.now(UTC)
        campaign_contact.last_call_outcome = CallStatus.INITIATED.value
        campaign.contacts_called += 1
        await db.commit()

        try:
            call_info = await telephony_service.initiate_call(
                to_number=contact.phone_number,
                from_number=campaign.from_phone_number,
                webhook_url=webhook_url,
                agent_id=str(campaign.agent_id),
            )
        except Exception as exc:
            if isinstance(telephony_service, TelnyxService) and is_unknown_telnyx_dial_outcome(exc):
                raise CampaignDialOutcomeUnknownError from exc
            # A definitive provider rejection never produced a call. Undo the
            # pre-dial metric reservation; the attempt itself remains counted.
            campaign.contacts_called = max(0, campaign.contacts_called - 1)
            locked = await db.execute(
                select(CallRecord)
                .where(CallRecord.id == call_record.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            failed_record = locked.scalar_one()
            failed_record.status = CallStatus.FAILED.value
            failed_record.ended_at = datetime.now(UTC)
            await db.commit()
            raise

        locked = await db.execute(
            select(CallRecord)
            .where(CallRecord.id == call_record.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        call_record = locked.scalar_one()
        call_record.provider_call_id = call_info.call_id
        await db.commit()

        log.info(
            "Campaign call initiated",
            call_id=call_info.call_id,
            status=call_info.status.value,
        )


# Global worker instance
_campaign_worker: CampaignWorker | None = None


async def start_campaign_worker(base_url: str = "http://localhost:8000") -> CampaignWorker:
    """Start the global campaign worker.

    Args:
        base_url: Base URL for webhook callbacks

    Returns:
        Campaign worker instance
    """
    global _campaign_worker
    if _campaign_worker is None:
        _campaign_worker = CampaignWorker(base_url=base_url)
        await _campaign_worker.start()
    return _campaign_worker


async def stop_campaign_worker() -> None:
    """Stop the global campaign worker."""
    global _campaign_worker
    if _campaign_worker:
        await _campaign_worker.stop()
        _campaign_worker = None


def get_campaign_worker() -> CampaignWorker | None:
    """Get the global campaign worker instance.

    Returns:
        Campaign worker or None if not started
    """
    return _campaign_worker
