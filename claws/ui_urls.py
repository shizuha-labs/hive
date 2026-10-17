from django.urls import path
from . import views

urlpatterns = [
    path('', views.HiveAssistantsListShellView.as_view(), name='hive-home'),
    path('login/', views.HiveLoginView.as_view(), name='hive-login'),
    path('login', views.HiveLoginView.as_view()),
    path('assistants/', views.HiveAssistantsListShellView.as_view(), name='assistants-list'),
    path('assistants', views.HiveAssistantsListShellView.as_view()),
    path('assistants/new/', views.HiveNewAssistantShellView.as_view(), name='assistant-new'),
    path('assistants/new', views.HiveNewAssistantShellView.as_view()),
    path('claw/', views.HiveClawLaunchShellView.as_view(), name='claw-launch'),
    path('claw', views.HiveClawLaunchShellView.as_view()),
    path('assistants/<uuid:claw_id>/', views.HiveAssistantShellView.as_view(), name='assistant-shell'),
    path('assistants/<uuid:claw_id>', views.HiveAssistantShellView.as_view()),
    path('assistants/<uuid:claw_id>/rename/', views.HiveAssistantRenameShellView.as_view(), name='assistant-rename'),
    path('assistants/<uuid:claw_id>/rename', views.HiveAssistantRenameShellView.as_view()),
    path('assistants/<uuid:claw_id>/data/', views.HiveAssistantShellView.as_view(), {'tab': 'data'}, name='assistant-data'),
    path('assistants/<uuid:claw_id>/data', views.HiveAssistantShellView.as_view(), {'tab': 'data'}),
    path('assistants/<uuid:claw_id>/dash/', views.HiveAssistantDashRedirectView.as_view(), name='assistant-dash'),
    path('assistants/<uuid:claw_id>/dash', views.HiveAssistantDashRedirectView.as_view()),
    path('assistants/<uuid:claw_id>/dashboard/', views.HiveAssistantShellView.as_view(), {'tab': 'dashboard'}, name='assistant-dashboard'),
    path('assistants/<uuid:claw_id>/dashboard', views.HiveAssistantShellView.as_view(), {'tab': 'dashboard'}),
    path('assistants/<uuid:claw_id>/<str:action>/', views.HiveAssistantActionView.as_view(), name='assistant-action'),
    path('assistants/<uuid:claw_id>/<str:action>', views.HiveAssistantActionView.as_view()),
]
