"""
Signing in and out.

Rewritten for SE-1, SE-13 and SE-15 in Phase 2. Three things changed and each
matters on its own:

1. The password is no longer part of the query. It used to be
   `WHERE username=%s AND password=%s`, which let MySQL decide the match - and
   the column collation is utf8mb4_general_ci, case-insensitive and PAD SPACE,
   so `ADMIN` and `admin   ` both authenticated as `admin` (measured, SE-15).
   The row is fetched by identifier and the password is verified in Python
   against a bcrypt hash.
2. The *identifier* is re-checked in Python for the same reason. bcrypt fixes
   the password half of SE-15 and does nothing for the username half.
3. Consecutive failures are counted and the account is locked (SE-13).
"""

from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from config.settings import settings as app_config
from infra.db import db_cursor
from repositories import credentials as credentials_repo
from security.access import MUST_CHANGE_PASSWORD, authenticated, public
from security.passwords import (
    PasswordTooLongError,
    hash_password,
    is_known_default,
    verify_password,
)
from security.rate_limit import LoginRateLimiter
from web.errors import error_page

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

# ==============================
# LOGIN THROTTLING (SE-13)
# ==============================
login_rate_limiter = LoginRateLimiter(
    max_attempts=app_config.login_max_attempts,
    lockout_seconds=app_config.login_lockout_seconds,
    window_seconds=app_config.login_attempt_window_seconds,
)


@auth_bp.route('/')
@public
def home():
    return render_template('login.html')


@auth_bp.route('/login', methods=['POST'])
@public
def login():

    role = request.form.get('role', '')
    user_id = request.form.get('user_id', '')
    password = request.form.get('password', '')

    rate_limit_key = LoginRateLimiter.key(user_id, request.remote_addr)
    locked_for = login_rate_limiter.seconds_until_unlocked(rate_limit_key)

    if locked_for > 0:
        logger.warning(
            "Login refused: %r is locked out for another %d second(s)",
            user_id,
            int(locked_for),
        )
        return render_template(
            'login.html',
            error=(
                "Too many failed attempts. Try again in "
                f"{max(1, int(locked_for // 60))} minute(s)."
            ),
        ), 429

    account = None

    with db_cursor(dictionary=True) as cursor:
        if role == "admin":
            row = credentials_repo.admin_by_username(cursor, user_id)

            # The collation makes the lookup above case-insensitive too, so
            # the identifier is confirmed here rather than trusted.
            if row is not None and row['username'] == user_id:
                account = row

        elif role == "instructor":
            row = credentials_repo.instructor_by_id(cursor, user_id)

            if row is not None and row['instructor_id'] == user_id:
                account = row

    # SE-1/SE-15: verified in Python, outside the `with`. Keeping bcrypt off a
    # pooled connection matters here more than anywhere - /login is the one
    # route an unauthenticated attacker can call repeatedly, and holding one of
    # five connections for the duration of each hash would turn the rate
    # limiter's job into a denial-of-service opportunity.
    if account is None or not verify_password(password, account.get('password')):
        login_rate_limiter.record_failure(rate_limit_key)

        # Deliberately identical whether the account exists or not, so the
        # response cannot be used to enumerate usernames.
        logger.info("Failed login for %r as %r", user_id, role)

        return render_template(
            'login.html',
            error='Invalid Login Credentials'
        ), 401

    login_rate_limiter.record_success(rate_limit_key)

    session.clear()
    session.permanent = True

    if role == "admin":
        session['user'] = account['username']
        session['role'] = 'admin'
        # ⚠️ **Which** administrator, not just that one signed in (SE-16, R8).
        # Without this the account screens fall back to `admin WHERE id=1` and
        # `SELECT ... LIMIT 1`, so a second administrator would rename and
        # re-password row 1 while being shown row 1's name. The instructor
        # branch below has always carried its identity forward; the admin
        # branch did not, because the table happened to hold one row.
        session['admin_id'] = account['id']
    else:
        session['user'] = account['fullname']
        session['role'] = 'instructor'
        session['instructor_id'] = account['instructor_id']

    # Flagged in the database, or still using a credential this system ships
    # with. Either way the account can reach nothing but the change-password
    # page until it is dealt with (see security/access.py).
    must_change = bool(account.get('must_change_password')) or is_known_default(
        account.get('password')
    )

    if must_change:
        session[MUST_CHANGE_PASSWORD] = True
        logger.warning(
            "%r signed in with a credential that must be changed", user_id
        )
        return redirect(url_for('auth.change_password'))

    logger.info("Successful login for %r as %r", user_id, role)

    return redirect(url_for('dashboard.dashboard'))


@auth_bp.route('/logout')
@public
def logout():
    session.clear()
    return redirect(url_for('auth.home'))


# ==============================
# CHANGE PASSWORD
# ==============================
#
# SE-16. This route used to update `admin WHERE id=1` no matter who was signed
# in, so an instructor had no way to change their own password and the one form
# pointed at somebody else's account. That blocks the forced-change flow, which
# has to work for both roles.
#
# GET renders the form. That is not decoration: the access-control hook
# redirects an account flagged `must_change_password` here, and a POST-only
# route would answer that redirect with a 405.
#
# ⚠️ It lives in this blueprint rather than with the other account screens
# because `create_app()` names it as the hook's `change_password_endpoint`,
# beside `auth.home`. Two endpoints the access-control hook depends on, in one
# place.


def _credential_target():
    """
    (table, primary-key column, primary-key value) for the signed-in account.

    Returns None for a session with no recognised role, which the caller turns
    into a 403 rather than guessing. The same happens for a session carrying no
    key - see below.

    ⚠️ **The admin branch returned a literal `1` (R8).** Login authenticates any
    row in `admin` by username, but this handed every password change to row 1
    regardless of who was signed in. SE-16's write-up describes exactly that
    defect - "used to update `admin WHERE id=1` no matter who was signed in" -
    and the fix corrected the *role* handling while carrying the hardcoded key
    across. It is `session['admin_id']` now, set at login beside
    `instructor_id`, which is how the other branch has always worked.

    ⚠️ **A session created before this change has no `admin_id`**, so an
    administrator who was signed in across the deployment gets a 403 with the
    reason logged and has to sign in again. That is the correct failure: the
    alternative is falling back to row 1, which is the defect.
    """
    role = session.get('role')
    table = credentials_repo.CREDENTIAL_TABLES.get(role)

    if table is None:
        return None

    if role == 'admin':
        return table[0], table[1], session.get('admin_id')

    return table[0], table[1], session.get('instructor_id')


@auth_bp.route('/change_password', methods=['GET', 'POST'])
@authenticated
def change_password():

    target = _credential_target()

    if target is None or target[2] is None:
        logger.error(
            "change_password reached with an unusable session: role=%r",
            session.get('role'),
        )
        return error_page(403)

    table, key_column, key_value = target
    must_change = bool(session.get(MUST_CHANGE_PASSWORD))

    if request.method == 'GET':
        return render_template(
            'change_password.html',
            must_change=must_change
        )

    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    def form_error(message):
        return render_template(
            'change_password.html',
            must_change=must_change,
            error=message
        ), 400

    if new_password != confirm_password:
        return form_error("The new passwords do not match.")

    if len(new_password) < 8:
        return form_error("The new password must be at least 8 characters.")

    if is_known_default(new_password):
        return form_error(
            "That password ships with the system and is public knowledge. "
            "Choose a different one."
        )

    try:
        new_hash = hash_password(new_password)
    except PasswordTooLongError as error:
        return form_error(str(error))

    with db_cursor(dictionary=True, commit=True) as cursor:
        account = credentials_repo.password_hash(
            cursor, table, key_column, key_value
        )

        if account is None or not verify_password(
            current_password, account['password']
        ):
            logger.warning(
                "Rejected password change for %r: current password incorrect",
                session.get('user'),
            )
            return form_error("The current password is incorrect.")

        credentials_repo.set_password(
            cursor, table, key_column, key_value, new_hash
        )

    session.pop(MUST_CHANGE_PASSWORD, None)

    logger.info("Password changed for %r", session.get('user'))

    flash("Your password was changed.", "success")

    if session.get('role') == 'admin':
        return redirect(url_for('account.settings'))

    return redirect(url_for('dashboard.dashboard'))
