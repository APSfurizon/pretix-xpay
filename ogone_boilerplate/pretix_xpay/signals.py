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

from pretix_xpay.constants import PENDING_STATES

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="payment_ogone")
def register_payment_provider(sender, **kwargs):
    from .payment import OgoneCreditCard, OgoneSettingsHolder

    return [
        OgoneSettingsHolder,
        OgoneCreditCard,
    ]


@receiver(signal=logentry_display, dispatch_uid="ogone_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith("pretix_ogone.event"):
        return

    return _("Ogone reported an event (Status {status}).").format(
        status=logentry.parsed_data.get("STATUS", "?")
    )


@receiver(periodic_task, dispatch_uid="payment_ogone_periodic_poll")
@scopes_disabled()
def poll_pending_payments(sender, **kwargs):
    for op in OrderPayment.objects.filter(
        provider__startswith="ogone_",
        state=OrderPayment.PAYMENT_STATE_PENDING,
    ):
        if op.created < now() - timedelta(days=3):
            op.fail(
                log_data={"result": "poll_timeout"},
            )
            continue
        try:
            pprov = op.payment_provider
            data = pprov._direct_api_call(
                "querydirect.asp",
                {
                    "PAYID": op.info_data["PAYID"],
                    "PSPID": pprov.settings.pspid,
                    "PSWD": pprov.settings.api_user_password,
                    "USERID": pprov.settings.api_user_userid,
                },
            )
            op.info_data = {**op.info_data, **data}
            op.save(update_fields=["info"])
            if data["STATUS"] == "9":
                op.confirm()
            elif data["STATUS"] in PENDING_STATES:
                continue
            else:
                op.fail(
                    log_data=data,
                )
        except Exception:
            logger.exception("Could not poll transaction status")
            pass


settings_hierarkey.add_default("payment_ogone_method_creditcard", True, bool)
settings_hierarkey.add_default("payment_ogone_hash", "sha512", str)
