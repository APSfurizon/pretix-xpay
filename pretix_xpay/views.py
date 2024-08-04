import logging
import pretix_xpay.xpay_api as xpay
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
from pretix_xpay.utils import get_settings_object
from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.constants import XPAY_STATUS_SUCCESS, XPAY_STATUS_FAILS, XPAY_STATUS_PENDING, HASH_TAG

PENDING_OR_CREATED_STATES = (OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED)

logger = logging.getLogger(__name__)

class XPayOrderView:
    @scopes_disabled()
    def dispatch(self, request, *args, **kwargs):
        try:
            event: Event = request.event if hasattr(request, "event") else Event.objects.get(slug=kwargs.get("event"), organizer__slug=kwargs.get("organizer"))
            self.order: Order = event.orders.get_with_secret_check(code=kwargs["order"], received_secret=kwargs["hash"], tag=HASH_TAG)
        except Order.DoesNotExist:
            raise Http404("Unknown order")
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def pprov(self) -> XPayPaymentProvider:
            return self.payment.payment_provider
    
    @property
    def payment(self) -> OrderPayment:
        return get_object_or_404(self.order.payments, pk=self.kwargs["payment"], provider__istartswith="xpay")

    # On success, return gracefully, otherwise throws a PaymentException
    def process_result(self, get_params: dict, payment: OrderPayment, provider: XPayPaymentProvider):
        with transaction.atomic():
            # Recover order payment
            payment = OrderPayment.objects.select_for_update().get(pk=payment.pk)

            if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                return  # race condition
            
            payment.info_data = {**payment.info_data, **get_params}
            payment.save(update_fields=["info"])

            if(get_params["esito"] in XPAY_STATUS_SUCCESS):
                pass # go to fallback. Yes, spaghetti code :D
            elif(get_params["esito"] in XPAY_STATUS_PENDING):
                logger.info(f"XPAY_order_process_result [{payment.full_id}]: Payment is now pending")
                messages.info(self.request, _("You payment is now pending. You will be notified either if the payment is confirmed or not."))
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                payment.save(update_fields=["state"])
                return
            elif(get_params["esito"] in XPAY_STATUS_FAILS):
                logger.info(f"XPAY_order_process_result [{payment.full_id}]: Payment is now failed")
                messages.error(self.request, _("The payment has failed. You can click below to try again."))
                payment.fail(info={"error": str(_("Payment result is in a failed status"))})
                return
            else:
                raise PaymentException("Unrecognized state.")

        # Fallback if payment is success
        xpay.confirm_payment_and_capture_from_preauth(payment, provider, self.order)
    
@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(xframe_options_exempt, "dispatch")
class ReturnView(XPayOrderView, View):
    def get(self, request: HttpRequest, *args, **kwargs):
        return self._handle(request.GET.dict())
        
    def _handle(self, data: dict):
        if self.kwargs.get("result") == "ko":
            logger.error(f"XPAY_return_handle [{self.payment.full_id}]: payment failed gracefully.")
            self.payment.fail(info=dict(data.items()), log_data={"result": self.kwargs.get("result"), **dict(data.items())} )
            messages.error(self.request, _("The payment has failed. You can click below to try again."))
            return self._redirect_to_order()
        
        
        elif self.kwargs.get("result") == "ok":
            if not xpay.return_page_validate_digest(self.request, self.pprov):
                logger.error(f"XPAY_return_handle [{self.payment.full_id}]: HMAC verification failed.")
                messages.error(self.request, _("Sorry, we could not validate the payment result. Please try again or contact the event organizer to check if your payment was successful."))
                return self._redirect_to_order()
            
            try:
                # On success, return gracefully, otherwise throws a PaymentException
                self.process_result(data, self.payment, self.pprov)
            except Quota.QuotaExceededException as e:
                logger.error(f"XPAY_return_handle [{self.payment.full_id}]: A QuotaExceededException occurred: {repr(e)}")
                messages.error(self.request, _("The was an availability error while confirming your order! A refund has been issued."))
            except PaymentException as e:
                logger.error(f"XPAY_return_handle [{self.payment.full_id}]: A PaymentException occurred: {repr(e)}")
                messages.error(self.request, _("The payment has failed. You can click below to try again. Details: %s") % repr(e))
                if self.payment.state in PENDING_OR_CREATED_STATES:
                    self.payment.fail(log_data={"exception": str(e)})

            return self._redirect_to_order()
        
        else:
            self.payment.fail(info=dict(data.items()), log_data={"result": self.kwargs.get("result"), **dict(data.items())} )
            messages.error(self.request, _("The payment has failed. You can click below to try again."))
            logger.error(f"XPAY_return_handle [{self.payment.full_id}]: The payment has failed due to an unknown result.")
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
    
@method_decorator(xframe_options_exempt, "dispatch")
class RedirectView(XPayOrderView, TemplateView):
    template_name = "pretix_xpay/redirecting.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["url"] = xpay.initialize_payment_get_url(self.pprov)
        ctx["params"] = xpay.initialize_payment_get_params(self.pprov, self.payment, kwargs["order"], kwargs["hash"], kwargs["payment"])
        return ctx
    



# These are for testing purpose

@method_decorator(xframe_options_exempt, "dispatch")
class PollPendingView(View):
    def get(self, request: HttpRequest, *args, **kwargs):
        from pretix_xpay.signals import poll_pending_payments
        event: Event = Event.objects.get(slug=kwargs.get("event"), organizer__slug=kwargs.get("organizer"))
        if event.testmode:
            settings = get_settings_object(event)
            if settings.enable_test_endpoints:
                logger.info(f"poll_pending_payments called.")
                poll_pending_payments(None)
                return HttpResponse("ok", content_type="text/plain")
        return HttpResponse("nope", content_type="text/plain")
@method_decorator(xframe_options_exempt, "dispatch")
class ManualRefundEmailView(XPayOrderView, View):
    def get(self, request: HttpRequest, *args, **kwargs):
        from pretix_xpay.utils import send_refund_needed_email
        if self.order.event.testmode:
            settings = get_settings_object(self.order.event)
            if settings.enable_test_endpoints:
                logger.info(f"test_manual_refund_email called with order: {self.order.code}")
                send_refund_needed_email(self.payment, origin="Testing! :3")
                return HttpResponse("ok", content_type="text/plain")
        return HttpResponse("nope", content_type="text/plain")