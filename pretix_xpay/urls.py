from django.urls import include, path, re_path

from .views import ReturnView

event_patterns = [
    re_path(
        r"^xpay/",
        include(
            [
                path(
                    "return/<str:order>/<str:hash>/<str:payment>/<str:result>",
                    ReturnView.as_view(),
                    name="return",
                ),
            ]
        ),
    ),
]

