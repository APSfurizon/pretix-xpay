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

settings_hierarkey.add_default("payment_xpay_hash", "sha1", str)
