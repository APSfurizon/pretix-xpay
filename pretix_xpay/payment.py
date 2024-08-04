import json
import logging
import pretix_xpay.xpay_api as xpay
from collections import OrderedDict
from django import forms
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import Event, OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import eventreverse
from pretix_xpay.constants import TEST_URL, DOCS_TEST_CARDS_URL, HASH_TAG, XPAY_RESULT_AUTHORIZED, XPAY_RESULT_PENDING, XPAY_RESULT_RECORDED, XPAY_RESULT_REFUNDED, XPAY_RESULT_CANCELED
from pretix_xpay.utils import send_refund_needed_email, get_settings_object

logger = logging.getLogger(__name__)


class XPayPaymentProvider(BasePaymentProvider):
    identifier = "xpay"
    verbose_name = _("XPay")
    public_name = _("Pay through XPay")
    abort_pending_allowed = False
    execute_payment_needs_user = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = get_settings_object(event)
        self.event : Event = event
        

    @property
    def settings_form_fields(self):
        fields = [
            (
                "alias_key", # Will be used to identify the merchant during api calls
                forms.CharField(
                    label=_("XPay's Alias key"),
                    help_text=_(
                        'Check your backoffice area to recover the Alias value.'
                    ),
                )
            ),
            (
                "hash",
                forms.ChoiceField(
                    label=_("Mac's hash algorithm"),
                    choices=(
                        ("sha1", "SHA-1"),
                        ("sha256", "SHA-256"),
                    ),
                    help_text=_(
                        'By default it is set to SHA-1, contact XPay\'s support in order to use SHA-256.'
                    ),
                ),
            ),
            (
                "mac_secret_pass",
                SecretKeySettingsField(
                    label=_("Mac Secret"),
                    help_text=_(
                        'Check your backoffice area to recover the mac secret value. It is used to secure the hash'
                    ),
                ),
            ),
            (
                "poll_pending_timeout",
                forms.IntegerField(
                    label=_("Poll pending timeout (mins)"),
                    min_value = 1,
                    max_value = 50000000,
                    step_size = 1,
                    help_text=_(
                        'Pending and newly created payment orders are refreshed with regular intervals, to check if the user have actually paid, but left the process of returning back to pretix\'s pages.'
                        'This timeout specifies in how much time the payment should be considered over and should be marked as expired.'
                    ),
                ),
            ),
            (
                "payment_error_email", # Email address to send manual refund requests to
                forms.EmailField(
                    label=_("Failed payments email address"),
                    help_text=_(
                        'Enter an email address recipient for manual verification requests.'
                        'It might happen because of a failed refund request, or an already recorded payment.'
                    ),
                )
            ),
        ] + list(super().settings_form_fields.items())
        d = OrderedDict(fields)
        d.move_to_end("_enabled", last=False)
        return d
    
    @property
    def test_mode_message(self):
        if self.event.testmode:
            return _(
                f"The XPay plugin is operating in test mode. No money will actually be transferred, but BE SURE to check you're redirected to {TEST_URL}."
                f"You can use credit card and configurations avaible at {DOCS_TEST_CARDS_URL} for testing."
            )
        return None
    
    def cancel_payment(self, payment: OrderPayment):
        try:
            order_status = xpay.get_order_status(payment=payment, provider=self)

            if order_status.status in XPAY_RESULT_AUTHORIZED or order_status.status in XPAY_RESULT_PENDING:
                xpay.refund_preauth(payment, self)
                super().cancel_payment(payment)

            elif order_status.status in XPAY_RESULT_RECORDED:
                logger.info(f"XPAY_cancelPayment [{payment.full_id}]: Preauth payment was already settled!")
                payment.fail(info={"error": str(_("Tried canceling the payment, but the preauth was already settled. MANUAL REFUND NEEDED!"))})
                send_refund_needed_email(payment, origin="XPayPaymentProvider.cancel_payment")
                raise "Preauth payment was already settled"

            elif order_status.status in XPAY_RESULT_REFUNDED or order_status.status in XPAY_RESULT_CANCELED:
                logger.info(f"XPAY_cancelPayment [{payment.full_id}]: Payment was already in refunded or canceled state")
                super().cancel_payment(payment)

        except Exception as e:
            raise PaymentException(_("An error occurred while trying to cancel the payment %s: %s") % (payment.full_id, repr(e)))
        
    
    def payment_form_render(self, request) -> str: # Should return an explainatory paragraph
        template = get_template("pretix_xpay/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)
    
    def checkout_confirm_render(self, request) -> str: # (Mandatory to implement)
        template = get_template("pretix_xpay/checkout_payment_confirm.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings, "provider": self}
        return template.render(ctx)
    
    def payment_pending_render(self, request, payment) -> str: # Render customer-facing instructions on how to proceed with a pending payment
        template = get_template("pretix_xpay/pending.html")
        payment_info = json.loads(payment.info) if payment.info else None
        ctx = {"request": request, "event": self.event, "settings": self.settings, "provider": self, "order": payment.order, "payment": payment, "payment_info": payment_info}
        return template.render(ctx)

    def payment_control_render(self, request, payment) -> str: # It should return to admins HTML code containing information regarding the current payment status and, if applicable, next steps. NOT MANDATORY
        template = get_template("pretix_xpay/control.html")
        payment_info = json.loads(payment.info) if payment.info else None
        ctx = {"request": request, "event": self.event, "settings": self.settings, "payment_info": payment_info, "payment": payment, "provider": self}
        return template.render(ctx)

    def shred_payment_info(self, obj: OrderPayment):
       if not obj.info: return
       
       d = json.loads(obj.info)
       if "cognome" in d: d["cognome"] = "█"
       if "mail" in d: d["mail"] = "█"
       if "nome" in d: d["nome"] = "█"
       if "pan" in d: d["pan"] = "█"
       if "regione" in d: d["regione"] = "█"
       if "scadenza_pan" in d: d["scadenza_pan"] = "█"
       if "tipoProdotto" in d: d["tipoProdotto"] = "█"

       d["_shredded"] = True
       obj.info = json.dumps(d)
       obj.save(update_fields=["info"])

    
    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        return eventreverse(
            self.event,
            "plugins:pretix_xpay:redirect",
            kwargs={
                "order": payment.order.code,
                "payment": payment.pk,
                "hash": payment.order.tagged_secret(HASH_TAG),
            },
        )
    


    # Mandatory properties for the plugin to work
    @property
    def identifier(self):
        return "xpay"
    
    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return False

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return False
    
    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, None)
    
    def payment_is_valid_session(self, request: HttpRequest):
        return True
