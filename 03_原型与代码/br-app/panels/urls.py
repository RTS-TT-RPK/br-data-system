from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("p/<str:panel_key>/", views.panel_table, name="panel_table"),
    path("p/<str:panel_key>/export.csv", views.panel_export, name="panel_export"),
    path("p/<str:panel_key>/new/", views.record_new, name="record_new"),
    path("p/<str:panel_key>/<int:pk>/", views.record_detail, name="record_detail"),
    path("p/<str:panel_key>/<int:pk>/edit/", views.record_edit, name="record_edit"),
    path("p/<str:panel_key>/<int:pk>/delete/", views.record_delete, name="record_delete"),
    path("ingestions/", views.ingestion_list, name="ingestion_list"),
    path("ingestions/simulate/", views.ingestion_simulate, name="ingestion_simulate"),
    path("ingestions/<int:pk>/", views.ingestion_review, name="ingestion_review"),
    path("ingestions/<int:pk>/approve/", views.ingestion_approve, name="ingestion_approve"),
    path("ingestions/<int:pk>/reject/", views.ingestion_reject, name="ingestion_reject"),
    path("components/", views.component_query, name="component_query"),
    path("query/", views.combo_query, name="combo_query"),
    path("data/query/", views.data_query, name="data_query"),
    path("data/query.xlsx", views.data_query_export, name="data_query_export"),
    path("analysis/incoming/", views.incoming_analysis, name="incoming_analysis"),
    path("analysis/incoming.xlsx", views.summary_report_export, name="incoming_analysis_export"),
    path("export/summary.xlsx", views.summary_report_export, name="summary_report_export"),
    path("api/health", views.api_health, name="api_health"),
    path("api/records", views.api_records, name="api_records"),
    path("api/analysis/incoming", views.api_analysis_incoming, name="api_analysis_incoming"),
    path("api/ingestions/<int:pk>", views.api_ingestion_detail, name="api_ingestion_detail"),
    path("api/panels", views.api_panels, name="api_panels"),
    path("api/components", views.api_components, name="api_components"),
    path("api/ingestions", views.api_ingestions, name="api_ingestions"),
]
