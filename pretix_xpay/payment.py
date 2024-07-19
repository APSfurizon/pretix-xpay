import hashlib
import json
import logging
import requests
from collections import OrderedDict
from django import forms
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.functional import lazy
from django.utils.translation import gettext_lazy as _
from lxml import etree
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse

from pretix_xpay.constants import (
    PENDING_STATES,
    REFUND_OK_STATES,
    REFUNDABLE_STATES,
    SHA_IN_PARAMETERS,
    SUCCESS_STATES,
)

logger = logging.getLogger(__name__)


class OgoneSettingsHolder(BasePaymentProvider):
    identifier = "ogone"
    verbose_name = _("Ogone")
    is_enabled = False
    is_meta = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "ogone", event)

    @property
    def settings_form_fields(self):
        fields = [
            (
                "backend",
                forms.ChoiceField(
                    label="Payment backend",
                    choices=(
                        (
                            "https://secure.payengine.de/ncol/prod/",
                            "secure.payengine.de production",
                        ),
                        (
                            "https://secure.payengine.de/ncol/test/",
                            "secure.payengine.de test",
                        ),
                        (
                            "https://secure.ogone.com/ncol/prod/",
                            "secure.ogone.com production",
                        ),
                        (
                            "https://secure.ogone.com/ncol/test/",
                            "secure.ogone.com test",
                        ),
                        (
                            "https://ogone.test.v-psp.com/ncol/test/",
                            "ogone.test.v-psp.com test",
                        ),
                        # Possibly to add later if they still exist and someone needs them:
                        # https://mdepayments.epdq.co.uk/ncol/test
                        # https://mdepayments.epdq.co.uk/ncol/prod
                        # https://e-payment.postfinance.ch/ncol/test
                        # https://e-payment.postfinance.ch/ncol/prod
                        # https://viveum.v-psp.com/ncol/prod
                        # https://viveum.v-psp.com/ncol/prod
                    ),
                ),
            ),
            (
                "pspid",
                forms.CharField(
                    label="PSP ID",
                ),
            ),
            (
                "hash",
                forms.ChoiceField(
                    label=_("Hash algorithm"),
                    choices=(
                        ("sha1", "SHA-1"),
                        ("sha256", "SHA-256"),
                        ("sha512", "SHA-512"),
                    ),
                    help_text=_(
                        'Check at "Configuration", "Technical information", "Global security parameters" that '
                        "the Hash algorithm is set to the value configured here."
                    ),
                ),
            ),
            (
                "sha_in_pass",
                SecretKeySettingsField(
                    label=_("SHA-IN pass phrase"),
                    help_text=_(
                        'Can be found at "Configuration", "Technical information", "Data and origin verification".'
                    ),
                ),
            ),
            (
                "sha_out_pass",
                SecretKeySettingsField(
                    label=_("SHA-OUT pass phrase"),
                    help_text=_(
                        'Must be set at "Configuration", "Technical information", "Transaction feedback". Please also make '
                        'sure to set the option "I would like to receive transaction feedback parameters on the'
                        'redirection URLs."'
                    ),
                ),
            ),
            (
                "api_user_userid",
                forms.CharField(
                    label=_("API User ID"),
                    help_text=_(
                        "<a href='https://support.legacy.worldline-solutions.com/en/integration-solutions/integrations/"
                        "directlink#directlink_integration_guides_api_user'>Setup guide</a>"
                    ),
                ),
            ),
            (
                "api_user_password",
                SecretKeySettingsField(
                    label=_("API User password"),
                ),
            ),
            (
                "method_creditcard",
                forms.BooleanField(
                    label=_("Credit card"),
                    required=False,
                ),
            ),
            # (
            #     "method_postfinance_card",
            #     forms.BooleanField(
            #         label=_("PostFinance Card"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_eps",
            #     forms.BooleanField(
            #         label=_("EPS"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_amazon",
            #     forms.BooleanField(
            #         label=_("Amazon Checkout"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_giropay",
            #     forms.BooleanField(
            #         label=_("giropay"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_ideal",
            #     forms.BooleanField(
            #         label=_("iDEAL"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_paysafecard",
            #     forms.BooleanField(
            #         label=_("paysafecard"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_paypal",
            #     forms.BooleanField(
            #         label=_("PayPal"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_przelewy24",
            #     forms.BooleanField(
            #         label=_("Przelewy24"),
            #         required=False,
            #     ),
            # ),
            # (
            #     "method_postfinance",
            #     forms.BooleanField(
            #         label=_("PostFinance e-finance"),
            #         required=False,
            #     ),
            # ),
        ] + list(super().settings_form_fields.items())
        d = OrderedDict(fields)
        d.move_to_end("_enabled", last=False)
        return d


class OgoneMethod(BasePaymentProvider):
    method = ""
    abort_pending_allowed = False
    pm_value = "UNSET"

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "ogone", event)

    @property
    def test_mode_message(self):
        if "/test/" in self.settings.backend:
            return _(
                "The Ogone plugin is operating in test mode. No money will actually be transferred. You can use credit "
                "card 4111111111111111 for testing."
            )
        return None

    @property
    def settings_form_fields(self):
        return {}

    @property
    def identifier(self):
        return "ogone_{}".format(self.method)

    @property
    def is_enabled(self) -> bool:
        return self.settings.get("_enabled", as_type=bool) and self.settings.get(
            "method_{}".format(self.method), as_type=bool
        )

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, None)

    def payment_is_valid_session(self, request: HttpRequest):
        return True

    def payment_form_render(self, request) -> str:
        template = get_template("pretix_ogone/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_ogone/checkout_payment_confirm.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
        }
        return template.render(ctx)

    def payment_pending_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template("pretix_ogone/pending.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
            "order": payment.order,
            "payment": payment,
            "payment_info": payment_info,
        }
        return template.render(ctx)

    def payment_control_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template("pretix_ogone/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": payment_info,
            "payment": payment,
            "method": self.method,
            "provider": self,
        }
        return template.render(ctx)

    def shred_payment_info(self, obj: OrderPayment):
        if not obj.info:
            return
        d = json.loads(obj.info)
        if "details" in d:
            d["details"] = {k: "█" for k in d["details"].keys()}

        d["_shredded"] = True
        obj.info = json.dumps(d)
        obj.save(update_fields=["info"])

    @property
    def verbose_name(self) -> str:
        return lazy(
            lambda *args: _("{public_name} via Ogone").format(
                public_name=self.public_name,
            ),
            str,
        )()

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        return eventreverse(
            self.event,
            "plugins:pretix_ogone:redirect",
            kwargs={
                "order": payment.order.code,
                "payment": payment.pk,
                "hash": payment.order.tagged_secret("plugins:pretix_ogone"),
            },
        )

    def execute_refund(self, refund: OrderRefund):
        payment = refund.payment
        current_status = self._direct_api_call(
            "querydirect.asp",
            {
                "PAYID": payment.info_data["PAYID"],
                "PSPID": self.settings.pspid,
                "PSWD": self.settings.api_user_password,
                "USERID": self.settings.api_user_userid,
            },
        )
        if current_status["STATUS"] not in REFUNDABLE_STATES:
            raise PaymentException(
                _("Payment is not in correct status to be refunded.")
            )

        response = self._direct_api_call(
            "maintenancedirect.asp",
            {
                "AMOUNT": str(int(refund.amount * 100)),
                "OPERATION": (
                    "RFS" if payment.refunded_amount >= payment.amount else "RFD"
                ),
                "PAYID": payment.info_data["PAYID"],
                "PSPID": self.settings.pspid,
                "PSWD": self.settings.api_user_password,
                "USERID": self.settings.api_user_userid,
            },
        )
        refund.info_data = response
        refund.save()
        if response["STATUS"] in REFUND_OK_STATES:
            refund.done()
        else:
            raise PaymentException(
                _("Refund was not sucessful (error message: {error})").format(
                    error=response["STATUS"]
                    + " / "
                    + response.get("NCERROR")
                    + " "
                    + response.get("NCERRORPLUS", "")
                )
            )

    @property
    def hashalg(self):
        if self.settings.get("hash") == "sha1":
            return hashlib.sha1
        elif self.settings.get("hash") == "sha256":
            return hashlib.sha256
        return hashlib.sha512

    def sign_parameters(self, params: OrderedDict) -> OrderedDict:
        digest = self.hashalg()
        for parname in SHA_IN_PARAMETERS:
            if parname in params and params[parname]:
                digest.update(
                    f"{parname}={params[parname]}{self.settings.sha_in_pass}".encode()
                )
        params["SHASIGN"] = digest.hexdigest().upper()
        return params

    def params_for_payment(self, payment, request):
        supported_languages = {
            "ar_AR",
            "cs_CZ",
            "da_DK",
            "de_DE",
            "el_GR",
            "en_US",
            "es_ES",
            "fi_FI",
            "fr_FR",
            "he_IL",
            "hu_HU",
            "it_IT",
            "ja_JP",
            "ko_KR",
            "nl_BE",
            "nl_NL",
            "no_NO",
            "pl_PL",
            "pt_PT",
            "ru_RU",
            "se_SE",
            "sk_SK",
            "tr_TR",
            "zh_CN",
        }
        locale = "en_US"
        locale_parts = payment.order.locale.split("-")
        if len(locale_parts) > 1:
            if locale_parts[0] + "_" + locale_parts[1].upper() in supported_languages:
                locale = locale_parts[0] + "_" + locale_parts[1].upper()
            else:
                for language in supported_languages:
                    if language.startswith(locale_parts[0]):
                        locale = language

        hash = payment.order.tagged_secret("plugins:pretix_ogone")
        return {
            "PSPID": self.settings.get("pspid"),
            "ORDERID": payment.full_id,
            "OPERATION": "SAL",
            "AMOUNT": str(int(payment.amount * 100)),
            "CURRENCY": self.event.currency,
            "LANGUAGE": locale,
            "EMAIL": payment.order.email,
            "PM": self.pm_value,
            "PARAMVAR": f"{self.event.organizer.slug}-{self.event.slug}-{payment.order.code}-{payment.pk}",
            "ACCEPTURL": build_absolute_uri(
                self.event,
                "plugins:pretix_ogone:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment.pk,
                    "hash": hash,
                    "result": "accept",
                },
            ),
            "DECLINEURL": build_absolute_uri(
                self.event,
                "plugins:pretix_ogone:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment.pk,
                    "hash": hash,
                    "result": "decline",
                },
            ),
            "EXCEPTIONURL": build_absolute_uri(
                self.event,
                "plugins:pretix_ogone:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment.pk,
                    "hash": hash,
                    "result": "exception",
                },
            ),
            "CANCELURL": build_absolute_uri(
                self.event,
                "plugins:pretix_ogone:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment.pk,
                    "hash": hash,
                    "result": "cancel",
                },
            ),
        }

    def _direct_api_call(self, path, data):
        try:
            r = requests.post(
                f"{self.settings.backend}{path}",
                data=self.sign_parameters(data),
            )
            r.raise_for_status()
        except requests.RequestException:
            logger.exception("Could not reach ogone backend")
            raise PaymentException(_("Could not reach payment provider"))

        return dict(etree.fromstring(r.text).attrib)

    def capture_payment(self, payment):
        response = self._direct_api_call(
            "maintenancedirect.asp",
            {
                "AMOUNT": str(int(payment.amount * 100)),
                "OPERATION": "SAS",
                "PAYID": payment.info_data["PAYID"],
                "PSPID": self.settings.pspid,
                "PSWD": self.settings.api_user_password,
                "USERID": self.settings.api_user_userid,
            },
        )
        payment.info_data = {
            **payment.info_data,
            **response,
        }
        payment.save(update_fields=["info"])
        if response["STATUS"] in SUCCESS_STATES:
            payment.confirm()
        elif response["STATUS"] in PENDING_STATES:
            payment.state = OrderPayment.PAYMENT_STATE_PENDING
            payment.save()


class OgoneCreditCard(OgoneMethod):
    method = "creditcard"
    pm_value = "CreditCard"
    public_name = _("Credit card")


# The other payment methods are not active because their state handling has not been tested.
"""
class OgonePostfinanceCard(OgoneMethod):
    method = "postfinance_card"
    pm_value = "PostFinance Card"
    public_name = _("PostFinance Card")


class OgoneAmazon(OgoneMethod):
    method = "amazon"
    pm_value = "Amazon Checkout"
    public_name = _("Amazon Checkout")


class OgoneEPS(OgoneMethod):
    method = "eps"
    pm_value = "EPS"
    public_name = _("EPS")


class OgoneGiropay(OgoneMethod):
    method = "giropay"
    pm_value = "giropay"
    public_name = _("giropay")


class OgoneIdeal(OgoneMethod):
    method = "ideal"
    pm_value = "iDEAL"
    public_name = _("iDEAL")


class OgonePaysafecard(OgoneMethod):
    method = "paysafecard"
    pm_value = "paysafecard"
    public_name = _("paysafecard")


class OgonePrzelewy24(OgoneMethod):
    method = "przelewy24"
    pm_value = "Przelewy24"
    public_name = _("Przelewy24")


class OgonePostfinance(OgoneMethod):
    method = "postfinance"
    pm_value = "PostFinance e-finance"
    public_name = _("PostFinance e-finance")


class OgonePayPal(OgoneMethod):
    method = "paypal"
    pm_value = "PAYPAL"
    public_name = _("PayPal")
"""
