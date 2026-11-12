"""
Account views, split by area. Every name stays importable from
apps.accounts.views, so URLconfs and tests are unaffected.
"""
from .auth import (  # noqa: F401
    AutoCompleteSocialSignup,
    SafePasswordResetView,
    _email_is_verified,
    login_view,
    logout_view,
    register_view,
)
from .profile import (  # noqa: F401
    access_history,
    _profile_form_for,
    change_password,
    consent_settings,
    data_export,
    delete_account,
    profile_edit,
    profile_view,
)
from .emergency import (  # noqa: F401
    _get_client_ip,
    emergency_card,
    emergency_card_public,
    revoke_emergency_token,
    toggle_emergency_card,
)
from .doctors import (  # noqa: F401
    approve_doctor_access,
    my_doctors,
    revoke_doctor_access,
)
from .sharing import (  # noqa: F401
    SHARING_PAGE,
    _apply_grant,
    _care_activity,
    _clean_scopes,
    _find_user,
    _name,
    _present_signal,
    accept_share,
    answer_share_request,
    cancel_share_request,
    create_share,
    decline_share,
    my_shares,
    request_share,
    revoke_share,
    shared_patient,
    shared_record,
)
