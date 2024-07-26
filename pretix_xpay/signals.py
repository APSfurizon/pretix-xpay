import logging
from datetime import timedelta
from django.dispatch import receiver
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment, Order, Quota
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import (
    logentry_display,
    periodic_task,
    register_payment_providers,
)

from pretix_xpay.utils import confirm_payment_and_capture_from_preauth
from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.xpay_api import get_order_status
from pretix_xpay.constants import XPAY_RESULT_AUTHORIZED, XPAY_RESULT_PENDING, XPAY_RESULT_RECORDED, XPAY_RESULT_REFUNDED, XPAY_RESULT_CANCELED

logger = logging.getLogger(__name__)

@receiver(register_payment_providers, dispatch_uid="payment_xpay")
def register_payment_provider(sender, **kwargs):
    return [XPayPaymentProvider]

@receiver(signal=logentry_display, dispatch_uid="xpay_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith("pretix_xpay.event"):
        return
    return _("XPay reported an event (Status {status}).").format(status=logentry.parsed_data.get("STATUS", "?"))

# TODO: Periodically refresh pending events
@receiver(periodic_task, dispatch_uid="payment_xpay_periodic_poll")
@scopes_disabled()
def poll_pending_payments(sender, **kwargs):
    for payment in OrderPayment.objects.filter(provider__startswith="xpay_", state=[OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED]):
        if payment.created < now() - timedelta(days=3): #TODO: Is configurable timeout needed?
            payment.fail(log_data={"result": "poll_timeout"})
            continue
        if payment.order.status != Order.STATUS_EXPIRED and payment.order.status != Order.STATUS_PENDING:
            continue
        try:
            provider = payment.payment_provider
            data = get_order_status(payment=payment, provider=provider)

            if data.status in XPAY_RESULT_AUTHORIZED:
                confirm_payment_and_capture_from_preauth(payment, provider, payment.order)

            elif data.status in XPAY_RESULT_RECORDED:
                try:
                    payment.confirm()
                    logger.info(f"XPAY_periodic [{payment.full_id}]: Payment confirmed with status {data.status}")
                except Quota.QuotaExceededException:
                    logger.info(f"XPAY_periodic [{payment.full_id}]: Canceling payment quota was exceeded")
                    payment.fail(info=_("Tried confirming payment, but quota was exceeded. MANUAL REFUND NEEDED!"), log_data=data) #TODO; Check if manual fail() call is needed

            elif data.status in XPAY_RESULT_PENDING:
                # If the payment it's still pending, weep waiting
                if(payment.state == OrderPayment.PAYMENT_STATE_CREATED):
                    logger.info(f"XPAY_periodic [{payment.full_id}]: Payment is now pending")
                    payment.state = OrderPayment.PAYMENT_STATE_PENDING
                    payment.save()

            elif data.status in XPAY_RESULT_REFUNDED or data.status in XPAY_RESULT_CANCELED:
                logger.info(f"XPAY_periodic [{payment.full_id}]: Canceling payment because found in a refounded or canceled status: {data.status}")
                payment.fail(info=_("Payment in refund or canceled state"), log_data=data)

            else:
                logger.exception(f"XPAY_periodic [{payment.full_id}]: Unrecognized payment status: {data.status}")

        except Exception:
            logger.exception(f"XPAY_periodic [{payment.full_id}]: Could not poll transaction status")


settings_hierarkey.add_default("payment_xpay_hash", "sha1", str)
