from django.urls import path

from . import views

# Public, server-rendered landing surface (mounted under /hive/ and bare /).
urlpatterns = [
    path('free-claw/', views.FreeClawLandingView.as_view(), name='free-claw-landing'),
    path('free-claw', views.FreeClawLandingView.as_view()),
]
