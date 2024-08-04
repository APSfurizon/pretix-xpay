import logging
import pretix_xpay.xpay_api as xpay
from datetime import timedelta
from django.http import Http404
from django.dispatch import receiver
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_scopes import scopes_disabled
from django.db import transaction
from pretix.base.models import OrderPayment, Order, Quota
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import (
    logentry_display,
    periodic_task,
    register_payment_providers,
)
from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.constants import XPAY_RESULT_AUTHORIZED, XPAY_RESULT_PENDING, XPAY_RESULT_RECORDED, XPAY_RESULT_REFUNDED, XPAY_RESULT_CANCELED
from pretix_xpay.utils import send_refund_needed_email, get_settings_object

logger = logging.getLogger(__name__)

@receiver(register_payment_providers, dispatch_uid="payment_xpay")
def register_payment_provider(sender, **kwargs):
    return [XPayPaymentProvider]

@receiver(signal=logentry_display, dispatch_uid="xpay_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith("pretix_xpay.event"):
        return
    return _("XPay reported an event (Status {status}).").format(status=logentry.parsed_data.get("STATUS", "?"))

@receiver(periodic_task, dispatch_uid="payment_xpay_periodic_poll")
@scopes_disabled()
def poll_pending_payments(sender, **kwargs):
    for payment in OrderPayment.objects.filter(provider="xpay", state__in=[OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED]):
        settings = get_settings_object(payment.order.event)
        mins = int(settings.poll_pending_timeout) if settings.poll_pending_timeout else 60

        if payment.order.status != Order.STATUS_EXPIRED and payment.order.status != Order.STATUS_PENDING:
            continue

        try:
            provider = payment.payment_provider
            data = xpay.get_order_status(payment=payment, provider=provider)

            if data.status in XPAY_RESULT_AUTHORIZED:
                xpay.confirm_payment_and_capture_from_preauth(payment, provider, payment.order)

            elif data.status in XPAY_RESULT_RECORDED:
                try:
                    payment.confirm()
                    logger.info(f"XPAY_poll_pending_payments [{payment.full_id}]: Payment confirmed with status {data.status}")
                except Quota.QuotaExceededException:
                    logger.info(f"XPAY_poll_pending_payments [{payment.full_id}]: Canceling payment quota was exceeded")
                    send_refund_needed_email(payment, origin="periodic_task.poll_pending_payments")

            elif data.status in XPAY_RESULT_PENDING:
                # If the payment it's still pending, weep waiting
                if(payment.state == OrderPayment.PAYMENT_STATE_CREATED):
                    with transaction.atomic():
                        logger.info(f"XPAY_poll_pending_payments [{payment.full_id}]: Payment is now pending")
                        payment.state = OrderPayment.PAYMENT_STATE_PENDING
                        payment.save(update_fields=["state"])

            elif data.status in XPAY_RESULT_REFUNDED or data.status in XPAY_RESULT_CANCELED:
                logger.info(f"XPAY_poll_pending_payments [{payment.full_id}]: Canceling payment because found in a refunded or canceled status: {data.status}")
                payment.fail(info={"error": str(_("Payment in refund or canceled state"))})

            else:
                logger.exception(f"XPAY_poll_pending_payments [{payment.full_id}]: Unrecognized payment status: {data.status}")

        except Http404 as e:
            if payment.order.status == Order.STATUS_EXPIRED and payment.created < now() - timedelta(minutes=mins):
                logger.exception(f"XPAY_poll_pending_payments [{payment.full_id}]: Setting payment status to fail due to expired order and poll_pending_timeout reached")
                payment.fail(log_data={"result": "poll_timeout"})

        except Exception as e:
            logger.exception(f"XPAY_poll_pending_payments [{payment.full_id}]: Exception in polling transaction status: {repr(e)}")

settings_hierarkey.add_default("payment_xpay_hash", "sha1", str)
settings_hierarkey.add_default("poll_pending_timeout", 60, int)
