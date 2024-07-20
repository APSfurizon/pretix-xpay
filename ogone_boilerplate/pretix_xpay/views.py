import logging
from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _  # NoQA
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView
from django_scopes import scopes_disabled
from pretix.base.models import Event, Order, OrderPayment, Quota
from pretix.base.payment import PaymentException
from pretix.multidomain.urlreverse import eventreverse

from pretix_xpay.constants import (
    CANCELED_STATES,
    PENDING_STATES,
    SHA_OUT_PARAMETERS,
    SUCCESS_STATES,
)

logger = logging.getLogger(__name__)


class OgoneOrderView:
    @scopes_disabled()
    def dispatch(self, request, *args, **kwargs):
        try:
            if hasattr(request, "event"):
                event = request.event
            else:
                event = Event.objects.get(
                    slug=kwargs.get("event"),
                    organizer__slug=kwargs.get("organizer"),
                )

            if "hash" in kwargs:
                self.order = event.orders.get_with_secret_check(
                    code=kwargs["order"],
                    received_secret=kwargs["hash"],
                    tag="plugins:pretix_ogone",
                )
            else:
                self.order = event.orders.get(code=kwargs["order"])
        except Order.DoesNotExist:
            raise Http404("Unknown order")
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def pprov(self):
        return self.payment.payment_provider

    def validate_digest(self, data, prov):
        if "SHASIGN" in data:
            data = {k.upper(): v for k, v in data.items()}
            digest_in = data["SHASIGN"]
            digest = prov.hashalg()
            for parname in SHA_OUT_PARAMETERS:
                if parname in data and data[parname]:
                    digest.update(
                        f"{parname}={data[parname]}{prov.settings.sha_out_pass}".encode()
                    )
            return digest.hexdigest().upper() == digest_in.upper()
        return False

    @property
    def payment(self):
        return get_object_or_404(
            self.order.payments,
            pk=self.kwargs["payment"],
            provider__istartswith="ogone",
        )

    @transaction.atomic()
    def process_result(self, data, payment, prov):
        payment = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            return  # race condition
        payment.info_data = {**payment.info_data, **data}
        payment.save(update_fields=["info"])
        if data["STATUS"] == "5" and payment.state in (
            OrderPayment.PAYMENT_STATE_PENDING,
            OrderPayment.PAYMENT_STATE_CREATED,
        ):
            prov.capture_payment(payment)
        elif data["STATUS"] in PENDING_STATES and payment.state in (
            OrderPayment.PAYMENT_STATE_PENDING,
            OrderPayment.PAYMENT_STATE_CREATED,
        ):
            self.payment.state = OrderPayment.PAYMENT_STATE_PENDING
            self.payment.save()
        elif data["STATUS"] in CANCELED_STATES and payment.state in (
            OrderPayment.PAYMENT_STATE_PENDING,
            OrderPayment.PAYMENT_STATE_CREATED,
        ):
            self.payment.state = OrderPayment.PAYMENT_STATE_CANCELED
            self.payment.save()
        elif data["STATUS"] in SUCCESS_STATES and payment.state in (
            OrderPayment.PAYMENT_STATE_PENDING,
            OrderPayment.PAYMENT_STATE_CREATED,
        ):
            self.payment.confirm()
            self.order.refresh_from_db()
        else:
            raise PaymentException(f"Status {data['STATUS']} not successful")


@method_decorator(xframe_options_exempt, "dispatch")
class RedirectView(OgoneOrderView, TemplateView):
    template_name = "pretix_ogone/redirecting.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["url"] = self.pprov.settings.backend + "orderstandard_utf8.asp"
        ctx["params"] = self.pprov.sign_parameters(
            self.pprov.params_for_payment(self.payment, self.request),
        )
        return ctx


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(xframe_options_exempt, "dispatch")
class ReturnView(OgoneOrderView, View):
    def get(self, request, *args, **kwargs):
        return self._handle(request.GET.dict())

    def post(self, request, *args, **kwargs):
        return self._handle(request.POST.dict())

    def _handle(self, data):
        if not self.validate_digest(data, self.pprov):
            messages.error(
                self.request,
                _(
                    "Sorry, we could not validate the payment result. Please try again or "
                    "contact the event organizer to check if your payment was successful."
                ),
            )
            return self._redirect_to_order()

        if self.kwargs.get("result") == "cancel":
            self.payment.fail(
                info=dict(data.items()),
                log_data={"result": self.kwargs.get("result"), **dict(data.items())},
            )
            return self._redirect_to_order()
        elif self.kwargs.get("result") in ("decline", "exception"):
            self.payment.fail(
                info=dict(data.items()),
                log_data={"result": self.kwargs.get("result"), **dict(data.items())},
            )
            messages.error(
                self.request,
                _("The payment has failed. You can click below to try again."),
            )
            return self._redirect_to_order()
        elif self.kwargs.get("result") == "accept":
            try:
                self.process_result(data, self.payment, self.pprov)
            except Quota.QuotaExceededException as e:
                messages.error(self.request, str(e))
            except PaymentException as e:
                messages.error(
                    self.request,
                    _("The payment has failed. You can click below to try again."),
                )
                if self.payment.state in (
                    OrderPayment.PAYMENT_STATE_PENDING,
                    OrderPayment.PAYMENT_STATE_CREATED,
                ):
                    self.payment.fail(log_data={"exception": str(e)})

            return self._redirect_to_order()
        else:
            self.payment.fail(
                info=dict(data.items()),
                log_data={"result": self.kwargs.get("result"), **dict(data.items())},
            )
            messages.error(
                self.request,
                _("The payment has failed. You can click below to try again."),
            )

        return self._redirect_to_order()

    def _redirect_to_order(self):
        return redirect(
            eventreverse(
                self.request.event,
                "presale:event.order",
                kwargs={"order": self.order.code, "secret": self.order.secret},
            )
            + ("?paid=yes" if self.order.status == Order.STATUS_PAID else "")
        )


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(xframe_options_exempt, "dispatch")
class HookView(OgoneOrderView, View):
    def get(self, request, *args, **kwargs):
        return self._handle(request.GET.dict())

    def post(self, request, *args, **kwargs):
        return self._handle(request.POST.dict())

    def _handle(self, data):
        if not self.validate_digest(data, self.pprov):
            logger.warning(f"Invalid digest received from Ogone for data {data}")
            return HttpResponse("")

        self.order.log_action(
            "pretix_ogone.event",
            data=data,
        )
        try:
            self.process_result(data, self.payment, self.pprov)
        except Quota.QuotaExceededException:
            pass
        except PaymentException as e:
            if self.payment.state in (
                OrderPayment.PAYMENT_STATE_PENDING,
                OrderPayment.PAYMENT_STATE_CREATED,
            ):
                self.payment.fail(log_data={"exception": str(e)})
        return HttpResponse("")
