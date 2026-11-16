# Console screenshots

These are real screenshots of the console (`v1/`) running against a local control plane with
the seeded Northstar Labs demo account. They were taken at 1440x900 with device scale factor 2,
in the light theme unless noted. To regenerate them, run `make demo-console-screenshots`. See
[docs/console-demo.md](../../console-demo.md) for what is real and what is mocked.

- Landing / sign-in: [01-landing.png](01-landing.png)
- Onboarding and account switcher: [02-onboarding-account-switcher.png](02-onboarding-account-switcher.png)
- Dashboard (overview): [03-dashboard.png](03-dashboard.png)
- Dashboard, dark theme: [04-dashboard-dark.png](04-dashboard-dark.png)
- Deployments list: [05-deployments.png](05-deployments.png)
- Deployments list, 1800 px wide: [05b-deployments-wide.png](05b-deployments-wide.png)
- Deployment detail, ready: [06-deployment-detail.png](06-deployment-detail.png)
- Deployment detail, failed, Status tab: [07-deployment-detail-failed-status.png](07-deployment-detail-failed-status.png)
- New deployment form: [08-deployment-new.png](08-deployment-new.png)
- Inference stamps: [09-stamps.png](09-stamps.png)
- Stamp enrollment dialog: [10-stamps-enroll-dialog.png](10-stamps-enroll-dialog.png)
- Usage: [11-usage.png](11-usage.png)
- Audit log: [12-audit.png](12-audit.png)
- Audit log, filtered to "Deployment created": [13-audit-filtered.png](13-audit-filtered.png)
- Access, API keys: [14-access-api-keys.png](14-access-api-keys.png)
- Access, members: [15-access-members.png](15-access-members.png)
- Access, service principals: [16-access-service-principals.png](16-access-service-principals.png)
- Account settings: [18-settings.png](18-settings.png)
- Playground, side-by-side streamed answers: [19-playground.png](19-playground.png)
- Architecture explorer: [20-architecture.png](20-architecture.png)

There is no identity provider screenshot. In this console version, `/access/identity-provider`
redirects to Account settings.
