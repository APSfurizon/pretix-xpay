import logging
from datetime import timedelta
from django.dispatch import receiver
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import (
    logentry_display,
    periodic_task,
    register_payment_providers,
)

from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.xpay_api import get_order_status
from pretix_xpay.constants import XPAY_RESULT_AUTHORIZED, XPAY_RESULT_PENDING, XPAY_RESULT_RECORDED, XPAY_RESULT_REFUNDED

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
    for op in OrderPayment.objects.filter(provider__startswith="xpay_", state=OrderPayment.PAYMENT_STATE_PENDING):
        if op.created < now() - timedelta(days=3): #TODO: Is configurable timeout needed?
            op.fail(log_data={"result": "poll_timeout"})
            continue
        try:
            pprov = op.payment_provider
            data = get_order_status(payment=op, provider=pprov)
            if data.status in XPAY_RESULT_AUTHORIZED:
                pass
            elif data.status in XPAY_RESULT_RECORDED:
                pass
            elif data.status in XPAY_RESULT_PENDING:
                pass
            # OLD CODE
            if data["STATUS"] == "9":
                op.confirm()
            elif data["STATUS"] in PENDING_STATES:
                continue
            else:
                op.fail(log_data=data)
        except Exception:
            logger.exception("Could not poll transaction status")
            pass


settings_hierarkey.add_default("payment_xpay_hash", "sha1", str)
