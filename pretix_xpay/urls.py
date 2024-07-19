from django.urls import include, path, re_path

from .views import HookView, RedirectView, ReturnView

event_patterns = [
    re_path(
        r"^ogone/",
        include(
            [
                path(
                    "redirect/<str:order>/<str:hash>/<str:payment>/",
                    RedirectView.as_view(),
                    name="redirect",
                ),
                path(
                    "return/<str:order>/<str:hash>/<str:payment>/<str:result>/",
                    ReturnView.as_view(),
                    name="return",
                ),
            ]
        ),
    ),
]


urlpatterns = [
    path(
        "_ogone/hook/<str:organizer>-<str:event>-<str:order>-<str:payment>/",
        HookView.as_view(),
        name="webhook",
    ),
]
