package controlplane

import (
	"context"
	"net/http"
	"net/url"
)

// ReportServingMetrics sends serving-metric samples for this stamp's deployments and GPUs
// (see internal/servingmetrics). Authorized by the agent credential, like host reports;
// unlike the collector's /v1/telemetry/metrics sample, the control plane stores these points
// as a time series the console charts.
func (c *Client) ReportServingMetrics(ctx context.Context, stampID string, report any) error {
	path := "/v1/stamps/" + url.PathEscape(stampID) + "/metrics"
	return c.do(ctx, http.MethodPost, path, report, nil, true)
}
