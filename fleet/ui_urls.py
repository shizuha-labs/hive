from django.urls import path
from . import ui_views

urlpatterns = [
    path('fleet/', ui_views.FleetListView.as_view(), name='fleet-home'),
    path('fleet', ui_views.FleetListView.as_view()),
    path('fleet/agents/new/', ui_views.FleetAgentFormView.as_view(), name='fleet-agent-new'),
    path('fleet/agents/new', ui_views.FleetAgentFormView.as_view()),
    path('fleet/agents/<uuid:agent_id>/', ui_views.FleetAgentDetailUIView.as_view(), name='fleet-agent-detail-ui'),
    path('fleet/agents/<uuid:agent_id>', ui_views.FleetAgentDetailUIView.as_view()),
    path('fleet/agents/<uuid:agent_id>/configure/', ui_views.FleetAgentConfigureUIView.as_view(), name='fleet-agent-configure-ui'),
    path('fleet/agents/<uuid:agent_id>/configure', ui_views.FleetAgentConfigureUIView.as_view()),
    path('fleet/agents/<uuid:agent_id>/switch-backend/', ui_views.FleetAgentSwitchBackendUIView.as_view(), name='fleet-agent-switch-backend-ui'),
    path('fleet/agents/<uuid:agent_id>/switch-backend', ui_views.FleetAgentSwitchBackendUIView.as_view()),
    path('fleet/agents/<uuid:agent_id>/enable/', ui_views.FleetAgentEnableUIView.as_view(), name='fleet-agent-enable-ui'),
    path('fleet/agents/<uuid:agent_id>/enable', ui_views.FleetAgentEnableUIView.as_view()),
    path('fleet/agents/<uuid:agent_id>/disable/', ui_views.FleetAgentDisableUIView.as_view(), name='fleet-agent-disable-ui'),
    path('fleet/agents/<uuid:agent_id>/disable', ui_views.FleetAgentDisableUIView.as_view()),
]
