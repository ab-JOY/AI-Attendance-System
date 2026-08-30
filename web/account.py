"""The administrator's own account settings."""

from __future__ import annotations

import logging

import mysql.connector
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from config.settings import settings as app_config
from infra.db import db_cursor
from repositories import credentials as credentials_repo
from security.access import role_required
from web.errors import error_page

logger = logging.getLogger(__name__)

account_bp = Blueprint("account", __name__)


@account_bp.route('/settings')
@role_required('admin')
def settings():

    admin_id = session.get('admin_id')

    if admin_id is None:
        # A session predating the admin_id change (R8). Falling back to
        # `SELECT ... LIMIT 1` would show whichever row the server felt like,
        # which is the defect rather than a graceful degradation.
        logger.error("settings reached with a session carrying no admin_id")
        return error_page(403)

    try:
        with db_cursor(dictionary=True) as cursor:
            admin = credentials_repo.admin_by_id(cursor, admin_id)

    except mysql.connector.Error:
        logger.exception("Could not load the administrator account")
        return error_page(500)

    if admin is None:
        logger.error("Signed-in administrator %s no longer exists", admin_id)
        return error_page(403)

    return render_template(
        'settings.html',
        admin=admin,
        # For the backup instruction on the page. Passed rather than written
        # into the template, so the command names the database this deployment
        # actually uses instead of whichever one the documentation assumed.
        database_name=app_config.db_name,
    )


@account_bp.route('/update_admin', methods=['POST'])
@role_required('admin')
def update_admin():

    username = request.form.get('username', '').strip()

    if not username:
        return error_page(400, "Username is required.")

    admin_id = session.get('admin_id')

    if admin_id is None:
        logger.error("update_admin reached with a session carrying no admin_id")
        return error_page(403)

    try:
        with db_cursor(commit=True) as cursor:
            credentials_repo.rename_admin(cursor, admin_id, username)

    except mysql.connector.Error:
        logger.exception("Could not rename administrator %s", admin_id)
        return error_page(500)

    logger.info("Administrator %s renamed to %r", admin_id, username)

    session['user'] = username

    flash("The administrator username was updated.", "success")

    return redirect(url_for('account.settings'))
