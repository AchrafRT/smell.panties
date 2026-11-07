"""Smell Panties affiliate membership extension.

The standalone demo has a separate database and never uses payment credentials.
The host extension reuses the application's identity and verified billing hooks.
"""

from .runtime import AffiliateRuntime


def install(app, platform=None, *, billing_module=None, demo=False, db_path=None):
    if "affiliate_program" in app.extensions:
        return app.extensions["affiliate_program"]
    runtime = AffiliateRuntime(app, platform, billing_module=billing_module,
                               demo=demo, db_path=db_path)
    app.extensions["affiliate_program"] = runtime
    from . import core
    @app.context_processor
    def affiliate_program_context():
        return {"affiliate_program_settings": core.get_settings(runtime.db())}
    from .web import create_blueprint
    app.register_blueprint(create_blueprint(runtime))
    # Keep the existing owner functions and their authentication intact.
    if platform is not None and "admin_dashboard" in app.view_functions:
        legacy = app.view_functions["admin_dashboard"]
        app.add_url_rule("/admin/legacy", "affiliate_legacy_admin", legacy)
        app.view_functions["admin_dashboard"] = app.view_functions["affiliates.admin_page"]
    return runtime
