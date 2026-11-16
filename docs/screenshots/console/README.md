# Console screenshots

These are real screenshots of the console (`v1/`) running against a local control plane with
the seeded Northstar Labs demo account. They were taken at 1440x900 with device scale factor 2,
in the light theme unless noted. To regenerate them, run `make demo-console-screenshots`. See
[docs/console-demo.md](../../console-demo.md) for what is real and what is mocked.

- Landing / sign-in: [01-landing.png](01-landing.png)
- Onboarding, choosing between named accounts: [02-onboarding-account-switcher.png](02-onboarding-account-switcher.png)
- Header account switcher, open: [02b-account-switcher-menu.png](02b-account-switcher-menu.png)
- Dashboard (overview), with the edge gateway card: [03-dashboard.png](03-dashboard.png)
- Dashboard, dark theme: [04-dashboard-dark.png](04-dashboard-dark.png)
- Deployments list: [05-deployments.png](05-deployments.png)
- Deployments list in a 1100 px window, scrolling inside its card: [05b-deployments-narrow.png](05b-deployments-narrow.png)
- Deployment detail, ready: [06-deployment-detail.png](06-deployment-detail.png)
- Deployment detail, Runtime and Routing cards: [06b-deployment-detail-routing.png](06b-deployment-detail-routing.png)
- Deployment Logs, the first deployment on a fresh machine pulling the model runtime image: [06c-deployment-logs-first-pull.png](06c-deployment-logs-first-pull.png)
- Deployment Logs, weights downloading with measured progress: [06d-deployment-logs-downloading.png](06d-deployment-logs-downloading.png)
- Deployment Logs, a ready host's log searched: [06e-deployment-logs-search.png](06e-deployment-logs-search.png)
- Deployment detail, failed, Status tab: [07-deployment-detail-failed-status.png](07-deployment-detail-failed-status.png)
- Deployment Logs, failed start-up with a diagnosis and hint: [07b-deployment-logs-failed.png](07b-deployment-logs-failed.png)
- Deployment whose stamp was removed, with Redeploy: [07c-deployment-stamp-removed.png](07c-deployment-stamp-removed.png)
- New deployment form: [08-deployment-new.png](08-deployment-new.png)
- Create-deployment drawer, Routing section filled in: [08b-deployment-drawer-routing.png](08b-deployment-drawer-routing.png)
- Inference stamps, with the edge gateway card: [09-stamps.png](09-stamps.png)
- Inference stamps with removed stamps shown: [09b-stamps-show-removed.png](09b-stamps-show-removed.png)
- Stamp page, Activity right after the one-command bootstrap: [09c-stamp-activity.png](09c-stamp-activity.png)
- Stamp page, Activity with a GPU runtime warning and a model-host failure: [09d-stamp-activity-warnings.png](09d-stamp-activity-warnings.png)
- Remove-stamp confirmation: [09e-stamp-remove-dialog.png](09e-stamp-remove-dialog.png)
- Stamp enrollment dialog: [10-stamps-enroll-dialog.png](10-stamps-enroll-dialog.png)
- Usage: [11-usage.png](11-usage.png)
- Audit log: [12-audit.png](12-audit.png)
- Audit log, filtered to "Deployment created": [13-audit-filtered.png](13-audit-filtered.png)
- Access, API keys: [14-access-api-keys.png](14-access-api-keys.png)
- Access, members with names and emails: [15-access-members.png](15-access-members.png)
- Access, service principals: [16-access-service-principals.png](16-access-service-principals.png)
- Access, identity provider: [17-access-identity-provider.png](17-access-identity-provider.png)
- Identity provider edit drawer: [17b-identity-provider-drawer.png](17b-identity-provider-drawer.png)
- Account settings: [18-settings.png](18-settings.png)
- Playground, side-by-side streamed answers from two stamps through the edge gateway: [19-playground.png](19-playground.png)
- Architecture explorer: [20-architecture.png](20-architecture.png)
- Agents, runs: [21-agents-runs.png](21-agents-runs.png)
- Agents, run trace: [22-agents-run-detail.png](22-agents-run-detail.png)
- Agents, agent playground: [23-agents-playground.png](23-agents-playground.png)

`drawers/` holds the create and edit drawers; `drawers/create-deployment.png` is regenerated
by the same command, the others were captured when the drawers were introduced. `agents/`
holds the step-by-step agent playground walkthrough from
[docs/console-agents.md](../../console-agents.md).
