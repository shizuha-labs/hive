from django.urls import path

from . import views

# JSON beacon endpoint (mounted under /hive/api/ and bare /api/).
urlpatterns = [
    path('v1/waitlist/', views.WaitlistSignupView.as_view(), name='waitlist-signup'),
    path('v1/waitlist', views.WaitlistSignupView.as_view()),
    path('v1/waitlist/event', views.WaitlistEventView.as_view(), name='waitlist-event'),
    path('v1/waitlist/event/', views.WaitlistEventView.as_view()),
]
