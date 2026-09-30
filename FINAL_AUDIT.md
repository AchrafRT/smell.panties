# Release audit — original-design version

This report replaces the audit supplied with the earlier MVP. The original desktop source was not modified.

## Verified

- 32 local tests passed: original account flows, ownership checks, CSRF, session revocation, order reservation/cancellation, checkout retries, shipping details, subscriptions and expiry, paid offers, signed callbacks, payment amount checking, duplicate notifications, refunds and review states.
- Tests mock provider APIs. No money was transferred and no live merchant account was contacted.
- Python compilation, Jinja template parsing, JavaScript syntax, Render configuration, upgrade of a copy of the supplied SQLite database and SQLite backup checks passed.
- Browser review confirmed the old storefront design and owner login. A mobile logo-glow overflow was corrected.
- Original logo images are byte-identical. Layout and visual assets were retained; checkout and owner payment pages reuse existing styles.

## Changes from the supplied version

Live mode now uses hosted payment adapters rather than granting free access. Provider callbacks must be authenticated and payment details independently retrieved before fulfillment. Subscriptions are explicit 30-day passes, not recurring billing. Replayed callbacks cannot extend access repeatedly. Canceled/expired orders cannot silently be fulfilled. Refunds and disputes enter review. Shipping addresses are collected for physical purchases. Suspended accounts and password changes revoke sessions. Creator-edited custom domains require owner intervention. Render storage settings and launch scripts were corrected. Backups and password recovery are available through owner maintenance commands.

## Release limits

This is a locally tested source release, not a certification or proof of live production readiness. Provider account approval, deployment, real transaction tests, TURN connectivity, creator identity verification, business policies and operations remain external setup. Stripe's published rules prohibit this business's adult-content/fetish transactions. See START_HERE.md before enabling any provider.

Creator payouts and refunds are external actions. Per-minute billing, automatic recurring subscriptions, cash wallet top-ups, regional tax calculation, email delivery/recovery and automated identity verification are not implemented. Promotional credits are retained from the original app. Old subscriptions without an expiry are preserved during migration and need owner review.

The code ZIP excludes databases, uploads, secrets, virtual environments and the obsolete free-demo blueprint. Sample creators are available only when explicitly seeded. See TEST_RESULTS.txt and VALIDATION.json for the checks performed.
