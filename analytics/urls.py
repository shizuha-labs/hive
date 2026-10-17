from django.urls import path

from .views import (
    AnalyticsIngestView,
    FeedbackListView,
    FeedbackSupportIngestView,
    FeedbackWidgetView,
)

urlpatterns = [
    path('analytics/events', AnalyticsIngestView.as_view(), name='analytics-ingest'),
    # HIVE-394: feedback intake (≥2 channels → feedback_event).
    path('feedback/widget', FeedbackWidgetView.as_view(), name='feedback-widget'),
    path('feedback/support', FeedbackSupportIngestView.as_view(), name='feedback-support'),
    path('feedback', FeedbackListView.as_view(), name='feedback-list'),
]
