import logging
from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponse, HttpRequest
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
from utils import encode_order_id, HASH_TAG

PENDING_OR_CREATED_STATES = (OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED)

logger = logging.getLogger(__name__)

class XPayOrderView:
    @scopes_disabled()
    def dispatch(self, request, *args, **kwargs):
        try:
            event: Event = request.event if hasattr(request, "event") else Event.objects.get(slug=kwargs.get("event"), organizer__slug=kwargs.get("organizer"))
            if "hash" in kwargs:
                self.order = event.orders.get_with_secret_check(code=kwargs["order"], received_secret=kwargs["hash"], tag=HASH_TAG)
            else:
                self.order = event.orders.gept(code=kwargs["order"])
        except Order.DoesNotExist:
            raise Http404("Unknown order")
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def pprov(self):
            return self.payment.payment_provider
    
    @property
    def payment(self):
        return get_object_or_404(self.order.payments, pk=self.kwargs["payment"], provider__istartswith="xpay")

    # On success, return gracefully, otherwise throws a PaymentException
    @transaction.atomic() # TODO: recover the order hash, compare it and fail if mismatch. Call /orders/<orderId> and handle order status
    def process_result(self, get_params, payment, prov):
        payment = OrderPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            return  # race condition
        





        # Old code
        payment.info_data = {**payment.info_data, **get_params}
        payment.save(update_fields=["info"])

        if data["STATUS"] == "5" and payment.state in PENDING_OR_CREATED_STATES:
            prov.capture_payment(payment)
        elif data["STATUS"] in PENDING_STATES and payment.state in PENDING_OR_CREATED_STATES:
            self.payment.state = OrderPayment.PAYMENT_STATE_PENDING
            self.payment.save()
        elif data["STATUS"] in CANCELED_STATES and payment.state in PENDING_OR_CREATED_STATES:
            self.payment.state = OrderPayment.PAYMENT_STATE_CANCELED
            self.payment.save()
        elif data["STATUS"] in SUCCESS_STATES and payment.state in PENDING_OR_CREATED_STATES:
            self.payment.confirm()
            self.order.refresh_from_db()
        else:
            raise PaymentException(f"Status {data['STATUS']} not successful")
    
@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(xframe_options_exempt, "dispatch")
class ReturnView(XPayOrderView, View):
    def get(self, request: HttpRequest, *args, **kwargs):
        return self._handle(request.GET.dict())
    
    #TODO: Maybe also the POST method has to be implemented
    
    def _handle(self, request: HttpRequest, get_params: dict):

        if self.kwargs.get("result") == "ko":
            self.payment.fail(info=dict(get_params.items()), log_data={"result": self.kwargs.get("result"), **dict(get_params.items())} )
            messages.error(self.request, _("The payment has failed. You can click below to try again."))
            return self._redirect_to_order()
        
        elif self.kwargs.get("result") == "ok":
            try:
                # On success, return gracefully, otherwise throws a PaymentException
                self.process_result(get_params, self.payment, self.pprov)
            except Quota.QuotaExceededException as e:
                messages.error(self.request, str(e))
            except PaymentException as e:
                messages.error(self.request, _("The payment has failed. You can click below to try again."))
                if self.payment.state in PENDING_OR_CREATED_STATES:
                    self.payment.fail(log_data={"exception": str(e)})

            return self._redirect_to_order()
        
        else:
            self.payment.fail(info=dict(get_params.items()), log_data={"result": self.kwargs.get("result"), **dict(get_params.items())} )
            messages.error(self.request, _("The payment has failed. You can click below to try again."))
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