from django.urls import include, path, re_path

from .views import ReturnView, RedirectView, PollPendingView

event_patterns = [
    re_path(
        r"^xpay/",
        include(
            [
                path(
                    "redirect/<str:order>/<str:hash>/<str:payment>/",
                    RedirectView.as_view(),
                    name="redirect",
                ),
                path(
                    "return/<str:order>/<str:hash>/<str:payment>/<str:result>",
                    ReturnView.as_view(),
                    name="return",
                ),
                path( # Test purpose
                    "poll_pending_payments",
                    PollPendingView.as_view(),
                    name="poll_pending_payments",
                ),
            ]
        ),
    ),
]

