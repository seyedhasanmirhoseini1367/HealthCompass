from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = "accounts"

urlpatterns = [
    path("register/",        views.register_view,  name="register"),
    path("login/",           views.login_view,      name="login"),
    path("logout/",          views.logout_view,     name="logout"),
    path("profile/",         views.profile_view,    name="profile"),
    path("profile/edit/",    views.profile_edit,    name="profile_edit"),
    path("password/change/", views.change_password, name="change_password"),
    path("delete/",          views.delete_account,  name="delete_account"),
    path("consent/",                 views.consent_settings,       name="consent"),
    path("export/",                  views.data_export,            name="data_export"),
    # Patient control over who may read their records (NEW-05).
    path("my-doctors/",                    views.my_doctors,            name="my_doctors"),
    path("my-shares/",                     views.my_shares,             name="my_shares"),
    path("my-shares/create/",              views.create_share,          name="create_share"),
    path("my-shares/<int:pk>/revoke/",     views.revoke_share,          name="revoke_share"),
    path("shared/<int:pk>/",               views.shared_patient,        name="shared_patient"),
    path("shared/<int:pk>/record/<uuid:record_pk>/", views.shared_record, name="shared_record"),
    path("my-doctors/<int:pk>/approve/",   views.approve_doctor_access, name="approve_doctor_access"),
    path("my-doctors/<int:pk>/revoke/",    views.revoke_doctor_access,  name="revoke_doctor_access"),
    path("emergency-card/",          views.emergency_card,         name="emergency_card"),
    path("emergency-card/revoke/",   views.revoke_emergency_token, name="revoke_emergency_token"),
    path("emergency-card/toggle/",   views.toggle_emergency_card,  name="toggle_emergency_card"),
    path("emergency/<uuid:token>/",  views.emergency_card_public,  name="emergency_card_public"),

    # ── Password reset (Django built-in flow) ──────────────────────────────
    path("password/reset/",
         views.SafePasswordResetView.as_view(),
         name="password_reset"),

    path("password/reset/done/",
         auth_views.PasswordResetDoneView.as_view(
             template_name="accounts/password_reset_done.html",
         ),
         name="password_reset_done"),

    path("password/reset/<uidb64>/<token>/",
         auth_views.PasswordResetConfirmView.as_view(
             template_name="accounts/password_reset_confirm.html",
             success_url="/accounts/password/reset/complete/",
         ),
         name="password_reset_confirm"),

    path("password/reset/complete/",
         auth_views.PasswordResetCompleteView.as_view(
             template_name="accounts/password_reset_complete.html",
         ),
         name="password_reset_complete"),
]
