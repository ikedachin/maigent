from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("threads/<int:thread_id>/", views.dashboard, name="thread"),
    path("browse-directories/", views.browse_directories, name="browse_directories"),
    path("settings/rag/", views.update_rag_settings, name="update_rag_settings"),
    path("projects/", views.create_project, name="create_project"),
    path("projects/<int:project_id>/switch/", views.switch_project, name="switch_project"),
    path("projects/<int:project_id>/threads/", views.create_thread, name="create_thread"),
    path("projects/<int:project_id>/access-paths/", views.add_access_path, name="add_access_path"),
    path("access-paths/<int:access_path_id>/delete/", views.delete_access_path, name="delete_access_path"),
    path("threads/<int:thread_id>/delete/", views.delete_thread, name="delete_thread"),
    path("threads/<int:thread_id>/messages/", views.send_message, name="send_message"),
    path("threads/<int:thread_id>/messages/<int:message_id>/stream/", views.stream_message, name="stream_message"),
    path("threads/<int:thread_id>/approvals/", views.create_approval, name="create_approval"),
    path("approvals/<int:approval_id>/", views.approval_action, name="approval_action"),
]
